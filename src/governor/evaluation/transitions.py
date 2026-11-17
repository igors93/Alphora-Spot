"""Observational symbol state transitions recorded while a symbol is evaluated."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from governor.symbol_evaluation_pipeline import SymbolEvaluationContext
from governor.symbol_state_machine import SymbolOperationalState, SymbolStateTracker

TracePayload = Callable[[], Mapping[str, str]]


def action_from_evaluation(evaluation: SymbolEvaluationContext) -> str | None:
    """The intent's action when there is one, otherwise the score's action."""

    action = getattr(evaluation.intent, "action", None)
    if action:
        return str(action)
    score_action = getattr(evaluation.score, "action", None)
    return str(score_action) if score_action else None


class SymbolTransitionRecorder:
    """Record a transition of the observational symbol state machine.

    Satisfies the ``StateTransitionRecorder`` port of the stages. The tracker is
    long-lived (one per Governor); the journal and logger are the Governor's
    current ones, so a journal replaced by a test or the golden harness is honored.
    """

    def __init__(
        self,
        *,
        tracker: SymbolStateTracker,
        journal: Any,
        logger: Any,
        trace_payload: TracePayload,
    ) -> None:
        self.tracker = tracker
        self.journal = journal
        self.logger = logger
        self.trace_payload = trace_payload

    def __call__(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        to_state: SymbolOperationalState | str,
        reason: str,
        stage: str,
        action: str | None = None,
        trade_plan_id: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        tracker = self.tracker
        tracker.journal = self.journal
        tracker.logger = self.logger

        symbol_config = evaluation.evaluation_input.symbol_config
        trace_payload = self.trace_payload()
        transition_diagnostics = dict(diagnostics or {})
        previous_observational_state = tracker.state_for(symbol_config.symbol)
        if (
            str(getattr(to_state, "value", to_state))
            == SymbolOperationalState.LIGHT_EVALUATED.value
        ):
            transition_diagnostics.update(
                {
                    "evaluation_cycle_reset": True,
                    "previous_observational_state": previous_observational_state,
                    "observational_only": True,
                }
            )
        tracker.transition(
            symbol=symbol_config.symbol,
            to_state=to_state,
            reason=reason,
            stage=stage,
            cycle_id=(evaluation.evaluation_input.cycle_id or trace_payload.get("cycle_id", "")),
            symbol_eval_id=(
                evaluation.evaluation_input.symbol_eval_id
                or trace_payload.get("symbol_eval_id", "")
            ),
            trade_plan_id=trade_plan_id or evaluation.trade_plan_id,
            action=action or action_from_evaluation(evaluation),
            diagnostics=transition_diagnostics,
            journal=self.journal,
        )
