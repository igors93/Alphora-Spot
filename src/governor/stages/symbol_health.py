"""Per-symbol health stages: warmup (4, 12) and quarantine (5, 13).

Each stage runs twice: before the heavy snapshot, on the score action, and after
it, on the intent action. Only the pre-heavy warmup and both quarantine runs can
block a new entry; the post-heavy warmup is observational.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.stages.ports import TracePayload, required
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext, SymbolEvaluationResult
from governor.symbol_quarantine import SymbolQuarantine, SymbolQuarantineDecision
from governor.symbol_warmup import SymbolWarmupEvaluator, SymbolWarmupResult

Phase = Literal["PRE_HEAVY", "POST_HEAVY"]


class SymbolWarmupStage:
    """Stages 4 and 12: check that the symbol has observed enough market data."""

    def __init__(
        self,
        *,
        phase: Phase,
        outcomes: StageOutcomes,
        journal: EventJournal,
        trace_payload: TracePayload,
        evaluator: Callable[[], SymbolWarmupEvaluator],
        min_observed_prices: Callable[[], int],
    ) -> None:
        self.phase = phase
        self.name = (
            "run_symbol_warmup_pre_heavy"
            if phase == "PRE_HEAVY"
            else "run_symbol_warmup_post_heavy"
        )
        self.outcomes = outcomes
        self.journal = journal
        self.trace_payload = trace_payload
        self.evaluator = evaluator
        self.min_observed_prices = min_observed_prices

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        if self.phase == "PRE_HEAVY":
            result = self._evaluate(
                evaluation,
                snapshot=evaluation.light,
                action=getattr(evaluation.score, "action", None),
                heavy_snapshot=None,
            )
            if result.blocks_new_entry:
                return self._block(evaluation, result)
            return None

        self._evaluate(
            evaluation,
            snapshot=evaluation.heavy,
            action=getattr(evaluation.intent, "action", None),
            heavy_snapshot=evaluation.heavy,
        )
        return None

    def _evaluate(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        snapshot: object,
        action: str | None,
        heavy_snapshot: object,
    ) -> SymbolWarmupResult:
        evaluator = self.evaluator()
        result = evaluator.evaluate(
            symbol=evaluation.evaluation_input.symbol_config.symbol,
            snapshot=snapshot,
            action=action,
            position_open=required(evaluation.position, "position").is_open,
            min_observed_prices=self.min_observed_prices(),
            heavy_snapshot=heavy_snapshot,
        )
        self.journal.append(
            "symbol_warmup_evaluated",
            **self.trace_payload(),
            stage=self.phase,
            **result.to_dict(),
            execution_changed=False,
            order_mutation=False,
        )
        return result

    def _block(
        self,
        evaluation: SymbolEvaluationContext,
        result: SymbolWarmupResult,
    ) -> SymbolEvaluationResult:
        self.journal.append(
            "symbol_warmup_new_entry_blocked",
            **self.trace_payload(),
            **result.to_dict(),
            execution_changed=False,
            order_mutation=False,
        )
        return self.outcomes.blocked(
            evaluation,
            stage="SYMBOL_WARMUP",
            classification="operacional",
            reason=result.reason,
            action=result.action,
        )


class QuarantineStage:
    """Stages 5 and 13: block new entries of a symbol in cooldown or quarantine."""

    def __init__(
        self,
        *,
        phase: Phase,
        outcomes: StageOutcomes,
        journal: EventJournal,
        trace_payload: TracePayload,
        quarantine: Callable[[], SymbolQuarantine],
    ) -> None:
        self.phase = phase
        self.name = (
            "run_symbol_quarantine_pre_heavy"
            if phase == "PRE_HEAVY"
            else "run_symbol_quarantine_post_heavy"
        )
        self.outcomes = outcomes
        self.journal = journal
        self.trace_payload = trace_payload
        self.quarantine = quarantine

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        if self.phase == "PRE_HEAVY":
            decision = self._evaluate(
                evaluation,
                action=getattr(evaluation.score, "action", None),
                position_open=required(evaluation.position, "position").is_open,
            )
            block_stage = "SYMBOL_QUARANTINE"
        else:
            position = getattr(evaluation.heavy, "position", evaluation.position)
            decision = self._evaluate(
                evaluation,
                action=getattr(evaluation.intent, "action", None),
                position_open=getattr(position, "is_open", False),
            )
            block_stage = "SYMBOL_QUARANTINE_POST_HEAVY"
        if decision.blocks_new_entry:
            return self._block(evaluation, decision, stage=block_stage)
        return None

    def _evaluate(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        action: str | None,
        position_open: bool,
    ) -> SymbolQuarantineDecision:
        decision = self.quarantine().evaluate(
            evaluation.evaluation_input.symbol_config.symbol,
            action=action,
            position_open=position_open,
            now=evaluation.evaluation_input.now,
        )
        self.journal.append(
            "symbol_quarantine_shadow_evaluated",
            **self.trace_payload(),
            stage=self.phase,
            **decision.to_dict(),
            execution_changed=False,
            order_mutation=False,
        )
        return decision

    def _block(
        self,
        evaluation: SymbolEvaluationContext,
        decision: SymbolQuarantineDecision,
        *,
        stage: str,
    ) -> SymbolEvaluationResult:
        self.journal.append(
            "symbol_quarantine_new_entry_blocked",
            **self.trace_payload(),
            **decision.to_dict(),
            execution_changed=False,
            order_mutation=False,
        )
        return self.outcomes.blocked(
            evaluation,
            stage=stage,
            classification="operacional",
            reason=decision.reason,
            action=decision.action,
        )
