"""Light-phase stages that only read the light evaluation (stages 2, 3, 7 and 8)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from domain.models import FuturesPosition
from governor.cycle_context import CycleContext
from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.evaluation.runtime_board import StrategyNameSource
from governor.events import ATTENTION_LIGHT_ONLY
from governor.governor_trade_helpers import _ai_signal_audit_fields
from governor.models import SymbolRuntimeStatus
from governor.request_budget import RequestBudgetDecision
from governor.stages.ports import StateTransitionRecorder, TracePayload, required
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext, SymbolEvaluationResult
from governor.symbol_state_machine import SymbolOperationalState
from scanner.signal_score import SignalScore


class ManualControlStage:
    """Stage 2: stop when the symbol needs manual reconciliation or protections."""

    name = "handle_manual_or_protection_status"

    def __init__(
        self,
        *,
        outcomes: StageOutcomes,
        record_transition: StateTransitionRecorder,
    ) -> None:
        self.outcomes = outcomes
        self.record_transition = record_transition

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        if evaluation.status is SymbolRuntimeStatus.MANUAL_RECONCILE_REQUIRED:
            self.record_transition(
                evaluation,
                to_state=SymbolOperationalState.MANUAL_RECONCILE_REQUIRED,
                reason=SymbolRuntimeStatus.MANUAL_RECONCILE_REQUIRED.value,
                stage="MANUAL_RECONCILE",
            )
            return self.outcomes.terminal(
                evaluation,
                stage="MANUAL_RECONCILE",
                classification="absoluto",
                reason=SymbolRuntimeStatus.MANUAL_RECONCILE_REQUIRED.value,
            )

        if evaluation.status in {
            SymbolRuntimeStatus.PROTECTIONS_PENDING,
            SymbolRuntimeStatus.POSITION_OPEN_UNPROTECTED,
        }:
            status = required(evaluation.status, "status")
            self.record_transition(
                evaluation,
                to_state=SymbolOperationalState.PROTECTIONS_PENDING,
                reason=status.value,
                stage="PROTECTION_STATUS",
            )
            return self.outcomes.terminal(
                evaluation,
                stage="PROTECTION_STATUS",
                classification="operacional",
                reason=status.value,
            )
        return None


class WarmupStage:
    """Stage 3: a flat symbol whose strategy is still warming up stops here."""

    name = "handle_warmup"

    def __init__(self, *, outcomes: StageOutcomes) -> None:
        self.outcomes = outcomes

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        if evaluation.warmup and not required(evaluation.position, "position").is_open:
            return self.outcomes.terminal(
                evaluation,
                stage="WARMUP",
                classification="observacao",
                reason=required(evaluation.score, "score").reason,
            )
        return None


class LightOnlyStage:
    """Stage 7: decide whether the symbol needs the heavy snapshot at all."""

    name = "handle_light_only"

    def __init__(
        self,
        *,
        outcomes: StageOutcomes,
        needs_heavy_check: Callable[[SignalScore, FuturesPosition], bool],
        journal: EventJournal,
        trace_payload: TracePayload,
        strategy: StrategyNameSource,
        record_transition: StateTransitionRecorder,
    ) -> None:
        self.outcomes = outcomes
        self.needs_heavy_check = needs_heavy_check
        self.journal = journal
        self.trace_payload = trace_payload
        self.strategy = strategy
        self.record_transition = record_transition

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        score = required(evaluation.score, "score")
        if not self.needs_heavy_check(score, required(evaluation.position, "position")):
            self.journal.append(
                ATTENTION_LIGHT_ONLY,
                **self.trace_payload(),
                symbol=evaluation.evaluation_input.symbol_config.symbol,
                strategy=self.strategy.global_strategy_name,
                score=score.score,
                priority=required(score.priority, "score.priority").value,
                action=score.action,
                reason=score.reason,
                next_check_at=required(evaluation.scheduled, "scheduled").next_check_at,
                **_ai_signal_audit_fields(score, final_action="NO_ACTION"),
            )
            return self.outcomes.terminal(
                evaluation,
                stage="LIGHT_ONLY",
                classification="observacao",
                reason=score.reason,
            )
        self.record_transition(
            evaluation,
            to_state=SymbolOperationalState.HEAVY_REQUIRED,
            reason="HEAVY_CHECK_REQUIRED",
            stage="LIGHT_ONLY",
        )
        return None


class HeavyCheckBudget(Protocol):
    """Consumes one heavy check from the cycle request budget (``None`` = disabled)."""

    def __call__(
        self,
        *,
        symbol_config: object,
        score: SignalScore,
        position: FuturesPosition,
        context: CycleContext | None,
    ) -> RequestBudgetDecision | None: ...


class RequestBudgetStage:
    """Stage 8: spend the cycle's heavy-check budget or degrade to no action."""

    name = "run_request_budget_guard"

    def __init__(self, *, outcomes: StageOutcomes, consume_budget: HeavyCheckBudget) -> None:
        self.outcomes = outcomes
        self.consume_budget = consume_budget

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        evaluation.budget_decision = self.consume_budget(
            symbol_config=symbol_config,
            score=required(evaluation.score, "score"),
            position=required(evaluation.position, "position"),
            context=evaluation.cycle_context,
        )
        if evaluation.budget_decision is not None and not evaluation.budget_decision.allowed:
            reason = evaluation.budget_decision.reason
            self.outcomes.board.mark_no_action(symbol_config.symbol, reason)
            return self.outcomes.terminal(
                evaluation,
                stage="REQUEST_BUDGET",
                classification="degradacao",
                reason=reason,
            )
        return None
