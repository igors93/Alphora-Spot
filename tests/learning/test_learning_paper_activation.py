from decimal import Decimal

from trading.learning_activation_criteria import LearningActivationCriteriaEvaluator
from trading.learning_paper_activation import (
    PAPER_ACTIVATION_STATUS_BLOCK_READY,
    PAPER_ACTIVATION_STATUS_CRITERIA_NOT_MET,
    PAPER_ACTIVATION_STATUS_NOT_PAPER_MODE,
    PAPER_ACTIVATION_STATUS_REDUCTION_READY,
    PAPER_ACTIVATION_STATUS_STORE_DISABLED,
    LearningPaperActivationGate,
    LearningPaperActivationSettings,
    format_learning_paper_activation,
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


def _activation(summary):
    confidence = LearningShadowConfidenceEvaluator().evaluate(summary)
    return LearningActivationCriteriaEvaluator().evaluate(
        summary=summary,
        confidence=confidence,
    )


def test_paper_activation_stays_disabled_by_default():
    evaluation = LearningPaperActivationGate().evaluate(
        _activation(_summary()),
    )

    assert evaluation.status == PAPER_ACTIVATION_STATUS_STORE_DISABLED
    assert evaluation.can_apply_reduction_in_paper is False
    assert evaluation.can_apply_block_in_paper is False
    assert evaluation.can_use_for_execution is False


def test_paper_activation_requires_paper_mode():
    evaluation = LearningPaperActivationGate(
        settings=LearningPaperActivationSettings(
            execution_mode="TESTNET_LIVE",
            learning_store_enabled=True,
            paper_activation_enabled=True,
            paper_reduction_enabled=True,
        )
    ).evaluate(_activation(_summary()))

    assert evaluation.status == PAPER_ACTIVATION_STATUS_NOT_PAPER_MODE
    assert evaluation.can_apply_reduction_in_paper is False
    assert evaluation.can_use_for_execution is False


def test_paper_activation_allows_reduction_when_flags_and_criteria_pass():
    evaluation = LearningPaperActivationGate(
        settings=LearningPaperActivationSettings(
            execution_mode="PAPER_LIVE",
            learning_store_enabled=True,
            paper_activation_enabled=True,
            paper_reduction_enabled=True,
        )
    ).evaluate(_activation(_summary()))

    assert evaluation.status == PAPER_ACTIVATION_STATUS_REDUCTION_READY
    assert evaluation.can_apply_reduction_in_paper is True
    assert evaluation.can_apply_block_in_paper is False
    assert evaluation.can_activate_testnet is False
    assert evaluation.can_activate_real is False
    assert evaluation.can_use_for_execution is False


def test_paper_activation_allows_block_when_flags_and_criteria_pass():
    summary = _summary(
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

    evaluation = LearningPaperActivationGate(
        settings=LearningPaperActivationSettings(
            execution_mode="PAPER_LIVE",
            learning_store_enabled=True,
            paper_activation_enabled=True,
            paper_reduction_enabled=True,
            paper_block_enabled=True,
        )
    ).evaluate(_activation(summary))

    assert evaluation.status == PAPER_ACTIVATION_STATUS_BLOCK_READY
    assert evaluation.can_apply_reduction_in_paper is True
    assert evaluation.can_apply_block_in_paper is True
    assert evaluation.can_use_for_execution is False


def test_paper_activation_does_not_allow_when_criteria_fail():
    summary = _summary(
        total_shadow_decisions=5,
        matched_outcomes=5,
        would_reduce_size_count=5,
        reduce_size_would_help_count=4,
        reduce_size_would_hurt_count=0,
        estimated_total_shadow_impact=Decimal("5"),
    )

    evaluation = LearningPaperActivationGate(
        settings=LearningPaperActivationSettings(
            execution_mode="PAPER_LIVE",
            learning_store_enabled=True,
            paper_activation_enabled=True,
            paper_reduction_enabled=True,
        )
    ).evaluate(_activation(summary))

    assert evaluation.status == PAPER_ACTIVATION_STATUS_CRITERIA_NOT_MET
    assert evaluation.can_apply_reduction_in_paper is False
    assert evaluation.can_apply_block_in_paper is False


def test_paper_activation_formatter_outputs_readable_text():
    evaluation = LearningPaperActivationGate().evaluate(
        _activation(_summary()),
    )

    text = format_learning_paper_activation(evaluation)

    assert "Learning paper activation flags:" in text
    assert "Paper activation enabled: False" in text
    assert "Can use for execution: False" in text
