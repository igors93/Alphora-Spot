from decimal import Decimal

from trading.learning_shadow_confidence import (
    CONFIDENCE_STATUS_INSUFFICIENT_DATA,
    CONFIDENCE_STATUS_NEGATIVE_IMPACT,
    CONFIDENCE_STATUS_PROMISING,
    CONFIDENCE_STATUS_STRONG,
    READINESS_NOT_READY,
    READINESS_PAPER_BLOCK_CANDIDATE,
    READINESS_PAPER_REDUCTION_CANDIDATE,
    LearningShadowConfidenceEvaluator,
    format_learning_shadow_confidence,
)
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


def test_learning_shadow_confidence_requires_minimum_sample():
    evaluation = LearningShadowConfidenceEvaluator().evaluate(
        _summary(
            total_shadow_decisions=5,
            matched_outcomes=5,
            would_reduce_size_count=5,
            reduce_size_would_help_count=4,
            reduce_size_would_hurt_count=0,
            estimated_total_shadow_impact=Decimal("5"),
        )
    )

    assert evaluation.status == CONFIDENCE_STATUS_INSUFFICIENT_DATA
    assert evaluation.activation_readiness != READINESS_PAPER_REDUCTION_CANDIDATE
    assert evaluation.can_use_for_execution is False


def test_learning_shadow_confidence_rejects_negative_impact():
    evaluation = LearningShadowConfidenceEvaluator().evaluate(
        _summary(
            estimated_total_shadow_impact=Decimal("-2"),
            reduce_size_would_help_count=5,
            reduce_size_would_hurt_count=20,
        )
    )

    assert evaluation.status == CONFIDENCE_STATUS_NEGATIVE_IMPACT
    assert evaluation.activation_readiness == READINESS_NOT_READY
    assert evaluation.can_use_for_execution is False


def test_learning_shadow_confidence_marks_promising_signal():
    evaluation = LearningShadowConfidenceEvaluator().evaluate(
        _summary(
            total_shadow_decisions=30,
            matched_outcomes=30,
            would_reduce_size_count=30,
            reduce_size_would_help_count=20,
            reduce_size_would_hurt_count=3,
            estimated_total_shadow_impact=Decimal("10"),
        )
    )

    assert evaluation.status == CONFIDENCE_STATUS_PROMISING
    assert evaluation.activation_readiness == READINESS_PAPER_REDUCTION_CANDIDATE
    assert evaluation.confidence_score == 65
    assert evaluation.can_use_for_execution is False


def test_learning_shadow_confidence_marks_strong_block_candidate():
    evaluation = LearningShadowConfidenceEvaluator().evaluate(
        _summary(
            total_shadow_decisions=60,
            matched_outcomes=60,
            would_block_count=60,
            would_reduce_size_count=0,
            block_would_help_count=50,
            block_would_hurt_count=5,
            reduce_size_would_help_count=0,
            reduce_size_would_hurt_count=0,
            estimated_total_shadow_impact=Decimal("25"),
        )
    )

    assert evaluation.status == CONFIDENCE_STATUS_STRONG
    assert evaluation.activation_readiness == READINESS_PAPER_BLOCK_CANDIDATE
    assert evaluation.confidence_score == 85
    assert evaluation.can_use_for_execution is False


def test_learning_shadow_confidence_formatter_outputs_readable_text():
    evaluation = LearningShadowConfidenceEvaluator().evaluate(
        _summary(
            total_shadow_decisions=30,
            matched_outcomes=30,
            would_reduce_size_count=30,
            reduce_size_would_help_count=20,
            reduce_size_would_hurt_count=3,
            estimated_total_shadow_impact=Decimal("10"),
        )
    )

    text = format_learning_shadow_confidence(evaluation)

    assert "Learning confidence:" in text
    assert "Score: 65/100" in text
    assert "Can use for execution: False" in text
