"""Planning stages: trade plan and economic gate (15) and intelligent layers (16).

Both stages orchestrate planning helpers that still live in the Governor
(``TradePipelineMixin``). They receive them as named ports; the helpers become
services in G8 without changing these stages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.events import (
    PLANNED_EXIT_AFTER_INTELLIGENT_LAYERS,
    PLANNED_EXIT_BEFORE_INTELLIGENT_LAYERS,
    TRADE_BLOCKED_BY_ECONOMIC_GATE,
)
from governor.governor_trade_helpers import _ai_signal_audit_fields, _merge_planned_exit_candidate
from governor.stages.ports import StateTransitionRecorder, TracePayload, required
from governor.symbol_evaluation_pipeline import (
    IntentCandidate,
    PlannedTrade,
    SymbolEvaluationContext,
    SymbolEvaluationResult,
)
from governor.symbol_state_machine import SymbolOperationalState

Port = Callable[..., Any]


@dataclass(frozen=True)
class TradePlanPorts:
    portfolio_account_snapshot: Port
    record_shadow_trade_plan: Port
    evaluate_economic_gate: Port
    economic_gate_event_payload: Port
    planned_quantity_for_execution: Port
    record_learning_execution_shadow: Port
    planned_exit_for_execution: Port
    strategy_planned_exit_for_execution: Port
    record_planned_exit_checkpoint: Port
    dynamic_exit_enabled: Callable[[], bool]
    journal: EventJournal
    trace_payload: TracePayload
    log_warning: Callable[..., None]
    record_transition: StateTransitionRecorder


class TradePlanStage:
    """Stage 15: build the shadow trade plan, run the initial economic gate, plan the exit."""

    name = "planning_economic_gate_initial"

    def __init__(self, *, outcomes: StageOutcomes, ports: TradePlanPorts) -> None:
        self.outcomes = outcomes
        self.ports = ports

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        ports = self.ports
        symbol_config = evaluation.evaluation_input.symbol_config
        context = evaluation.cycle_context
        intent = required(evaluation.intent, "intent")
        action = intent.action

        evaluation.account_snapshot = ports.portfolio_account_snapshot(context=context)
        evaluation.trade_pipeline_result = ports.record_shadow_trade_plan(
            symbol_config=symbol_config,
            score=evaluation.score,
            intent=intent,
            heavy=evaluation.heavy,
            account_snapshot=evaluation.account_snapshot,
            learning_advisory=evaluation.learning_advisory,
        )
        result = evaluation.trade_pipeline_result
        evaluation.trade_plan_id = result.plan.plan_id if result is not None else None
        evaluation.opportunity = (
            getattr(result, "opportunity", None) if result is not None else None
        )
        ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.TRADE_PLANNED,
            reason="TRADE_PLAN_CREATED",
            stage="TRADE_PLAN",
            action=action,
        )
        if isinstance(evaluation.intent_candidate, IntentCandidate) and result is not None:
            evaluation.planned_trade = PlannedTrade(
                candidate=evaluation.intent_candidate,
                plan=result.plan,
                trade_plan_id=result.plan.plan_id,
                opportunity=evaluation.opportunity,
            )
        gate = ports.evaluate_economic_gate(
            symbol_config=symbol_config,
            action=action,
            trade_pipeline_result=result,
            stage="initial",
        )
        evaluation.economic_gate_decision = gate
        if not gate.allowed:
            ports.journal.append(
                TRADE_BLOCKED_BY_ECONOMIC_GATE,
                **ports.trace_payload(),
                symbol=symbol_config.symbol,
                action=action,
                **ports.economic_gate_event_payload(gate),
                **_ai_signal_audit_fields(
                    evaluation.score,
                    final_action="BLOCKED",
                    block_reason=gate.reason,
                ),
            )
            ports.log_warning(
                "Trade bloqueado pelo Economic Gate | symbol=%s | action=%s | motivo=%s",
                symbol_config.symbol,
                action,
                gate.reason,
            )
            self.outcomes.board.mark_blocked(symbol_config.symbol, gate.reason)
            ports.record_transition(
                evaluation,
                to_state=SymbolOperationalState.BLOCKED,
                reason=gate.reason,
                stage="ECONOMIC_GATE",
                action=action,
            )
            return self.outcomes.terminal(
                evaluation,
                stage="ECONOMIC_GATE",
                classification="economico",
                reason=gate.reason,
                action=action,
            )

        ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.ECONOMIC_APPROVED,
            reason=gate.reason,
            stage="ECONOMIC_GATE",
            action=action,
        )
        evaluation.planned_quantity, evaluation.planned_quantity_reason = (
            ports.planned_quantity_for_execution(result, action=action)
        )
        evaluation.learning_execution_decision = ports.record_learning_execution_shadow(
            trade_pipeline_result=result,
            action=action,
            planned_quantity=evaluation.planned_quantity,
        )
        evaluation.planned_exit, evaluation.planned_exit_reason = ports.planned_exit_for_execution(
            result, action=action
        )
        if ports.dynamic_exit_enabled():
            strategy_exit, strategy_exit_reason = ports.strategy_planned_exit_for_execution(
                score=evaluation.score,
                heavy_snapshot=evaluation.heavy,
                action=action,
            )
            if strategy_exit is not None:
                evaluation.planned_exit = strategy_exit
                evaluation.planned_exit_reason = strategy_exit_reason
        ports.record_planned_exit_checkpoint(
            event_type=PLANNED_EXIT_BEFORE_INTELLIGENT_LAYERS,
            symbol_config=symbol_config,
            action=action,
            trade_pipeline_result=result,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            planned_exit_reason=evaluation.planned_exit_reason,
        )
        ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.TRADE_PLANNED,
            reason=PLANNED_EXIT_BEFORE_INTELLIGENT_LAYERS,
            stage="PLANNED_EXIT",
            action=action,
        )
        return None


@dataclass(frozen=True)
class IntelligentLayersPorts:
    record_order_shadow: Port
    record_order_comparison: Port
    record_exit_shadow: Port
    record_exit_comparison: Port
    evaluate_order_activation: Port
    record_order_activation_result: Port
    evaluate_exit_activation: Port
    record_exit_activation_result: Port
    apply_exit_candidate_if_economic: Port
    record_planned_exit_checkpoint: Port
    record_transition: StateTransitionRecorder


class IntelligentLayersStage:
    """Stage 16: intelligent order and exit layers may adjust quantity and exit."""

    name = "apply_intelligent_layers"

    def __init__(self, ports: IntelligentLayersPorts) -> None:
        self.ports = ports

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        ports = self.ports
        symbol_config = evaluation.evaluation_input.symbol_config
        action = required(evaluation.intent, "intent").action
        result = evaluation.trade_pipeline_result

        evaluation.intelligent_order_plan = ports.record_order_shadow(
            symbol_config=symbol_config,
            action=action,
            trade_pipeline_result=result,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
        )
        ports.record_order_comparison(
            symbol_config=symbol_config,
            action=action,
            trade_pipeline_result=result,
            intelligent_order_plan=evaluation.intelligent_order_plan,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
        )
        evaluation.intelligent_exit_plan = ports.record_exit_shadow(
            symbol_config=symbol_config,
            action=action,
            trade_pipeline_result=result,
            planned_exit=evaluation.planned_exit,
        )
        ports.record_exit_comparison(
            symbol_config=symbol_config,
            action=action,
            trade_pipeline_result=result,
            intelligent_exit_plan=evaluation.intelligent_exit_plan,
            planned_exit=evaluation.planned_exit,
        )
        order_activation = ports.evaluate_order_activation(
            symbol_config=symbol_config,
            action=action,
            intelligent_order_plan=evaluation.intelligent_order_plan,
        )
        evaluation.intelligent_order_activation = order_activation
        if order_activation is not None and order_activation.applied:
            if order_activation.quantity_applied:
                evaluation.planned_quantity = order_activation.final_quantity
                evaluation.planned_quantity_reason = order_activation.reason

            if order_activation.exit_applied:
                candidate_exit = _merge_planned_exit_candidate(
                    evaluation.planned_exit,
                    stop_price=order_activation.final_stop_price,
                    take_profit_price=order_activation.final_take_profit_price,
                )
                evaluation.planned_exit, evaluation.planned_exit_reason = (
                    ports.apply_exit_candidate_if_economic(
                        symbol_config=symbol_config,
                        action=action,
                        trade_pipeline_result=result,
                        planned_quantity=evaluation.planned_quantity,
                        current_planned_exit=evaluation.planned_exit,
                        current_planned_exit_reason=evaluation.planned_exit_reason,
                        candidate_exit=candidate_exit,
                        candidate_reason=order_activation.reason,
                        candidate_source="INTELLIGENT_ORDER",
                        activation_decision=order_activation,
                    )
                )

        ports.record_order_activation_result(
            symbol_config=symbol_config,
            action=action,
            activation_decision=order_activation,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            planned_quantity_reason=evaluation.planned_quantity_reason,
            planned_exit_reason=evaluation.planned_exit_reason,
        )

        exit_activation = ports.evaluate_exit_activation(
            symbol_config=symbol_config,
            action=action,
            intelligent_exit_plan=evaluation.intelligent_exit_plan,
        )
        evaluation.intelligent_exit_activation = exit_activation
        if exit_activation is not None and exit_activation.applied and exit_activation.exit_applied:
            candidate_exit = _merge_planned_exit_candidate(
                evaluation.planned_exit,
                stop_price=exit_activation.final_stop_price,
                take_profit_price=exit_activation.final_take_profit_price,
            )
            evaluation.planned_exit, evaluation.planned_exit_reason = (
                ports.apply_exit_candidate_if_economic(
                    symbol_config=symbol_config,
                    action=action,
                    trade_pipeline_result=result,
                    planned_quantity=evaluation.planned_quantity,
                    current_planned_exit=evaluation.planned_exit,
                    current_planned_exit_reason=evaluation.planned_exit_reason,
                    candidate_exit=candidate_exit,
                    candidate_reason=exit_activation.reason,
                    candidate_source="INTELLIGENT_EXIT",
                    activation_decision=exit_activation,
                )
            )

        ports.record_exit_activation_result(
            symbol_config=symbol_config,
            action=action,
            activation_decision=exit_activation,
            planned_exit=evaluation.planned_exit,
            planned_exit_reason=evaluation.planned_exit_reason,
        )
        ports.record_planned_exit_checkpoint(
            event_type=PLANNED_EXIT_AFTER_INTELLIGENT_LAYERS,
            symbol_config=symbol_config,
            action=action,
            trade_pipeline_result=result,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            planned_exit_reason=evaluation.planned_exit_reason,
        )
        ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.INTELLIGENT_LAYERS_APPLIED,
            reason=PLANNED_EXIT_AFTER_INTELLIGENT_LAYERS,
            stage="INTELLIGENT_LAYERS",
            action=action,
        )
        return None
