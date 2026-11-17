"""Journal and audit events of a trade between its plan and the dispatch of its order."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from config.symbol_context import SymbolTradingConfig
from futures.planning.trade_plan_execution_validator import TradePlanExecutionValidation
from governor.evaluation.outcomes import EventJournal
from governor.event_payloads import (
    planned_exit_stop,
    planned_exit_take,
    portfolio_trade_approved_payload,
    pre_execution_guard_payload,
)
from governor.event_sink import EventSink
from governor.events import (
    EXECUTION_PAYLOAD_SENT,
    PORTFOLIO_TRADE_APPROVED,
    PRE_EXECUTION_GUARD_PASSED,
    PRE_EXECUTION_PAYLOAD_GUARD,
    TRADE_BLOCKED_BY_PRE_EXECUTION_GUARD,
)
from governor.execution_engine import ExecutionCommand
from governor.governor_trade_helpers import (
    EXECUTION_OPEN_ACTIONS,
    _ai_signal_audit_fields,
    _close_source_from_intent,
    _exit_diagnostics_from_plan,
    _seconds_since_open,
)
from governor.pre_execution_guard import PreExecutionGuardDecision
from governor.safe_journal import safe_append
from governor.services.economic_gate import economic_gate_event_payload
from governor.stages.ports import required
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext
from trading.intent import metadata_to_plain_dict

TracePayload = Callable[[], Mapping[str, str]]
PlannedExit = dict[str, Any] | None


def _no_trace() -> Mapping[str, str]:
    return {}


def _plan_of(trade_pipeline_result: Any) -> Any:
    return trade_pipeline_result.plan if trade_pipeline_result is not None else None


def _exit_source(planned_exit: PlannedExit) -> Any:
    return planned_exit.get("source") if isinstance(planned_exit, dict) else None


def _passed_exit(planned_exit: PlannedExit, key: str) -> str | None:
    return str(planned_exit[key]) if planned_exit is not None else None


class TradeEventRecorder:
    """Write the evidence trail of one trade: approvals, guards, plans and payloads.

    Recording never decides anything: every method only describes a decision that
    a stage already took.
    """

    def __init__(
        self,
        *,
        journal: EventJournal,
        logger: Any = None,
        trace_payload: TracePayload = _no_trace,
        config: Any = None,
        pipeline_events: EventSink | None = None,
        trade_audit_recorder: Any = None,
    ) -> None:
        self.journal = journal
        self.logger = logger
        self.trace_payload = trace_payload
        self.config = config
        self.pipeline_events = pipeline_events
        self.trade_audit_recorder = trade_audit_recorder

    # --- approval and pre-execution guard -------------------------------------------------

    def record_portfolio_trade_approved(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        action: str,
    ) -> None:
        symbol_config = evaluation.evaluation_input.symbol_config
        portfolio_decision = required(evaluation.portfolio_decision, "portfolio_decision")
        payload = portfolio_trade_approved_payload(
            trace=dict(self.trace_payload()),
            symbol=symbol_config.symbol,
            action=action,
            reason=portfolio_decision.reason,
            decision=portfolio_decision,
            trade_plan_id=evaluation.trade_plan_id,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            diagnostics={
                "portfolio_allowed": getattr(portfolio_decision, "allowed", None),
                "final_economic_gate_allowed": (
                    evaluation.final_economic_validation.decision.allowed
                    if evaluation.final_economic_validation is not None
                    else None
                ),
            },
        )
        safe_append(
            self.journal,
            PORTFOLIO_TRADE_APPROVED,
            logger=self.logger,
            **payload,
            intent_id=getattr(evaluation.intent, "intent_id", None),
            **_ai_signal_audit_fields(evaluation.score, final_action=action),
        )

    def record_pre_execution_guard_decision(
        self,
        decision: PreExecutionGuardDecision,
        *,
        intent_id: str | None = None,
    ) -> None:
        payload = pre_execution_guard_payload(decision)
        safe_append(
            self.journal,
            PRE_EXECUTION_PAYLOAD_GUARD,
            logger=self.logger,
            **self.trace_payload(),
            intent_id=intent_id,
            **payload,
        )
        event_type = (
            PRE_EXECUTION_GUARD_PASSED if decision.allowed else TRADE_BLOCKED_BY_PRE_EXECUTION_GUARD
        )
        safe_append(
            self.journal,
            event_type,
            logger=self.logger,
            **self.trace_payload(),
            intent_id=intent_id,
            **payload,
        )

    # --- plan checkpoints -----------------------------------------------------------------

    def record_planned_exit_checkpoint(
        self,
        *,
        event_type: str,
        symbol_config: SymbolTradingConfig,
        action: str,
        trade_pipeline_result: Any,
        planned_quantity: Any,
        planned_exit: PlannedExit,
        planned_exit_reason: str,
    ) -> None:
        plan = _plan_of(trade_pipeline_result)
        exit_diagnostics = _exit_diagnostics_from_plan(plan)
        self.journal.append(
            event_type,
            **self.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            plan_id=getattr(plan, "plan_id", None),
            plan_status=getattr(plan, "status", None),
            planned_quantity=planned_quantity,
            planned_stop_price=planned_exit_stop(planned_exit),
            planned_take_profit_price=planned_exit_take(planned_exit),
            planned_exit_source=_exit_source(planned_exit),
            planned_exit_reason=planned_exit_reason,
            configured_stop_bps=exit_diagnostics.get("configured_stop_bps"),
            configured_take_bps=exit_diagnostics.get("configured_take_bps"),
            required_take_bps=exit_diagnostics.get("required_take_bps"),
            final_take_bps=exit_diagnostics.get("final_take_bps"),
            final_stop_bps=exit_diagnostics.get("final_stop_bps"),
            expected_net_gain=getattr(plan, "expected_net_gain", None),
            expected_net_loss=getattr(plan, "expected_net_loss", None),
            reward_risk=getattr(plan, "reward_risk", None),
            min_net_reward_risk=exit_diagnostics.get("min_net_reward_risk"),
            min_net_gain_bps=exit_diagnostics.get("min_net_gain_bps"),
            round_trip_cost=getattr(plan, "estimated_round_trip_cost", None),
            round_trip_cost_bps=exit_diagnostics.get("round_trip_cost_bps"),
            exit_model_requested=exit_diagnostics.get("exit_model_requested"),
            exit_model_effective=exit_diagnostics.get("exit_model_effective"),
            economic_exit_adjusted=exit_diagnostics.get("economic_exit_adjusted"),
            economic_exit_adjustment_reason=exit_diagnostics.get("economic_exit_adjustment_reason"),
            block_reason=getattr(plan, "block_reason", None),
            diagnostics={
                "exit_diagnostics": exit_diagnostics,
                "trade_plan_diagnostics": (
                    plan.diagnostics
                    if plan is not None and isinstance(plan.diagnostics, dict)
                    else {}
                ),
            },
        )

    def record_trade_plan_execution_ready(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        trade_pipeline_result: Any,
        planned_quantity: Any,
        reason: str,
    ) -> None:
        plan = _plan_of(trade_pipeline_result)
        self.journal.append(
            "trade_plan_execution_ready",
            symbol=symbol_config.symbol,
            action=action,
            dynamic_sizing_enabled=self.config.dynamic_sizing_enabled,
            plan_available=plan is not None,
            plan_id=plan.plan_id if plan is not None else None,
            plan_status=plan.status if plan is not None else None,
            planned_quantity=str(plan.quantity) if plan is not None else None,
            planned_quantity_passed=(
                str(planned_quantity) if planned_quantity is not None else None
            ),
            planned_quantity_applied=planned_quantity is not None,
            quantity_unchanged=planned_quantity is None,
            reason=reason,
        )

    def record_trade_plan_exit_result(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        trade_pipeline_result: Any,
        planned_exit: PlannedExit,
        reason: str,
    ) -> None:
        plan = _plan_of(trade_pipeline_result)
        self.journal.append(
            "trade_plan_exit_execution_ready",
            symbol=symbol_config.symbol,
            action=action,
            dynamic_exit_enabled=self.config.dynamic_exit_enabled,
            plan_available=plan is not None,
            plan_id=plan.plan_id if plan is not None else None,
            plan_status=plan.status if plan is not None else None,
            planned_stop_price=(
                str(plan.stop_price) if plan is not None and plan.stop_price is not None else None
            ),
            planned_take_profit_price=(
                str(plan.take_profit_price)
                if plan is not None and plan.take_profit_price is not None
                else None
            ),
            planned_stop_price_passed=_passed_exit(planned_exit, "stop_price"),
            planned_take_profit_price_passed=_passed_exit(planned_exit, "take_profit_price"),
            planned_exit_source=_exit_source(planned_exit),
            planned_exit_applied=planned_exit is not None,
            exit_unchanged=planned_exit is None,
            reason=reason,
        )

    # --- close and dispatch ---------------------------------------------------------------

    def record_close_position_event(
        self,
        event_type: str,
        *,
        symbol_config: SymbolTradingConfig,
        intent: Any,
        heavy_snapshot: Any,
        now: float,
        planned_quantity: Any,
        trade_plan_id: str | None,
        planned_exit: PlannedExit,
    ) -> None:
        position = heavy_snapshot.position
        self.journal.append(
            event_type,
            symbol=symbol_config.symbol,
            action=intent.action,
            intent_id=intent.intent_id,
            reason=intent.reason,
            close_source=_close_source_from_intent(intent),
            seconds_since_open=_seconds_since_open(position, now),
            position_direction=getattr(getattr(position, "direction", None), "value", None),
            entry_price=getattr(position, "entry_price", None),
            exit_price=getattr(intent, "market_price", None),
            quantity=getattr(position, "quantity", None),
            planned_quantity=planned_quantity,
            trade_plan_id=trade_plan_id,
            planned_stop_price=planned_exit_stop(planned_exit),
            planned_take_profit_price=planned_exit_take(planned_exit),
            stop_take_available=(
                planned_exit_stop(planned_exit) is not None
                and planned_exit_take(planned_exit) is not None
            ),
        )

    def record_execution_payload_sent(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        intent: Any,
        execution_command: ExecutionCommand,
        heavy_snapshot: Any,
        score: Any,
        planned_quantity: Any,
        trade_plan_id: str | None,
        planned_exit: PlannedExit,
        final_economic_validation: TradePlanExecutionValidation,
    ) -> None:
        decision = final_economic_validation.decision
        economic_payload = economic_gate_event_payload(
            decision,
            validation=final_economic_validation,
        )
        position = heavy_snapshot.position
        trace: dict[str, Any] = dict(self.trace_payload())
        required(self.pipeline_events, "pipeline_events").emit(
            EXECUTION_PAYLOAD_SENT,
            **trace,
            symbol=symbol_config.symbol,
            action=intent.action,
            intent_id=intent.intent_id,
            execution_command_id=execution_command.command_id,
            reason=intent.reason,
            trade_plan_id=trade_plan_id,
            planned_quantity=planned_quantity,
            planned_stop_price=planned_exit_stop(planned_exit),
            planned_take_profit_price=planned_exit_take(planned_exit),
            planned_exit_source=_exit_source(planned_exit),
            market_price=getattr(intent, "market_price", None),
            position_open=getattr(position, "is_open", False),
            position_direction=getattr(getattr(position, "direction", None), "value", None),
            position_quantity=getattr(position, "quantity", None),
            economic_gate_allowed=decision.allowed,
            economic_gate_reason=decision.reason,
            expected_net_gain=decision.expected_net_gain,
            expected_net_gain_bps=decision.expected_net_gain_bps,
            expected_net_loss=decision.expected_net_loss,
            reward_risk=decision.reward_risk,
            diagnostics={
                "final_economic_gate": economic_payload,
                "execution_validation": final_economic_validation.diagnostics,
            },
        )
        if intent.action in EXECUTION_OPEN_ACTIONS:
            self.record_trade_audit_open_candidate(
                symbol_config=symbol_config,
                intent=intent,
                score=score,
                planned_quantity=planned_quantity,
                trade_plan_id=trade_plan_id,
                planned_exit=planned_exit,
                final_economic_validation=final_economic_validation,
                economic_payload=economic_payload,
            )

    def record_trade_audit_open_candidate(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        intent: Any,
        score: Any,
        planned_quantity: Any,
        trade_plan_id: str | None,
        planned_exit: PlannedExit,
        final_economic_validation: TradePlanExecutionValidation,
        economic_payload: dict[str, Any],
    ) -> None:
        recorder = self.trade_audit_recorder
        if recorder is None:
            return

        decision = final_economic_validation.decision
        effective_plan = final_economic_validation.effective_plan

        def plan_cost(name: str) -> Any:
            return getattr(effective_plan, name) if effective_plan is not None else None

        recorder.record_open_candidate(
            symbol=symbol_config.symbol,
            trade_plan_id=trade_plan_id,
            entry_time=str(getattr(intent, "timestamp", "") or ""),
            entry_reason=intent.reason,
            strategy_score=getattr(score, "score", None),
            strategy_priority=getattr(getattr(score, "priority", None), "value", None),
            strategy_confidence=getattr(score, "confidence", None),
            entry_price=getattr(intent, "market_price", None),
            quantity=(
                final_economic_validation.final_quantity
                if final_economic_validation.final_quantity is not None
                else planned_quantity
            ),
            direction="LONG" if intent.action == "OPEN_LONG" else "SHORT",
            stop_price=planned_exit_stop(planned_exit),
            take_profit_price=planned_exit_take(planned_exit),
            planned_stop_price=planned_exit_stop(planned_exit),
            planned_take_profit_price=planned_exit_take(planned_exit),
            reward_risk_liquido=decision.reward_risk,
            expected_net_gain=decision.expected_net_gain,
            expected_net_gain_bps=decision.expected_net_gain_bps,
            expected_net_loss=decision.expected_net_loss,
            round_trip_cost=decision.round_trip_cost,
            round_trip_cost_bps=decision.round_trip_cost_bps,
            estimated_entry_fee=plan_cost("estimated_entry_fee"),
            estimated_exit_fee=plan_cost("estimated_exit_fee"),
            estimated_spread_cost=plan_cost("estimated_spread_cost"),
            estimated_slippage_cost=plan_cost("estimated_slippage_cost"),
            estimated_funding_cost=plan_cost("estimated_funding_cost"),
            diagnostics={
                "trace": self.trace_payload(),
                "final_economic_gate": economic_payload,
                "execution_validation": final_economic_validation.diagnostics,
                "intent_metadata": metadata_to_plain_dict(getattr(intent, "metadata", {})),
            },
        )
