from pathlib import Path
from typing import Any

from trading.learning import OUTCOME_EVENT_TYPE
from trading.learning_activation_criteria import (
    LearningActivationCriteriaEvaluation,
    LearningActivationCriteriaEvaluator,
)
from trading.learning_paper_activation import (
    LearningPaperActivationEvaluation,
    LearningPaperActivationGate,
    LearningPaperActivationSettings,
)
from trading.learning_shadow_confidence import (
    LearningShadowConfidenceEvaluation,
    LearningShadowConfidenceEvaluator,
)
from trading.learning_shadow_impact import (
    LEARNING_EXECUTION_SHADOW_EVENT_TYPE,
    LearningShadowImpactAnalyzer,
    LearningShadowImpactSummary,
)
from utils.jsonl import parse_json_object_line
from utils.trade_journal import TradeJournal

LEARNING_SHADOW_IMPACT_REPORT_EVENT_TYPE = "learning_shadow_impact_report_created"


class LearningShadowImpactReporter:
    def __init__(
        self,
        *,
        source_journal_path: str | Path,
        output_journal: TradeJournal,
        analyzer: LearningShadowImpactAnalyzer | None = None,
        confidence_evaluator: LearningShadowConfidenceEvaluator | None = None,
        activation_criteria_evaluator: LearningActivationCriteriaEvaluator | None = None,
        paper_activation_settings: LearningPaperActivationSettings | None = None,
    ):
        self.source_journal_path = Path(source_journal_path)
        self.output_journal = output_journal
        self.analyzer = analyzer or LearningShadowImpactAnalyzer()
        self.confidence_evaluator = confidence_evaluator or LearningShadowConfidenceEvaluator()
        self.activation_criteria_evaluator = (
            activation_criteria_evaluator or LearningActivationCriteriaEvaluator()
        )
        self.paper_activation_gate = LearningPaperActivationGate(
            settings=paper_activation_settings,
        )
        self._impact_cursor_offset = 0
        self._known_total_shadow_decisions = 0
        self._shadow_plan_counts: dict[str, int] = {}
        self._outcome_plan_ids: set[str] = set()

    def build_summary(self) -> LearningShadowImpactSummary:
        return self.analyzer.analyze_journal(self.source_journal_path)

    def build_confidence(
        self,
        summary: LearningShadowImpactSummary,
    ) -> LearningShadowConfidenceEvaluation:
        return self.confidence_evaluator.evaluate(summary)

    def build_activation_criteria(
        self,
        *,
        summary: LearningShadowImpactSummary,
        confidence: LearningShadowConfidenceEvaluation,
    ) -> LearningActivationCriteriaEvaluation:
        return self.activation_criteria_evaluator.evaluate(
            summary=summary,
            confidence=confidence,
        )

    def build_paper_activation(
        self,
        activation: LearningActivationCriteriaEvaluation,
    ) -> LearningPaperActivationEvaluation:
        return self.paper_activation_gate.evaluate(activation)

    def record_summary(self, summary: LearningShadowImpactSummary) -> None:
        confidence = self.build_confidence(summary)
        activation = self.build_activation_criteria(
            summary=summary,
            confidence=confidence,
        )
        paper_activation = self.build_paper_activation(activation)

        self.output_journal.append(
            LEARNING_SHADOW_IMPACT_REPORT_EVENT_TYPE,
            total_shadow_decisions=summary.total_shadow_decisions,
            matched_outcomes=summary.matched_outcomes,
            unmatched_shadow_decisions=summary.unmatched_shadow_decisions,
            allow_count=summary.allow_count,
            would_block_count=summary.would_block_count,
            would_reduce_size_count=summary.would_reduce_size_count,
            block_would_help_count=summary.block_would_help_count,
            block_would_hurt_count=summary.block_would_hurt_count,
            block_neutral_count=summary.block_neutral_count,
            reduce_size_would_help_count=summary.reduce_size_would_help_count,
            reduce_size_would_hurt_count=summary.reduce_size_would_hurt_count,
            reduce_size_neutral_count=summary.reduce_size_neutral_count,
            estimated_loss_avoided_by_blocks=summary.estimated_loss_avoided_by_blocks,
            estimated_profit_missed_by_blocks=summary.estimated_profit_missed_by_blocks,
            estimated_block_net_impact=summary.estimated_block_net_impact,
            estimated_reduce_size_net_impact=summary.estimated_reduce_size_net_impact,
            estimated_total_shadow_impact=summary.estimated_total_shadow_impact,
            matched_trades_count=len(summary.matched_trades),
            confidence_status=confidence.status,
            confidence_grade=confidence.grade,
            confidence_score=confidence.confidence_score,
            activation_readiness=confidence.activation_readiness,
            recommended_action=confidence.recommended_action,
            activation_status=activation.status,
            activation_recommended_action=activation.recommended_action,
            eligible_for_paper_reduction=activation.eligible_for_paper_reduction,
            eligible_for_paper_block=activation.eligible_for_paper_block,
            paper_activation_status=paper_activation.status,
            paper_activation_recommended_action=paper_activation.recommended_action,
            paper_activation_enabled=paper_activation.paper_activation_enabled,
            paper_reduction_enabled=paper_activation.paper_reduction_enabled,
            paper_block_enabled=paper_activation.paper_block_enabled,
            can_apply_reduction_in_paper=paper_activation.can_apply_reduction_in_paper,
            can_apply_block_in_paper=paper_activation.can_apply_block_in_paper,
            can_activate_testnet=paper_activation.can_activate_testnet,
            can_activate_real=paper_activation.can_activate_real,
            can_use_for_execution=paper_activation.can_use_for_execution,
            shadow_only=True,
            paper_only=paper_activation.paper_only,
            confidence=confidence,
            activation_criteria=activation,
            paper_activation=paper_activation,
            matched_trades=summary.matched_trades,
            summary=summary,
            diagnostics={
                **summary.diagnostics,
                "confidence_id": confidence.confidence_id,
                "confidence_version": confidence.version,
                "activation_id": activation.activation_id,
                "activation_version": activation.version,
                "paper_activation_id": paper_activation.activation_id,
                "paper_activation_version": paper_activation.version,
            },
        )

    def record_if_changed(
        self,
        *,
        previous_total_shadow_decisions: int,
        previous_matched_outcomes: int,
    ) -> LearningShadowImpactSummary | None:
        total_shadow_decisions, matched_outcomes = self._count_new_impact_inputs()
        if total_shadow_decisions <= 0:
            return None

        if (
            total_shadow_decisions == previous_total_shadow_decisions
            and matched_outcomes == previous_matched_outcomes
        ):
            return None

        summary = self.build_summary()

        if summary.total_shadow_decisions <= 0:
            return None

        if (
            summary.total_shadow_decisions == previous_total_shadow_decisions
            and summary.matched_outcomes == previous_matched_outcomes
        ):
            return None

        self.record_summary(summary)
        return summary

    def _count_new_impact_inputs(self) -> tuple[int, int]:
        journal_path = self.source_journal_path
        if not journal_path.exists():
            self._impact_cursor_offset = 0
            self._known_total_shadow_decisions = 0
            self._shadow_plan_counts.clear()
            self._outcome_plan_ids.clear()
            return (0, 0)

        file_size = journal_path.stat().st_size
        if self._impact_cursor_offset > file_size:
            self._impact_cursor_offset = 0
            self._known_total_shadow_decisions = 0
            self._shadow_plan_counts.clear()
            self._outcome_plan_ids.clear()

        with journal_path.open("r", encoding="utf-8") as file:
            file.seek(self._impact_cursor_offset)
            while True:
                line = file.readline()
                if not line:
                    break
                event = parse_json_object_line(line.strip())
                if event is None:
                    continue

                shadow = _extract_shadow_payload(event)
                if shadow is not None:
                    self._known_total_shadow_decisions += 1
                    plan_id = str(shadow.get("plan_id") or "")
                    if plan_id:
                        self._shadow_plan_counts[plan_id] = (
                            self._shadow_plan_counts.get(plan_id, 0) + 1
                        )
                    continue

                outcome = _extract_outcome_payload(event)
                if outcome is None:
                    continue

                trade_plan_id = outcome.get("trade_plan_id")
                if trade_plan_id:
                    self._outcome_plan_ids.add(str(trade_plan_id))
            self._impact_cursor_offset = file.tell()

        matched_outcomes = sum(
            count
            for plan_id, count in self._shadow_plan_counts.items()
            if plan_id in self._outcome_plan_ids
        )
        return (self._known_total_shadow_decisions, matched_outcomes)


