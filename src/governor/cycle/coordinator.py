"""CycleCoordinator: one Governor cycle as an explicit sequence of phases.

    open → control requests → session baseline → position audit → symbol selection
    → light phase (stages 1–8, every selected symbol) → heavy prefetch (parallel)
    → heavy phase (stages 9–22, sequential) → finalize → close

The heavy phase stays sequential on purpose: an order invalidates the private
snapshots prefetched for the symbols that come after it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

from config.symbol_context import SymbolTradingConfig
from governor.cycle_context import CycleContext
from governor.defensive_mode import DefensiveModeEvaluation
from governor.governor_observability import elapsed_ms, monotonic_now
from governor.models import SymbolRuntimeStatus
from governor.services import DefensiveModeService
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext
from utils.events import EventChannel, observe_error, observe_event

Clock = Callable[[], float]
Port = Callable[..., Any]


@dataclass(frozen=True)
class CyclePorts:
    """Governor collaborators one cycle reads or notifies, named by phase."""

    # open / close
    next_cycle_id: Callable[[], str]
    cycle_scope: Callable[[str], AbstractContextManager[Any]]
    start_cycle_context: Callable[[CycleContext], None]
    build_request_budget: Port
    request_budget_summary: Callable[[CycleContext], dict[str, Any]]
    symbol_configs: Collection[SymbolTradingConfig]
    collect_session_health: Callable[[str], Any]
    close_session: Callable[[CycleContext], Any]
    # control requests and session baseline
    consume_symbol_control_requests: Callable[[], Any]
    prepare_session_baseline: Callable[[CycleContext], Any]
    refresh_candle_data_hub: Callable[..., Any] | None
    # position audit and symbol selection
    audit_open_positions: Callable[..., set[str]]
    drain_dirty_symbols: Callable[[float], list[str]]
    drain_shock_events: Callable[[], list[dict[str, Any]]]
    record_shock_events: Callable[[list[dict[str, Any]]], Any]
    fast_lane_symbols_from_shocks: Callable[..., list[str]]
    market_health_snapshot: Callable[[float], dict[str, Any]]
    defensive_mode: DefensiveModeService
    defensive_mode_active: Callable[[], bool]
    set_defensive_mode_active: Callable[[bool], None]
    max_symbols_per_cycle: int
    rank_due_symbols: Callable[..., list[str]]
    record_fast_lane_prioritization: Port
    prepare_selected_snapshot: Callable[[CycleContext, list[str]], Any]
    # light, prefetch and heavy phases
    is_stopped: Callable[[str], bool]
    is_paused: Callable[[str], bool]
    set_disabled_snapshot: Callable[[SymbolTradingConfig, SymbolRuntimeStatus], Any]
    prepare_symbol_evaluation: Callable[..., SymbolEvaluationContext]
    record_symbol_error: Callable[[SymbolTradingConfig, Exception], Any]
    prefetch_heavy_snapshots: Callable[[CycleContext, list[SymbolEvaluationContext]], Any]
    resume_symbol_evaluation: Callable[[SymbolEvaluationContext], Any]
    # finalize
    write_state: Callable[..., Any]
    record_learning_recommendations: Callable[[], Any]
    record_learning_shadow_impact_report: Callable[[], Any]
    record_cycle_metrics: Port
    slow_cycle_threshold_ms: Callable[..., float]


@dataclass
class CycleRun:
    """Everything one cycle accumulates; the final audit event reads it."""

    cycle_id: str
    current_time: float
    started_perf: float
    context: CycleContext
    open_symbols: set[str] = field(default_factory=set)
    dirty_symbols: list[str] = field(default_factory=list)
    due_symbols: list[str] = field(default_factory=list)
    shock_events: list[dict[str, Any]] = field(default_factory=list)
    fast_lane_symbols: list[str] = field(default_factory=list)
    symbol_timings: list[dict[str, Any]] = field(default_factory=list)
    market_health: dict[str, Any] = field(default_factory=lambda: {"enabled": False})
    defensive_mode: DefensiveModeEvaluation | None = None
    status: str = "OK"

    def time_symbol(self, symbol: str, status: str, started_perf: float) -> None:
        self.symbol_timings.append(
            {"symbol": symbol, "status": status, "duration_ms": elapsed_ms(started_perf)}
        )


PreparedEvaluation = tuple[str, SymbolEvaluationContext, float]


class CycleCoordinator:
    def __init__(self, ports: CyclePorts, *, clock: Clock) -> None:
        self.ports = ports
        self.clock = clock

    def run(self, *, now: float | None = None) -> None:
        ports = self.ports
        current_time = self.clock() if now is None else now
        cycle_id = ports.next_cycle_id()
        with ports.cycle_scope(cycle_id):
            run = self._open(cycle_id, current_time)
            try:
                ports.consume_symbol_control_requests()
                self._prepare_session(run)
                run.open_symbols = ports.audit_open_positions(context=run.context)
                self._select_symbols(run)
                prepared = self._light_phase(run)
                ports.prefetch_heavy_snapshots(
                    run.context, [evaluation for _, evaluation, _ in prepared]
                )
                self._heavy_phase(run, prepared)
                self._finalize(run)
            except Exception as error:
                run.status = "ERROR"
                observe_error(
                    "cycle_error",
                    error=error,
                    component=__name__,
                    category="governor",
                    channel=EventChannel.AUDIT,
                    cycle_id=cycle_id,
                )
                raise
            finally:
                self._close(run)

    # --- phases ----------------------------------------------------------------------

    def _open(self, cycle_id: str, current_time: float) -> CycleRun:
        ports = self.ports
        started_perf = monotonic_now()
        context = CycleContext(
            now=current_time,
            cycle_id=cycle_id,
            request_budget=ports.build_request_budget(),
        )
        ports.start_cycle_context(context)
        symbols = [item.symbol for item in ports.symbol_configs]
        observe_event(
            "cycle_started",
            component="governor.cycle",
            category="governor",
            channel=EventChannel.AUDIT,
            cycle_id=cycle_id,
            symbols=symbols,
            symbol_count=len(symbols),
            request_budget=ports.request_budget_summary(context),
        )
        return CycleRun(
            cycle_id=cycle_id,
            current_time=current_time,
            started_perf=started_perf,
            context=context,
        )

    def _prepare_session(self, run: CycleRun) -> None:
        ports = self.ports
        ports.prepare_session_baseline(run.context)
        if ports.refresh_candle_data_hub is not None:
            ports.refresh_candle_data_hub(now=run.current_time)

    def _select_symbols(self, run: CycleRun) -> None:
        ports = self.ports
        context = run.context
        run.dirty_symbols = ports.drain_dirty_symbols(run.current_time)
        run.shock_events = ports.drain_shock_events()
        ports.record_shock_events(run.shock_events)
        run.fast_lane_symbols = ports.fast_lane_symbols_from_shocks(
            shock_events=run.shock_events,
            now=run.current_time,
        )
        context.fast_lane_symbols = set(run.fast_lane_symbols)

        request_budget_summary = ports.request_budget_summary(context)
        run.market_health = ports.market_health_snapshot(run.current_time)
        run.defensive_mode = ports.defensive_mode.evaluate_shadow(
            shock_events=run.shock_events,
            fast_lane_symbols=run.fast_lane_symbols,
            market_health=run.market_health,
            request_budget_summary=request_budget_summary,
            open_symbols=run.open_symbols,
            dirty_symbols=run.dirty_symbols,
        )
        context.defensive_mode = run.defensive_mode
        ports.set_defensive_mode_active(
            ports.defensive_mode.record_shadow(
                run.defensive_mode,
                previously_active=ports.defensive_mode_active(),
            )
        )

        priority_budget = max(
            ports.max_symbols_per_cycle,
            len(run.open_symbols | set(run.fast_lane_symbols)),
        )
        run.due_symbols = ports.rank_due_symbols(
            run.current_time,
            open_symbols=run.open_symbols,
            dirty_symbols=run.dirty_symbols,
            fast_lane_symbols=run.fast_lane_symbols,
        )[:priority_budget]
        ports.record_fast_lane_prioritization(
            current_time=run.current_time,
            fast_lane_symbols=run.fast_lane_symbols,
            selected_symbols=run.due_symbols,
            shock_events=run.shock_events,
        )
        ports.prepare_selected_snapshot(context, run.due_symbols)

    def _light_phase(self, run: CycleRun) -> list[PreparedEvaluation]:
        """Stages 1–8 for every selected symbol before any heavy (private) read."""

        ports = self.ports
        configs_by_symbol = {item.symbol: item for item in ports.symbol_configs}
        prepared: list[PreparedEvaluation] = []
        for symbol in run.due_symbols:
            started_perf = monotonic_now()
            symbol_config = configs_by_symbol.get(symbol)
            if symbol_config is None:
                status = "SKIPPED_UNKNOWN_SYMBOL"
                observe_event(
                    "symbol_skipped",
                    component=__name__,
                    category="governor",
                    channel=EventChannel.AUDIT,
                    severity="WARNING",
                    cycle_id=run.cycle_id,
                    symbol=symbol,
                    reason=status,
                )
                run.time_symbol(symbol, status, started_perf)
                continue

            disabled_status = self._disabled_status(symbol)
            if disabled_status is not None:
                ports.set_disabled_snapshot(symbol_config, disabled_status)
                observe_event(
                    "symbol_skipped",
                    component=__name__,
                    category="governor",
                    channel=EventChannel.AUDIT,
                    cycle_id=run.cycle_id,
                    symbol=symbol,
                    reason=disabled_status.value,
                )
                run.time_symbol(symbol, disabled_status.value, started_perf)
                continue

            try:
                evaluation = ports.prepare_symbol_evaluation(
                    symbol_config,
                    run.current_time,
                    context=run.context,
                )
            except Exception as error:
                ports.record_symbol_error(symbol_config, error)
                run.time_symbol(symbol, "ERROR", started_perf)
                continue

            if evaluation.terminal_result is not None:
                run.time_symbol(symbol, "OK", started_perf)
                continue

            prepared.append((symbol, evaluation, started_perf))
        return prepared

    def _disabled_status(self, symbol: str) -> SymbolRuntimeStatus | None:
        if self.ports.is_stopped(symbol):
            return SymbolRuntimeStatus.STOPPED
        if self.ports.is_paused(symbol):
            return SymbolRuntimeStatus.PAUSED
        return None

    def _heavy_phase(self, run: CycleRun, prepared: list[PreparedEvaluation]) -> None:
        """Stages 9–22, one symbol at a time (an order invalidates later prefetches)."""

        ports = self.ports
        ordered_prepared = sorted(
            enumerate(prepared),
            key=lambda item: (
                0 if bool(getattr(getattr(item[1][1], "position", None), "is_open", False)) else 1,
                -int(getattr(getattr(item[1][1], "score", None), "score", 0) or 0),
                item[0],
            ),
        )
        for _, (symbol, evaluation, started_perf) in ordered_prepared:
            status = "OK"
            try:
                ports.resume_symbol_evaluation(evaluation)
            except Exception as error:
                status = "ERROR"
                ports.record_symbol_error(evaluation.evaluation_input.symbol_config, error)
            finally:
                run.time_symbol(symbol, status, started_perf)

    def _finalize(self, run: CycleRun) -> None:
        ports = self.ports
        context = run.context
        request_budget_summary = ports.request_budget_summary(context)
        ports.write_state(context=context)
        ports.record_learning_recommendations()
        ports.record_learning_shadow_impact_report()
        ports.record_cycle_metrics(
            current_time=run.current_time,
            cycle_duration_ms=elapsed_ms(run.started_perf),
            due_symbols=run.due_symbols,
            dirty_symbols=run.dirty_symbols,
            open_symbols=run.open_symbols,
            shock_events=run.shock_events,
            fast_lane_symbols=run.fast_lane_symbols,
            request_budget_summary=request_budget_summary,
            defensive_mode_summary=DefensiveModeService.summary(run.defensive_mode),
            defensive_blocks=context.defensive_blocks,
            market_health=run.market_health,
            symbol_timings=run.symbol_timings,
            cycle_id=run.cycle_id,
            operational_snapshot=context.operational_snapshot,
        )

    def _close(self, run: CycleRun) -> None:
        ports = self.ports
        cycle_duration_ms = elapsed_ms(run.started_perf)
        slow_threshold_ms = ports.slow_cycle_threshold_ms(
            len(run.symbol_timings) or len(run.due_symbols),
            open_symbols=len(run.open_symbols),
        )
        is_slow_cycle = slow_threshold_ms > 0 and cycle_duration_ms >= slow_threshold_ms
        observe_event(
            "cycle_finished",
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            severity="WARNING" if is_slow_cycle else "INFO",
            cycle_id=run.cycle_id,
            status=run.status,
            duration_ms=cycle_duration_ms,
            slow_threshold_ms=slow_threshold_ms,
            slow_cycle=is_slow_cycle,
            due_symbols=run.due_symbols,
            open_symbols=sorted(run.open_symbols),
            dirty_symbols=run.dirty_symbols,
            fast_lane_symbols=run.fast_lane_symbols,
            symbol_timings=run.symbol_timings,
        )
        ports.collect_session_health("cycle_end")
        ports.close_session(run.context)
