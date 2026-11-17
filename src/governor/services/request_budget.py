"""Per-cycle budget of heavy (private REST) checks."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from binance.rest_weight import RestWeightLedger, shared_rest_weight_ledger
from domain.models import FuturesPosition, TradeAction
from governor.cycle_context import CycleContext
from governor.evaluation.outcomes import EventJournal
from governor.evaluation.runtime_board import StrategyNameSource
from governor.governor_trade_helpers import _ai_signal_audit_fields
from governor.request_budget import RequestBudget, RequestBudgetDecision
from scanner.signal_score import SignalPriority
from utils.events import EventChannel, observe_event


def _no_trace() -> Mapping[str, str]:
    return {}


REST_WEIGHT_PRESSURE = "REST_WEIGHT_PRESSURE"
# Heavy checks that protect capital are never postponed for REST weight.
_PROTECTED_CATEGORIES = frozenset({"OPEN_POSITION", "RECOVERY_OR_PROTECTION"})


class RequestBudgetService:
    """Build the cycle budget from config and spend it one heavy check at a time.

    ``config`` is the application config; every budget setting is optional and
    read with its default.
    """

    def __init__(
        self,
        *,
        config: Any,
        journal: EventJournal | None = None,
        strategy: StrategyNameSource | None = None,
        trace_payload: Callable[[], Mapping[str, str]] = _no_trace,
        rest_weight: RestWeightLedger | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.strategy = strategy
        self.trace_payload = trace_payload
        self.rest_weight = rest_weight or shared_rest_weight_ledger()

    # --- settings -----------------------------------------------------------------

    def enabled(self) -> bool:
        return bool(getattr(self.config, "governor_request_budget_enabled", True))

    def max_heavy_checks_per_cycle(self) -> int:
        value = int(
            getattr(self.config, "governor_request_budget_max_heavy_checks_per_cycle", 3) or 0
        )
        return max(0, value)

    def open_position_override(self) -> bool:
        return bool(getattr(self.config, "governor_request_budget_open_position_override", True))

    def max_heavy_checks_by_category(self) -> dict[str, int] | None:
        value = getattr(self.config, "governor_request_budget_max_heavy_checks_by_category", None)
        if not value:
            return None
        if not isinstance(value, dict):
            return None
        return {str(key).upper().strip(): int(limit) for key, limit in value.items()}

    def allow_borrowing(self) -> bool:
        return bool(getattr(self.config, "governor_request_budget_allow_borrowing", True))

    def rest_weight_guard_mode(self) -> str:
        return str(getattr(self.config, "rest_weight_guard_mode", "OBSERVE") or "OFF").upper()

    def rest_weight_guard_ratio(self) -> float:
        return float(getattr(self.config, "rest_weight_guard_ratio", 0.8) or 0.8)

    # --- budget -------------------------------------------------------------------

    def build_budget(self) -> RequestBudget | None:
        if not self.enabled():
            return None

        return RequestBudget(
            max_heavy_checks_per_cycle=self.max_heavy_checks_per_cycle(),
            open_position_override=self.open_position_override(),
            max_heavy_checks_by_category=self.max_heavy_checks_by_category(),
            allow_budget_borrowing=self.allow_borrowing(),
        )

    @staticmethod
    def summary(context: CycleContext | None) -> dict[str, Any]:
        budget = getattr(context, "request_budget", None)
        if budget is None:
            return {"enabled": False}
        summary: dict[str, Any] = budget.summary()
        return summary

    def consume_heavy_check(
        self,
        *,
        symbol_config: Any,
        score: Any,
        position: FuturesPosition,
        context: CycleContext | None,
    ) -> RequestBudgetDecision | None:
        budget = getattr(context, "request_budget", None)
        if budget is None:
            return None

        fast_lane_symbols: set[str] = getattr(context, "fast_lane_symbols", set())
        fast_lane = symbol_config.symbol in fast_lane_symbols
        category = self.category(score=score, position=position, fast_lane=fast_lane)
        decision: RequestBudgetDecision | None = self._rest_weight_decision(
            budget,
            symbol=symbol_config.symbol,
            score=score,
            position_open=position.is_open,
            fast_lane=fast_lane,
            category=category,
        )
        if decision is None:
            decision = budget.evaluate_heavy_check(
                symbol=symbol_config.symbol,
                priority=score.priority.value,
                action=score.action,
                position_open=position.is_open,
                fast_lane=fast_lane,
                category=category,
            )

        if decision.allowed:
            return decision

        strategy_name = self._strategy_name()
        observe_event(
            "heavy_check_budget_exhausted",
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            severity="WARNING",
            **self.trace_payload(),
            symbol=symbol_config.symbol,
            strategy=strategy_name,
            score=score.score,
            priority=score.priority.value,
            action=score.action,
            reason=decision.reason,
            request_budget=decision.to_dict(),
        )
        self._journal().append(
            "heavy_check_skipped_by_request_budget",
            symbol=symbol_config.symbol,
            strategy=self._strategy_name(),
            score=score.score,
            priority=score.priority.value,
            action=score.action,
            reason=decision.reason,
            request_budget=decision.to_dict(),
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10E: heavy snapshot adiado para economizar REST. "
                    "Nao bloqueia posicao aberta quando override esta ativo."
                ),
            },
            **_ai_signal_audit_fields(
                score,
                final_action="NO_EXECUTION",
                block_reason=decision.reason,
            ),
        )
        return decision

    def _rest_weight_decision(
        self,
        budget: RequestBudget,
        *,
        symbol: str,
        score: Any,
        position_open: bool,
        fast_lane: bool,
        category: str,
    ) -> RequestBudgetDecision | None:
        """Postpone (ENFORCE) or report (OBSERVE) an optional heavy check under REST pressure.

        The pressure is the used weight Binance reported for the current minute over the limit,
        1.0 while a host backs off after a 429/418 (OPT-036, ADR-005).
        """

        mode = self.rest_weight_guard_mode()
        if mode == "OFF" or category in _PROTECTED_CATEGORIES:
            return None
        pressure = self.rest_weight.pressure()
        ratio = self.rest_weight_guard_ratio()
        if pressure is None or pressure < ratio:
            return None
        enforced = mode == "ENFORCE"
        observe_event(
            "rest_weight_pressure",
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            severity="WARNING",
            **self.trace_payload(),
            symbol=symbol,
            heavy_check_category=category,
            pressure=round(pressure, 4),
            guard_ratio=ratio,
            mode=mode,
            heavy_check_postponed=enforced,
        )
        if not enforced:
            return None
        return budget.deny(
            reason=REST_WEIGHT_PRESSURE,
            symbol=symbol,
            priority=score.priority.value,
            action=score.action,
            position_open=position_open,
            fast_lane=fast_lane,
            category=category,
        )

    @staticmethod
    def category(*, score: Any, position: FuturesPosition, fast_lane: bool) -> str:
        if position.is_open:
            return "OPEN_POSITION"
        action = str(getattr(score, "action", "") or "").upper().strip()
        if action in {"CLOSE_POSITION", "PROTECT", "RECOVERY"}:
            return "RECOVERY_OR_PROTECTION"
        if fast_lane:
            return "FAST_LANE"
        priority = str(getattr(getattr(score, "priority", None), "value", "") or "").upper().strip()
        if priority in {"READY", "HOT", "WARM", "COLD"}:
            return priority
        return "COLD"

    @staticmethod
    def needs_heavy_check(score: Any, position: FuturesPosition) -> bool:
        if position.is_open:
            return True
        if score.priority in {SignalPriority.HOT, SignalPriority.READY}:
            return True
        action = str(score.action or "").upper()
        return action in {
            TradeAction.OPEN_LONG.value,
            TradeAction.OPEN_SHORT.value,
            TradeAction.CLOSE_POSITION.value,
        }

    def _journal(self) -> EventJournal:
        if self.journal is None:
            raise RuntimeError("RequestBudgetService needs a journal to record skips")
        return self.journal

    def _strategy_name(self) -> str:
        if self.strategy is None:
            raise RuntimeError("RequestBudgetService needs the strategy coordinator")
        return self.strategy.global_strategy_name
