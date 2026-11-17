"""Defensive-mode and AI-probability guards (stages 6 and 14).

Before the heavy snapshot the guard checks the score action (defensive mode,
then AI gate). After it, the intent action goes through the regime policy, the
AI gate and the defensive mode, in that order.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from domain.models import FuturesPosition
from futures.trend.regime_policy import evaluate_regime_policy
from governor.evaluation.gates import from_ai_gate, from_defensive_block, from_regime_policy
from governor.evaluation.outcomes import StageOutcomes
from governor.services import AIGateService, DefensiveModeService
from governor.stages.ports import required
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext, SymbolEvaluationResult
from utils.events import EventChannel, observe_event

Phase = Literal["PRE_HEAVY", "POST_HEAVY"]


class DefensiveAndAIGuardStage:
    def __init__(
        self,
        *,
        phase: Phase,
        outcomes: StageOutcomes,
        ai_gate: AIGateService,
        defensive_mode: DefensiveModeService,
        regime_policy_mode: Callable[[], str],
    ) -> None:
        self.phase = phase
        self.name = "run_pre_heavy_guards" if phase == "PRE_HEAVY" else "run_post_heavy_guards"
        self.outcomes = outcomes
        self.ai_gate = ai_gate
        self.defensive_mode = defensive_mode
        self.regime_policy_mode = regime_policy_mode

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        if self.phase == "PRE_HEAVY":
            return self._pre_heavy(evaluation)
        return self._post_heavy(evaluation)

    def _pre_heavy(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        score = required(evaluation.score, "score")
        position = required(evaluation.position, "position")

        blocked = self._defensive_block(
            evaluation,
            position=position,
            action=None,
            decision_stage="PRE_HEAVY_SCORE",
            result_stage="PRE_HEAVY_DEFENSIVE_MODE",
        )
        if blocked is not None:
            return blocked

        gate = self.ai_gate.evaluate(
            symbol_config=symbol_config,
            score=score,
            action=str(score.action or ""),
            position=position,
            stage="PRE_HEAVY_SCORE",
        )
        if gate is not None:
            self.ai_gate.record_gate(symbol_config=symbol_config, score=score, gate=gate)
        if gate is not None and not gate["allowed"]:
            return self.outcomes.gate_blocked(
                evaluation,
                from_ai_gate(gate),
                stage="PRE_HEAVY_AI_PROBABILITY",
                classification="absoluto",
                action=gate["action"],
            )
        return None

    def _post_heavy(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        score = required(evaluation.score, "score")
        action = required(evaluation.intent, "intent").action
        position = required(evaluation.heavy, "heavy").position

        regime_policy = evaluate_regime_policy(
            action=action,
            trend_context=evaluation.trend_context,
            mode=self.regime_policy_mode(),
        )
        evaluation.diagnostics["regime_policy"] = regime_policy.to_dict()
        observe_event(
            "regime_policy_evaluated",
            component=__name__,
            category="risk",
            channel=EventChannel.AUDIT,
            symbol=symbol_config.symbol,
            action=action,
            **regime_policy.to_dict(),
        )
        if not regime_policy.allowed:
            return self.outcomes.gate_blocked(
                evaluation,
                from_regime_policy(regime_policy),
                stage="POST_HEAVY_REGIME_POLICY",
                classification="mercado",
            )

        gate = self.ai_gate.evaluate(
            symbol_config=symbol_config,
            score=score,
            action=action,
            position=position,
            stage="POST_HEAVY_INTENT",
        )
        if gate is not None:
            self.ai_gate.record_gate(symbol_config=symbol_config, score=score, gate=gate)
        if gate is not None and not gate["allowed"]:
            return self.outcomes.gate_blocked(
                evaluation,
                from_ai_gate(gate),
                stage="POST_HEAVY_AI_PROBABILITY",
                classification="absoluto",
                action=action,
            )

        return self._defensive_block(
            evaluation,
            position=position,
            action=action,
            decision_stage="POST_HEAVY_INTENT",
            result_stage="POST_HEAVY_DEFENSIVE_MODE",
        )

    def _defensive_block(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        position: FuturesPosition,
        action: str | None,
        decision_stage: str,
        result_stage: str,
    ) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        score = required(evaluation.score, "score")
        decision = self.defensive_mode.new_entry_block_decision(
            symbol_config=symbol_config,
            score=score,
            position=position,
            context=evaluation.cycle_context,
            action=action,
            stage=decision_stage,
        )
        if decision is None:
            return None
        self.defensive_mode.record_active_block(
            symbol_config=symbol_config,
            score=score,
            decision=decision,
        )
        DefensiveModeService.register_block(context=evaluation.cycle_context, decision=decision)
        return self.outcomes.gate_blocked(
            evaluation,
            from_defensive_block(decision),
            stage=result_stage,
            classification="absoluto",
        )
