"""Execution preparation: smart execution (20) and the execution planner (21)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.governor_trade_helpers import (
    EXECUTION_OPEN_ACTIONS,
    INTELLIGENT_FINAL_STATUS_WAITING_BY_EXECUTION_PLANNER,
    _ai_signal_audit_fields,
)
from governor.smart_execution import (
    SmartExecutionDecision,
    SmartExecutionEngine,
    SmartExecutionInput,
)
from governor.stages.ports import StateTransitionRecorder, TracePayload, required
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext, SymbolEvaluationResult
from governor.symbol_state_machine import SymbolOperationalState
from operational_module_modes import MODULE_SMART_EXECUTION
from trading.intelligent_execution_planner import IntelligentExecutionPlan
from utils.events import EventChannel, observe_event

_LIMIT_NOT_IMPLEMENTED = "SMART_EXECUTION_LIMIT_NOT_IMPLEMENTED"


class SmartExecutionStage:
    """Stage 20: choose the entry order type; LIMIT entries are not supported live."""

    name = "run_smart_execution"

    def __init__(
        self,
        *,
        outcomes: StageOutcomes,
        module_modes: Mapping[str, Any],
        engine: Callable[[], SmartExecutionEngine],
        config: Any,
        journal: EventJournal,
        trace_payload: TracePayload,
        input_factory: Callable[[SymbolEvaluationContext], SmartExecutionInput] | None = None,
        limit_entry_supported: bool = False,
        audit_category: str = "governor",
        audit: Callable[..., None] = observe_event,
    ) -> None:
        self.outcomes = outcomes
        self.module_modes = module_modes
        self.engine = engine
        self.config = config
        self.journal = journal
        self.trace_payload = trace_payload
        self.input_factory = input_factory
        self.limit_entry_supported = limit_entry_supported
        self.audit_category = audit_category
        self.audit = audit

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        intent = required(evaluation.intent, "intent")
        action = intent.action
        mode = self.module_modes[MODULE_SMART_EXECUTION]
        if mode.value == "OFF" or action not in EXECUTION_OPEN_ACTIONS:
            return None

        engine = self.engine()
        config = self.config
        heavy = evaluation.heavy
        price = Decimal(str(intent.market_price))
        execution_input = (
            self.input_factory(evaluation)
            if self.input_factory is not None
            else SmartExecutionInput(
                symbol=symbol_config.symbol,
                side=action,
                entry_price=price,
                current_price=getattr(getattr(heavy, "market", None), "price", price),
                mark_price=getattr(heavy, "mark_price", None),
                estimated_spread_bps=getattr(config, "estimated_spread_bps", None),
                estimated_slippage_bps=getattr(config, "estimated_slippage_bps", None),
                limit_entry_enabled=getattr(config, "limit_entry_enabled", False),
                limit_entry_timeout_seconds=getattr(config, "limit_entry_timeout_seconds", None),
                limit_entry_max_price_drift_bps=getattr(
                    config, "limit_entry_max_price_drift_bps", None
                ),
                limit_entry_fallback_to_market=getattr(
                    config, "limit_entry_fallback_to_market", False
                ),
                limit_entry_post_only=getattr(config, "limit_entry_post_only", True),
                market_entry_require_economic_gate=getattr(
                    config, "market_entry_require_economic_gate", True
                ),
                planned_exit=evaluation.planned_exit,
                notional=(
                    Decimal(str(evaluation.planned_quantity)) * price
                    if evaluation.planned_quantity is not None
                    else None
                ),
                mode=mode,
                market_data_age_seconds=getattr(heavy, "data_age_seconds", None),
                market_data_stale=getattr(heavy, "stale", False),
            )
        )
        decision = engine.evaluate(execution_input)
        evaluation.smart_execution_decision = decision
        if (
            mode.can_affect_execution
            and decision.entry_order_type == "LIMIT"
            and not self.limit_entry_supported
        ):
            self._record_runtime_limit_block(
                symbol=symbol_config.symbol, action=action, decision=decision
            )
            return self.outcomes.blocked(
                evaluation,
                stage="SMART_EXECUTION",
                classification="execucao",
                reason=_LIMIT_NOT_IMPLEMENTED,
                action=action,
            )
        self._record_decision(symbol=symbol_config.symbol, action=action, decision=decision)
        if decision.blocked:
            return self.outcomes.blocked(
                evaluation,
                stage="SMART_EXECUTION",
                classification="execucao",
                reason="SMART_EXECUTION_BLOCKED",
                action=action,
            )
        return None

    def _record_runtime_limit_block(
        self, *, symbol: str, action: str, decision: SmartExecutionDecision
    ) -> None:
        payload = {
            **decision.to_dict(),
            "blocked": True,
            "reason": _LIMIT_NOT_IMPLEMENTED,
            "affected_result": True,
            "runtime_limit_supported": False,
        }
        self.journal.append(
            "smart_execution_blocked",
            **self.trace_payload(),
            symbol=symbol,
            action=action,
            mode=decision.mode.value,
            affected_result=True,
            order_type=decision.entry_order_type,
            requested_entry_order_type=decision.entry_order_type,
            reason=_LIMIT_NOT_IMPLEMENTED,
            diagnostics=payload,
            execution_changed=True,
            order_mutation=False,
        )
        self.audit(
            "smart_execution_blocked",
            component=__name__,
            category=self.audit_category,
            channel=EventChannel.AUDIT,
            symbol=symbol,
            action=action,
            mode=decision.mode.value,
            affected_result=True,
            order_type=decision.entry_order_type,
            reason=_LIMIT_NOT_IMPLEMENTED,
        )

    def _record_decision(
        self, *, symbol: str, action: str, decision: SmartExecutionDecision
    ) -> None:
        payload = decision.to_dict()
        event_type = "smart_execution_evaluated"
        if decision.blocked:
            event_type = "smart_execution_blocked"
        elif decision.affected_result:
            event_type = "smart_execution_applied"
        elif decision.mode.value == "SHADOW":
            event_type = "smart_execution_shadow_decision"
        self.journal.append(
            event_type,
            **self.trace_payload(),
            symbol=symbol,
            action=action,
            mode=decision.mode.value,
            affected_result=decision.affected_result,
            order_type=decision.entry_order_type,
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
            order_type=decision.entry_order_type,
            reason=decision.reason,
        )


class ExecutionPlannerStage:
    """Stage 21: the intelligent execution planner may ask the entry to wait."""

    name = "run_execution_planner"

    def __init__(
        self,
        *,
        outcomes: StageOutcomes,
        evaluate_plan: Callable[..., IntelligentExecutionPlan | None],
        record_finalized: Callable[..., Any],
        journal: EventJournal,
        record_transition: StateTransitionRecorder,
    ) -> None:
        self.outcomes = outcomes
        self.evaluate_plan = evaluate_plan
        self.record_finalized = record_finalized
        self.journal = journal
        self.record_transition = record_transition

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        intent = required(evaluation.intent, "intent")
        action = intent.action

        evaluation.trade_plan_id = (
            evaluation.trade_pipeline_result.plan.plan_id
            if evaluation.trade_pipeline_result is not None
            else None
        )
        plan = self.evaluate_plan(
            symbol_config=symbol_config,
            action=action,
            intent=intent,
            heavy_snapshot=evaluation.heavy,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            trade_plan_id=evaluation.trade_plan_id,
        )
        evaluation.intelligent_execution_plan = plan
        if plan is None or plan.should_execute:
            return None

        self.record_finalized(
            symbol_config=symbol_config,
            action=action,
            activation_decision=evaluation.intelligent_order_activation,
            governor_decision=evaluation.portfolio_decision,
            final_status=INTELLIGENT_FINAL_STATUS_WAITING_BY_EXECUTION_PLANNER,
            final_reason=plan.reason,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            planned_quantity_reason=evaluation.planned_quantity_reason,
            planned_exit_reason=evaluation.planned_exit_reason,
            execution_will_run=False,
        )
        self.journal.append(
            "trade_waiting_by_execution_planner",
            symbol=symbol_config.symbol,
            action=action,
            reason=plan.reason,
            plan_id=plan.plan_id,
            trade_plan_id=plan.trade_plan_id,
            execution_style=plan.execution_style,
            price_drift_bps=plan.price_drift_bps,
            market_data_age_seconds=plan.market_data_age_seconds,
            market_data_stale=plan.market_data_stale,
            execution_plan=plan,
            diagnostics=plan.diagnostics,
            **_ai_signal_audit_fields(
                evaluation.score,
                final_action="WAIT",
                block_reason=plan.reason,
            ),
        )
        self.outcomes.board.mark_blocked(symbol_config.symbol, plan.reason)
        self.record_transition(
            evaluation,
            to_state=SymbolOperationalState.EXECUTION_WAITING,
            reason=plan.reason,
            stage="EXECUTION_PLANNER",
            action=action,
        )
        return self.outcomes.terminal(
            evaluation,
            stage="EXECUTION_PLANNER",
            classification="degradacao",
            reason=plan.reason,
            action=action,
        )
