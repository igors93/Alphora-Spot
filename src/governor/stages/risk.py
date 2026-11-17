"""Risk stages: final guards (18) and learning execution guards (19).

Stage 18 runs the final economic gate, the order execution policy and the
portfolio risk engine. Stage 19 applies the learning paper/testnet policies and,
when they change the payload, re-runs the final economic gate and portfolio risk
so the approved notional always matches the quantity that reaches execution.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.governor_trade_helpers import (
    INTELLIGENT_FINAL_STATUS_BLOCKED_BY_GOVERNOR,
    INTELLIGENT_FINAL_STATUS_BLOCKED_BY_LEARNING_PAPER,
    INTELLIGENT_FINAL_STATUS_BLOCKED_BY_LEARNING_TESTNET,
    _ai_signal_audit_fields,
    _notional_from_planned_quantity,
)
from governor.stages.ports import StateTransitionRecorder, required
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext, SymbolEvaluationResult
from governor.symbol_state_machine import SymbolOperationalState

Port = Callable[..., Any]


@dataclass(frozen=True)
class RiskPorts:
    """Collaborators shared by the final guards and the learning guards."""

    evaluate_final_economic_gate: Port
    record_final_economic_gate_block: Port
    portfolio_risk_engine: Any
    symbol_configs: Sequence[Any]
    record_finalized: Port
    journal: EventJournal
    log_warning: Callable[..., None]
    record_transition: StateTransitionRecorder


class _RiskStage:
    def __init__(self, *, outcomes: StageOutcomes, ports: RiskPorts) -> None:
        self.outcomes = outcomes
        self.ports = ports

    def _final_economic_gate(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        action: str,
        approved_stage: str,
    ) -> SymbolEvaluationResult | None:
        ports = self.ports
        symbol_config = evaluation.evaluation_input.symbol_config
        validation = ports.evaluate_final_economic_gate(
            symbol_config=symbol_config,
            action=action,
            score=evaluation.score,
            trade_pipeline_result=evaluation.trade_pipeline_result,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
        )
        evaluation.final_economic_validation = validation
        return self._economic_gate_outcome(
            evaluation, validation=validation, action=action, approved_stage=approved_stage
        )

    def _economic_gate_outcome(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        validation: Any,
        action: str,
        approved_stage: str,
    ) -> SymbolEvaluationResult | None:
        ports = self.ports
        if not validation.decision.allowed:
            ports.record_final_economic_gate_block(
                symbol_config=evaluation.evaluation_input.symbol_config,
                action=action,
                score=evaluation.score,
                validation=validation,
            )
            ports.record_transition(
                evaluation,
                to_state=SymbolOperationalState.BLOCKED,
                reason=validation.decision.reason,
                stage="FINAL_ECONOMIC_GATE",
                action=action,
            )
            return self.outcomes.terminal(
                evaluation,
                stage="FINAL_ECONOMIC_GATE",
                classification="economico",
                reason=validation.decision.reason,
                action=action,
                record_flow_decision=False,
            )
        ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.FINAL_ECONOMIC_APPROVED,
            reason=validation.decision.reason,
            stage=approved_stage,
            action=action,
        )
        return None

    def _portfolio_decision(self, evaluation: SymbolEvaluationContext) -> Any:
        intent = required(evaluation.intent, "intent")
        decision = self.ports.portfolio_risk_engine.evaluate_trade_intent(
            intent,
            self.ports.symbol_configs,
            evaluation.account_snapshot,
            override_notional=_notional_from_planned_quantity(
                evaluation.planned_quantity,
                intent,
            ),
        )
        evaluation.portfolio_decision = decision
        return decision

    def _record_finalized(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        action: str,
        final_status: str,
        final_reason: str,
    ) -> None:
        self.ports.record_finalized(
            symbol_config=evaluation.evaluation_input.symbol_config,
            action=action,
            activation_decision=evaluation.intelligent_order_activation,
            governor_decision=evaluation.portfolio_decision,
            final_status=final_status,
            final_reason=final_reason,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            planned_quantity_reason=evaluation.planned_quantity_reason,
            planned_exit_reason=evaluation.planned_exit_reason,
            execution_will_run=False,
        )

    def _blocked(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        reason: str,
        stage: str,
        classification: str,
        action: str,
    ) -> SymbolEvaluationResult:
        self.outcomes.board.mark_blocked(evaluation.evaluation_input.symbol_config.symbol, reason)
        self.ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.BLOCKED,
            reason=reason,
            stage=stage,
            action=action,
        )
        return self.outcomes.terminal(
            evaluation,
            stage=stage,
            classification=classification,
            reason=reason,
            action=action,
        )


class FinalRiskGuardsStage(_RiskStage):
    """Stage 18: final economic gate, order execution policy and portfolio risk."""

    name = "economic_portfolio_pre_execution_inputs"

    def __init__(
        self,
        *,
        outcomes: StageOutcomes,
        ports: RiskPorts,
        select_order_execution_policy: Port,
        strategy_name: Callable[[], str],
        execution_mode: Callable[[], str],
        environment: Callable[[], str],
    ) -> None:
        super().__init__(outcomes=outcomes, ports=ports)
        self.select_order_execution_policy = select_order_execution_policy
        self.strategy_name = strategy_name
        self.execution_mode = execution_mode
        self.environment = environment

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        ports = self.ports
        symbol_config = evaluation.evaluation_input.symbol_config
        intent = required(evaluation.intent, "intent")
        score = required(evaluation.score, "score")
        action = intent.action

        stopped = self._final_economic_gate(
            evaluation, action=action, approved_stage="FINAL_ECONOMIC_GATE"
        )
        if stopped is not None:
            return stopped

        validation = required(evaluation.final_economic_validation, "final_economic_validation")
        order_plan = self.select_order_execution_policy(
            symbol_config=symbol_config,
            action=action,
            score=score,
            trade_pipeline_result=evaluation.trade_pipeline_result,
            economic_gate_decision=validation.decision,
        )
        evaluation.order_execution_plan = order_plan
        if order_plan is not None and not order_plan.allowed:
            return self._blocked(
                evaluation,
                reason=order_plan.reason,
                stage="ORDER_EXECUTION_POLICY",
                classification="execucao",
                action=action,
            )

        decision = self._portfolio_decision(evaluation)
        ports.journal.append(
            "governor_decision",
            symbol=symbol_config.symbol,
            strategy=self.strategy_name(),
            intent_id=intent.intent_id,
            score=score.score,
            priority=required(score.priority, "score.priority").value,
            action=action,
            reason=decision.reason,
            decision=decision,
            execution_mode=self.execution_mode(),
            environment=self.environment(),
            **_ai_signal_audit_fields(
                score,
                final_action=(action if decision.allowed else "BLOCKED"),
                block_reason=(None if decision.allowed else decision.reason),
            ),
        )
        if not decision.allowed:
            self._record_finalized(
                evaluation,
                action=action,
                final_status=INTELLIGENT_FINAL_STATUS_BLOCKED_BY_GOVERNOR,
                final_reason=decision.reason,
            )
            motivo_pt = decision.checks.get("motivo_pt", decision.reason)
            como_corrigir = decision.checks.get("como_corrigir", "")
            ports.log_warning(
                "Trade bloqueado pelo governador | symbol=%s | action=%s | motivo=%s | como_corrigir=%s",
                symbol_config.symbol,
                action,
                motivo_pt,
                como_corrigir,
            )
            ports.journal.append(
                "trade_blocked_by_governor",
                symbol=symbol_config.symbol,
                action=action,
                reason=decision.reason,
                motivo_pt=motivo_pt,
                como_corrigir=como_corrigir,
                acao_de_seguranca=decision.checks.get("acao_de_seguranca", ""),
                bloqueia_novas_entradas=decision.checks.get("bloqueia_novas_entradas", False),
                bloqueia_fechamentos=decision.checks.get("bloqueia_fechamentos", False),
                decision=decision,
                **_ai_signal_audit_fields(
                    score,
                    final_action="BLOCKED",
                    block_reason=decision.reason,
                ),
            )
            return self._blocked(
                evaluation,
                reason=motivo_pt,
                stage="PORTFOLIO_GOVERNOR",
                classification="operacional",
                action=action,
            )
        ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.PORTFOLIO_APPROVED,
            reason=decision.reason,
            stage="PORTFOLIO_GOVERNOR",
            action=action,
        )
        return None


class LearningExecutionStage(_RiskStage):
    """Stage 19: learning paper and testnet policies may block or reduce the entry.

    Kept as one stage (paper, then testnet, then revalidation) so the stage trace
    and the golden replay stay identical; the two policies are separate methods.
    """

    name = "run_learning_execution_guards"

    def __init__(
        self,
        *,
        outcomes: StageOutcomes,
        ports: RiskPorts,
        evaluate_paper: Port,
        record_testnet_observation: Port,
        evaluate_testnet: Port,
        dynamic_sizing_enabled: Callable[[], bool],
    ) -> None:
        super().__init__(outcomes=outcomes, ports=ports)
        self.evaluate_paper = evaluate_paper
        self.record_testnet_observation = record_testnet_observation
        self.evaluate_testnet = evaluate_testnet
        self.dynamic_sizing_enabled = dynamic_sizing_enabled

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        action = required(evaluation.intent, "intent").action
        learning_decision = evaluation.learning_execution_decision

        evaluation.learning_paper_execution = self.evaluate_paper(
            learning_execution_decision=learning_decision,
        )
        self.record_testnet_observation(learning_execution_decision=learning_decision)
        evaluation.learning_testnet_execution = self.evaluate_testnet(
            learning_execution_decision=learning_decision,
        )
        evaluation.learning_changed_execution_payload = False

        stopped = self._apply_paper(evaluation, action=action)
        if stopped is not None:
            return stopped
        stopped = self._apply_testnet(evaluation, action=action)
        if stopped is not None:
            return stopped
        if evaluation.learning_changed_execution_payload:
            return self._revalidate(evaluation, action=action)
        return None

    def _apply_paper(
        self, evaluation: SymbolEvaluationContext, *, action: str
    ) -> SymbolEvaluationResult | None:
        ports = self.ports
        symbol = evaluation.evaluation_input.symbol_config.symbol
        paper = evaluation.learning_paper_execution
        if paper is None:
            return None
        if paper.blocked:
            self._record_finalized(
                evaluation,
                action=action,
                final_status=INTELLIGENT_FINAL_STATUS_BLOCKED_BY_LEARNING_PAPER,
                final_reason=paper.reason,
            )
            ports.journal.append(
                "trade_blocked_by_learning_paper",
                symbol=symbol,
                action=action,
                reason=paper.reason,
                plan_id=paper.plan_id,
                original_quantity=paper.original_quantity,
                final_quantity=paper.final_quantity,
                paper_execution=paper,
                diagnostics=paper.diagnostics,
                **_ai_signal_audit_fields(
                    evaluation.score,
                    final_action="BLOCKED",
                    block_reason=paper.reason,
                ),
            )
            return self._blocked(
                evaluation,
                reason=paper.reason,
                stage="LEARNING_PAPER",
                classification="ajuste",
                action=action,
            )
        if paper.reduced and paper.final_quantity is not None:
            ports.journal.append(
                "trade_reduced_by_learning_paper",
                symbol=symbol,
                action=action,
                reason=paper.reason,
                plan_id=paper.plan_id,
                original_quantity=paper.original_quantity,
                final_quantity=paper.final_quantity,
                paper_execution=paper,
                diagnostics=paper.diagnostics,
            )
            evaluation.planned_quantity = paper.final_quantity
            evaluation.planned_quantity_reason = paper.reason
            evaluation.learning_changed_execution_payload = True
            ports.record_transition(
                evaluation,
                to_state=SymbolOperationalState.LEARNING_APPLIED,
                reason=paper.reason,
                stage="LEARNING_PAPER",
                action=action,
            )
        return None

    def _apply_testnet(
        self, evaluation: SymbolEvaluationContext, *, action: str
    ) -> SymbolEvaluationResult | None:
        ports = self.ports
        symbol = evaluation.evaluation_input.symbol_config.symbol
        testnet = evaluation.learning_testnet_execution
        if testnet is None:
            return None
        if testnet.blocked:
            self._record_finalized(
                evaluation,
                action=action,
                final_status=INTELLIGENT_FINAL_STATUS_BLOCKED_BY_LEARNING_TESTNET,
                final_reason=testnet.reason,
            )
            ports.journal.append(
                "trade_blocked_by_learning_testnet",
                symbol=symbol,
                action=action,
                reason=testnet.reason,
                plan_id=testnet.plan_id,
                original_quantity=testnet.original_quantity,
                final_quantity=testnet.final_quantity,
                testnet_execution=testnet,
                diagnostics=testnet.diagnostics,
                **_ai_signal_audit_fields(
                    evaluation.score,
                    final_action="BLOCKED",
                    block_reason=testnet.reason,
                ),
            )
            return self._blocked(
                evaluation,
                reason=testnet.reason,
                stage="LEARNING_TESTNET",
                classification="ajuste",
                action=action,
            )
        if testnet.reduced and testnet.final_quantity is not None:
            if not self.dynamic_sizing_enabled():
                ports.journal.append(
                    "learning_testnet_reduction_ignored",
                    symbol=symbol,
                    action=action,
                    reason="DYNAMIC_SIZING_DISABLED",
                    plan_id=testnet.plan_id,
                    original_quantity=testnet.original_quantity,
                    final_quantity=testnet.final_quantity,
                    testnet_execution=testnet,
                    diagnostics={
                        **testnet.diagnostics,
                        "safety_note": (
                            "Reducao do learning em TESTNET_REDUCE foi calculada, "
                            "mas nao foi aplicada porque dynamic_sizing_enabled=false."
                        ),
                    },
                )
            else:
                ports.journal.append(
                    "trade_reduced_by_learning_testnet",
                    symbol=symbol,
                    action=action,
                    reason=testnet.reason,
                    plan_id=testnet.plan_id,
                    original_quantity=testnet.original_quantity,
                    final_quantity=testnet.final_quantity,
                    testnet_execution=testnet,
                    diagnostics=testnet.diagnostics,
                )
                evaluation.planned_quantity = testnet.final_quantity
                evaluation.planned_quantity_reason = testnet.reason
                evaluation.learning_changed_execution_payload = True
                ports.record_transition(
                    evaluation,
                    to_state=SymbolOperationalState.LEARNING_APPLIED,
                    reason=testnet.reason,
                    stage="LEARNING_TESTNET",
                    action=action,
                )
        return None

    def _revalidate(
        self, evaluation: SymbolEvaluationContext, *, action: str
    ) -> SymbolEvaluationResult | None:
        ports = self.ports
        symbol = evaluation.evaluation_input.symbol_config.symbol
        validation = ports.evaluate_final_economic_gate(
            symbol_config=evaluation.evaluation_input.symbol_config,
            action=action,
            score=evaluation.score,
            trade_pipeline_result=evaluation.trade_pipeline_result,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
        )
        evaluation.final_economic_validation = validation
        evaluation.learning_payload_revalidated = True
        stopped = self._economic_gate_outcome(
            evaluation,
            validation=validation,
            action=action,
            approved_stage="FINAL_ECONOMIC_GATE_AFTER_LEARNING",
        )
        if stopped is not None:
            return stopped
        # Re-evaluate portfolio risk with the updated notional so the approved
        # notional always matches the quantity that will reach execution.
        decision = self._portfolio_decision(evaluation)
        if not decision.allowed:
            motivo_pt = decision.checks.get("motivo_pt", decision.reason)
            ports.log_warning(
                "Trade bloqueado pelo governador após revalidação do learning | "
                "symbol=%s | action=%s | motivo=%s",
                symbol,
                action,
                motivo_pt,
            )
            ports.journal.append(
                "trade_blocked_by_governor_after_learning_revalidation",
                symbol=symbol,
                action=action,
                reason=decision.reason,
                motivo_pt=motivo_pt,
                decision=decision,
            )
            return self._blocked(
                evaluation,
                reason=motivo_pt,
                stage="PORTFOLIO_GOVERNOR_AFTER_LEARNING",
                classification="operacional",
                action=action,
            )
        ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.PORTFOLIO_APPROVED,
            reason=decision.reason,
            stage="PORTFOLIO_GOVERNOR_AFTER_LEARNING",
            action=action,
        )
        return None
