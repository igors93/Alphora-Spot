"""Stage 17: smart sizing of a new entry."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.governor_helpers import _enum_value
from governor.governor_trade_helpers import EXECUTION_OPEN_ACTIONS, _daily_consecutive_losses
from governor.smart_sizing import SmartSizingDecision, SmartSizingEngine, SmartSizingInput
from governor.stages.ports import StateTransitionRecorder, TracePayload, required
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext, SymbolEvaluationResult
from governor.symbol_state_machine import SymbolOperationalState
from operational_module_modes import MODULE_SMART_SIZING, SmartSizingMode
from utils.events import EventChannel, observe_event


class SmartSizingStage:
    """Size the entry from the plan, the account drawdown and the symbol health."""

    name = "run_smart_sizing"

    def __init__(
        self,
        *,
        outcomes: StageOutcomes,
        module_modes: Mapping[str, Any],
        engine: Callable[[], SmartSizingEngine],
        journal: EventJournal,
        trace_payload: TracePayload,
        record_transition: StateTransitionRecorder,
        audit_category: str = "governor",
        audit: Callable[..., None] = observe_event,
    ) -> None:
        self.outcomes = outcomes
        self.module_modes = module_modes
        self.engine = engine
        self.journal = journal
        self.trace_payload = trace_payload
        self.record_transition = record_transition
        self.audit_category = audit_category
        self.audit = audit

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        intent = required(evaluation.intent, "intent")
        action = intent.action
        mode = self.module_modes[MODULE_SMART_SIZING]
        if mode is SmartSizingMode.OFF or action not in EXECUTION_OPEN_ACTIONS:
            return None

        symbol_config = evaluation.evaluation_input.symbol_config
        price = Decimal(str(intent.market_price))
        rules = getattr(evaluation.heavy, "exchange_rules", None)
        decision = self.engine().evaluate(build_smart_sizing_input(evaluation, mode))
        evaluation.smart_sizing_decision = decision
        self._record_decision(symbol=symbol_config.symbol, action=action, decision=decision)
        if decision.blocked:
            self.outcomes.board.mark_blocked(symbol_config.symbol, "SMART_SIZING_BLOCKED")
            self.record_transition(
                evaluation,
                to_state=SymbolOperationalState.BLOCKED,
                reason="SMART_SIZING_BLOCKED",
                stage="SMART_SIZING",
                action=action,
                diagnostics=decision.to_dict(),
            )
            return self._stop(evaluation, "SMART_SIZING_BLOCKED", action)
        if mode.can_affect_execution:
            if not decision.affected_result and evaluation.planned_quantity is not None:
                return None
            if rules is None:
                self.outcomes.board.mark_blocked(
                    symbol_config.symbol, "SMART_SIZING_RULES_UNAVAILABLE"
                )
                return self._stop(evaluation, "SMART_SIZING_RULES_UNAVAILABLE", action)
            quantity = rules.round_quantity(decision.final_notional_usdt / price)
            try:
                rules.validate_quantity(quantity=quantity, price=price)
            except ValueError:
                self.outcomes.board.mark_blocked(
                    symbol_config.symbol, "SMART_SIZING_QUANTITY_INVALID"
                )
                return self._stop(evaluation, "SMART_SIZING_QUANTITY_INVALID", action)
            evaluation.planned_quantity = quantity
            if decision.affected_result or not evaluation.planned_quantity_reason:
                evaluation.planned_quantity_reason = decision.reason
        return None

    def _stop(
        self, evaluation: SymbolEvaluationContext, reason: str, action: str
    ) -> SymbolEvaluationResult:
        return self.outcomes.terminal(
            evaluation,
            stage="SMART_SIZING",
            classification="risco",
            reason=reason,
            action=action,
        )

    def _record_decision(self, *, symbol: str, action: str, decision: SmartSizingDecision) -> None:
        payload = decision.to_dict()
        event_type = "smart_sizing_evaluated"
        if decision.blocked:
            event_type = "smart_sizing_blocked"
        elif decision.affected_result:
            event_type = "smart_sizing_applied"
        elif decision.mode is SmartSizingMode.SHADOW:
            event_type = "smart_sizing_shadow_decision"
        self.journal.append(
            event_type,
            **self.trace_payload(),
            symbol=symbol,
            action=action,
            mode=decision.mode.value,
            affected_result=decision.affected_result,
            base_notional=decision.base_notional_usdt,
            final_notional=decision.final_notional_usdt,
            reason=decision.reason,
            diagnostics=payload,
            execution_changed=decision.affected_result,
            order_mutation=False,
        )
        self.audit(
            event_type,
            component=__name__,
            category=self.audit_category,
            channel=EventChannel.AUDIT,
            symbol=symbol,
            action=action,
            mode=decision.mode.value,
            affected_result=decision.affected_result,
            base_notional=decision.base_notional_usdt,
            final_notional=decision.final_notional_usdt,
            reason=decision.reason,
        )


def build_smart_sizing_input(
    evaluation: SymbolEvaluationContext, mode: SmartSizingMode
) -> SmartSizingInput:
    """The pure input projection shared by live and historical stage adapters."""
    symbol_config = evaluation.evaluation_input.symbol_config
    intent = required(evaluation.intent, "intent")
    score = required(evaluation.score, "score")
    price = Decimal(str(intent.market_price))
    rules = getattr(evaluation.heavy, "exchange_rules", None)
    min_required = rules.minimum_required_notional(price) if rules is not None else None
    base_notional = (
        Decimal(str(evaluation.planned_quantity)) * price
        if evaluation.planned_quantity is not None
        else Decimal(str(symbol_config.position_notional_usdt))
    )
    metadata = getattr(score, "metadata", {}) or {}
    planned_exit = evaluation.planned_exit if isinstance(evaluation.planned_exit, dict) else {}
    drawdown = (evaluation.account_snapshot or {}).get("daily_drawdown")
    if not isinstance(drawdown, dict):
        drawdown = {"available": False, "reason": "DAILY_DRAWDOWN_NOT_COMPUTED"}
    return SmartSizingInput(
        symbol=symbol_config.symbol,
        side=intent.action,
        base_notional_usdt=base_notional,
        entry_price=price,
        planned_exit=planned_exit,
        strategy_score=score.score,
        strategy_priority=required(score.priority, "score.priority").value,
        reward_risk_liquido=planned_exit.get("reward_risk_liquido")
        or metadata.get("reward_risk_liquido"),
        cost_to_reward_ratio=planned_exit.get("cost_to_reward_ratio")
        or metadata.get("cost_to_reward_ratio"),
        risk_abs=planned_exit.get("risk_abs") or metadata.get("risk_abs"),
        reward_abs=planned_exit.get("reward_abs") or metadata.get("reward_abs"),
        max_exposure_usdt=symbol_config.max_exposure_usdt,
        max_daily_loss_usdt=symbol_config.max_daily_loss_usdt,
        consecutive_losses=_daily_consecutive_losses(evaluation.account_snapshot),
        current_drawdown=drawdown.get("fraction") if drawdown.get("available") else None,
        require_current_drawdown=True,
        drawdown_diagnostics=drawdown,
        symbol_health=evaluation.operational_state or _enum_value(evaluation.status),
        portfolio_context=evaluation.account_snapshot,
        mode=mode,
        min_required_notional_usdt=min_required,
    )
