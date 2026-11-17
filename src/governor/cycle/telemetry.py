"""Cycle telemetry: market health, shock drains and the throttled cycle metrics event."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from binance.rest_weight import RestWeightLedger, shared_rest_weight_ledger
from governor.evaluation.outcomes import EventJournal
from governor.evaluation.stage_telemetry import StageTelemetry
from governor.events import GOVERNOR_CYCLE_METRICS
from governor.governor_observability import build_cycle_metrics_payload
from market_data.snapshot_provider import snapshot_diagnostics_payload
from utils.process_metrics import process_memory_snapshot


@dataclass
class CycleMetricsState:
    """When the last ``governor_cycle_metrics`` event was written (0 means never)."""

    last_recorded_at: float = 0.0
    # REST ledger totals at the last metrics event, to report what happened since.
    last_rest_totals: dict[str, int] = field(default_factory=dict)


def _no_cycle_id() -> str:
    return ""


class CycleTelemetry:
    """Read the market data hub for the cycle and record the cycle metrics event.

    The metrics event is throttled: it is always written for a cycle with market
    shocks or slower than the slow-cycle budget, and otherwise at most once per
    ``governor_cycle_metrics_interval_seconds``.
    """

    def __init__(
        self,
        *,
        config: Any,
        journal: EventJournal,
        logger: Any,
        market_data_hub: Any,
        state: CycleMetricsState,
        current_cycle_id: Callable[[], str] = _no_cycle_id,
        rest_weight: RestWeightLedger | None = None,
        stage_telemetry: StageTelemetry | None = None,
    ) -> None:
        self.stage_telemetry = stage_telemetry
        self.config = config
        self.rest_weight = rest_weight or shared_rest_weight_ledger()
        self.journal = journal
        self.logger = logger
        self.market_data_hub = market_data_hub
        self.state = state
        self.current_cycle_id = current_cycle_id

    # --- market data hub ----------------------------------------------------------------

    def market_health(self, current_time: float) -> dict[str, Any]:
        if self.market_data_hub is None:
            return {"enabled": False}

        try:
            health: dict[str, Any] = self.market_data_hub.health_snapshot(now=current_time)
            return health
        except Exception as error:
            self.logger.warning("Falha ao coletar market health: %s", error)
            return {
                "enabled": True,
                "connected": False,
                "running": False,
                "stale_symbols": 0,
                "largest_price_age_seconds": 0.0,
                "last_error": str(error),
            }

    def rest_weight_payload(self) -> dict[str, Any]:
        """The shared REST ledger, with what changed since the previous metrics event."""

        totals = self.rest_weight.totals()
        previous = self.state.last_rest_totals
        self.state.last_rest_totals = dict(totals)
        payload: dict[str, Any] = self.rest_weight.snapshot()
        payload["enabled"] = True
        payload["pressure"] = self.rest_weight.pressure()
        payload["since_last_metrics"] = {
            key: value - previous.get(key, 0) for key, value in totals.items()
        }
        return payload

    def drain_dirty_symbols(self, now: float) -> list[str]:
        if self.market_data_hub is None:
            return []
        try:
            symbols: list[str] = self.market_data_hub.drain_dirty_symbols(now=now)
            return symbols
        except Exception as error:
            self.logger.warning("Falha ao drenar simbolos atualizados via WebSocket: %s", error)
            return []

    def drain_shock_events(self) -> list[dict[str, Any]]:
        if self.market_data_hub is None:
            return []

        drain_events = getattr(self.market_data_hub, "drain_market_shock_events", None)
        if not callable(drain_events):
            return []

        try:
            events: list[dict[str, Any]] = drain_events()
            return events
        except Exception as error:
            self.logger.warning("Falha ao drenar choques de mercado: %s", error)
            return []

    def record_shock_events(self, shock_events: list[dict[str, Any]]) -> None:
        for event in shock_events:
            self.journal.append("market_shock_detected", **event)

    # --- cycle metrics ------------------------------------------------------------------

    def observability_enabled(self) -> bool:
        return bool(getattr(self.config, "governor_observability_enabled", True))

    def slow_cycle_threshold_ms(self, evaluated_symbols: int, *, open_symbols: int = 0) -> float:
        """Effective slow-cycle budget for this cycle.

        Each evaluated symbol performs its own REST round-trips, so the budget
        scales with the symbol count. Symbols with open positions add another
        work unit because their safety path must refresh private position and
        protection state even when the strategy returns HOLD. A fixed global
        threshold otherwise labels healthy protected-position cycles as slow.
        """
        base = float(getattr(self.config, "governor_slow_cycle_ms", 0.0) or 0.0)
        if base <= 0:
            return 0.0
        work_units = max(1, evaluated_symbols) + max(0, open_symbols)
        return base * work_units

    def should_record(
        self,
        *,
        current_time: float,
        cycle_duration_ms: float,
        shock_events: list[dict[str, Any]],
        evaluated_symbols: int = 1,
        open_symbols: int = 0,
    ) -> bool:
        if not self.observability_enabled():
            return False

        if shock_events:
            return True

        slow_cycle_ms = self.slow_cycle_threshold_ms(evaluated_symbols, open_symbols=open_symbols)
        if slow_cycle_ms > 0 and cycle_duration_ms >= slow_cycle_ms:
            return True

        interval_seconds = float(
            getattr(self.config, "governor_cycle_metrics_interval_seconds", 0.0) or 0.0
        )
        if interval_seconds <= 0:
            return True

        last_recorded_at = float(self.state.last_recorded_at or 0.0)
        return last_recorded_at <= 0 or current_time - last_recorded_at >= interval_seconds

    def record_cycle_metrics(
        self,
        *,
        current_time: float,
        cycle_duration_ms: float,
        due_symbols: list[str],
        dirty_symbols: list[str],
        open_symbols: set[str],
        shock_events: list[dict[str, Any]],
        symbol_timings: list[dict[str, Any]],
        fast_lane_symbols: list[str] | None = None,
        request_budget_summary: dict[str, Any] | None = None,
        defensive_mode_summary: dict[str, Any] | None = None,
        defensive_blocks: list[dict[str, Any]] | None = None,
        market_health: dict[str, Any] | None = None,
        cycle_id: str | None = None,
        operational_snapshot: Any = None,
    ) -> None:
        if not self.should_record(
            current_time=current_time,
            cycle_duration_ms=cycle_duration_ms,
            shock_events=shock_events,
            evaluated_symbols=len(symbol_timings),
            open_symbols=len(open_symbols),
        ):
            return

        try:
            payload = build_cycle_metrics_payload(
                cycle_started_at=current_time,
                cycle_duration_ms=cycle_duration_ms,
                due_symbols=due_symbols,
                dirty_symbols=dirty_symbols,
                open_symbols=open_symbols,
                shock_events=shock_events,
                symbol_timings=symbol_timings,
                fast_lane_symbols=fast_lane_symbols or [],
                request_budget_summary=request_budget_summary or {"enabled": False},
                defensive_mode_summary=defensive_mode_summary
                or {
                    "enabled": False,
                    "mode": "DISABLED",
                },
                defensive_blocks=defensive_blocks or [],
                market_health=market_health or self.market_health(current_time),
                snapshot_diagnostics=snapshot_diagnostics_payload(operational_snapshot),
                rest_weight=self.rest_weight_payload(),
            )
            resolved_cycle_id = cycle_id or self.current_cycle_id()
            if resolved_cycle_id:
                payload["cycle_id"] = resolved_cycle_id
            payload["process_memory"] = process_memory_snapshot()
            if self.stage_telemetry is not None:
                # G9: per-stage counts and durations since the previous metrics event.
                payload["stage_telemetry"] = self.stage_telemetry.summary_and_reset()
            self.journal.append(GOVERNOR_CYCLE_METRICS, **payload)
            self.state.last_recorded_at = current_time
        except Exception as error:
            self.logger.warning("Falha ao registrar metricas do ciclo: %s", error)