def count_learning_shadow_impact_inputs(path: str | Path) -> tuple[int, int]:
    journal_path = Path(path)
    if not journal_path.exists():
        return (0, 0)

    shadows: list[dict[str, Any]] = []
    outcomes_by_plan_id: dict[str, dict[str, Any]] = {}
    with journal_path.open("r", encoding="utf-8") as file:
        for line in file:
            event = parse_json_object_line(line.strip())
            if event is None:
                continue

            shadow = _extract_shadow_payload(event)
            if shadow is not None:
                shadows.append(shadow)
                continue

            outcome = _extract_outcome_payload(event)
            if outcome is None:
                continue
            trade_plan_id = outcome.get("trade_plan_id")
            if trade_plan_id:
                outcomes_by_plan_id[str(trade_plan_id)] = outcome

    matched_outcomes = 0
    for shadow in shadows:
        plan_id = str(shadow.get("plan_id") or "")
        if plan_id and plan_id in outcomes_by_plan_id:
            matched_outcomes += 1

    return (len(shadows), matched_outcomes)


def load_latest_learning_shadow_impact_report(path: str | Path) -> dict[str, Any] | None:
    journal_path = Path(path)
    if not journal_path.exists():
        return None

    latest: dict[str, Any] | None = None
    with journal_path.open("r", encoding="utf-8") as file:
        for line in file:
            event = parse_json_object_line(line.strip())
            if event is None:
                continue

            if event.get("event_type") != LEARNING_SHADOW_IMPACT_REPORT_EVENT_TYPE:
                continue

            data = event.get("data")
            if isinstance(data, dict):
                latest = data

    return latest


def _extract_shadow_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("event_type") != LEARNING_EXECUTION_SHADOW_EVENT_TYPE:
        return None

    data = event.get("data")
    return data if isinstance(data, dict) else None


def _extract_outcome_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("event_type") != OUTCOME_EVENT_TYPE:
        return None

    data = event.get("data")
    if not isinstance(data, dict):
        return None

    outcome = data.get("outcome")
    if isinstance(outcome, dict):
        return outcome

    if data.get("trade_plan_id") is not None or data.get("outcome_status") is not None:
        return data

    return None
