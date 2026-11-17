from decimal import Decimal

from trading.learning_activation_criteria import (
    ACTIVATION_STATUS_NOT_READY,
    ACTIVATION_STATUS_OBSERVE_MORE,
    ACTIVATION_STATUS_PAPER_BLOCK_ELIGIBLE,
    ACTIVATION_STATUS_PAPER_REDUCTION_ELIGIBLE,
    LearningActivationCriteriaEvaluator,
    format_learning_activation_criteria,
)
from trading.learning_shadow_confidence import LearningShadowConfidenceEvaluator
from trading.learning_shadow_impact import LearningShadowImpactSummary


def _summary(
    *,
    total_shadow_decisions=30,
    matched_outcomes=30,
    would_block_count=0,
    would_reduce_size_count=30,
    block_would_help_count=0,
    block_would_hurt_count=0,
    reduce_size_would_help_count=20,
    reduce_size_would_hurt_count=3,
    estimated_total_shadow_impact=Decimal("10"),
):
    return LearningShadowImpactSummary(
        total_shadow_decisions=total_shadow_decisions,
        matched_outcomes=matched_outcomes,
        unmatched_shadow_decisions=total_shadow_decisions - matched_outcomes,
        allow_count=0,
        would_block_count=would_block_count,
        would_reduce_size_count=would_reduce_size_count,
        block_would_help_count=block_would_help_count,
        block_would_hurt_count=block_would_hurt_count,
        block_neutral_count=0,
        reduce_size_would_help_count=reduce_size_would_help_count,
        reduce_size_would_hurt_count=reduce_size_would_hurt_count,
        reduce_size_neutral_count=0,
        estimated_loss_avoided_by_blocks=Decimal("0"),
        estimated_profit_missed_by_blocks=Decimal("0"),
        estimated_block_net_impact=Decimal("0"),
        estimated_reduce_size_net_impact=estimated_total_shadow_impact,
        estimated_total_shadow_impact=estimated_total_shadow_impact,
    )


def _evaluate(summary):
    confidence = LearningShadowConfidenceEvaluator().evaluate(summary)
    return LearningActivationCriteriaEvaluator().evaluate(
        summary=summary,
        confidence=confidence,
    )


def test_activation_criteria_is_not_ready_with_small_sample():
    evaluation = _evaluate(
        _summary(
            total_shadow_decisions=5,
            matched_outcomes=5,
            would_reduce_size_count=5,
            reduce_size_would_help_count=4,
            reduce_size_would_hurt_count=0,
            estimated_total_shadow_impact=Decimal("5"),
        )
    )

    assert evaluation.status == ACTIVATION_STATUS_OBSERVE_MORE
    assert evaluation.eligible_for_paper_reduction is False
    assert evaluation.eligible_for_paper_block is False
    assert evaluation.can_activate_testnet is False
    assert evaluation.can_activate_real is False
    assert evaluation.can_use_for_execution is False


def test_activation_criteria_marks_paper_reduction_eligible():
    evaluation = _evaluate(
        _summary(
            total_shadow_decisions=30,
            matched_outcomes=30,
            would_reduce_size_count=30,
            reduce_size_would_help_count=20,
            reduce_size_would_hurt_count=3,
            estimated_total_shadow_impact=Decimal("10"),
        )
    )

    assert evaluation.status == ACTIVATION_STATUS_PAPER_REDUCTION_ELIGIBLE
    assert evaluation.eligible_for_paper_reduction is True
    assert evaluation.eligible_for_paper_block is False
    assert evaluation.can_use_for_execution is False


def test_activation_criteria_marks_paper_block_eligible_only_when_strong():
    evaluation = _evaluate(
        _summary(
            total_shadow_decisions=60,
            matched_outcomes=60,
            would_block_count=60,
            would_reduce_size_count=0,
            block_would_help_count=50,
            block_would_hurt_count=5,
            estimated_total_shadow_impact=Decimal("25"),
        )
    )

    assert evaluation.status == ACTIVATION_STATUS_PAPER_BLOCK_ELIGIBLE
    assert evaluation.eligible_for_paper_reduction is True
    assert evaluation.eligible_for_paper_block is True
    assert evaluation.can_use_for_execution is False


def test_activation_criteria_blocks_negative_impact():
    evaluation = _evaluate(
        _summary(
            total_shadow_decisions=30,
            matched_outcomes=30,
            would_reduce_size_count=30,
            reduce_size_would_help_count=5,
            reduce_size_would_hurt_count=20,
            estimated_total_shadow_impact=Decimal("-2"),
        )
    )

    assert evaluation.status == ACTIVATION_STATUS_NOT_READY
    assert evaluation.eligible_for_paper_reduction is False
    assert evaluation.eligible_for_paper_block is False


def test_activation_criteria_requires_minimum_coverage():
    summary = _summary(
        total_shadow_decisions=100,
        matched_outcomes=30,
        would_reduce_size_count=30,
        reduce_size_would_help_count=20,
        reduce_size_would_hurt_count=3,
        estimated_total_shadow_impact=Decimal("10"),
    )
    confidence = LearningShadowConfidenceEvaluator().evaluate(summary)
    evaluation = LearningActivationCriteriaEvaluator().evaluate(
        summary=summary,
        confidence=confidence,
    )

    assert evaluation.status == ACTIVATION_STATUS_OBSERVE_MORE
    assert evaluation.eligible_for_paper_reduction is False
    assert any(check.name == "coverage_rate" and not check.passed for check in evaluation.checks)


def test_activation_criteria_formatter_outputs_readable_text():
    evaluation = _evaluate(
        _summary(
            total_shadow_decisions=30,
            matched_outcomes=30,
            would_reduce_size_count=30,
            reduce_size_would_help_count=20,
            reduce_size_would_hurt_count=3,
            estimated_total_shadow_impact=Decimal("10"),
        )
    )

    text = format_learning_activation_criteria(evaluation)

    assert "Learning activation criteria:" in text
    assert "Eligible for paper reduction: True" in text
    assert "Can use for execution: False" in text
