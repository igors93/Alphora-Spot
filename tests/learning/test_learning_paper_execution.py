from decimal import Decimal

from trading.learning_execution_shadow import LearningExecutionShadowDecision
from trading.learning_paper_activation import LearningPaperActivationSettings
from trading.learning_paper_execution import (
    PAPER_EXECUTION_STATUS_BLOCK_APPLIED,
    PAPER_EXECUTION_STATUS_BLOCK_NOT_ELIGIBLE,
    PAPER_EXECUTION_STATUS_DISABLED,
    PAPER_EXECUTION_STATUS_NOT_ENTRY_ACTION,
    PAPER_EXECUTION_STATUS_REDUCTION_APPLIED,
    PAPER_EXECUTION_STATUS_REDUCTION_NOT_ELIGIBLE,
    LearningPaperExecutionPolicy,
)


def _settings(*, reduction=True, block=False):
    return LearningPaperActivationSettings(
        execution_mode="PAPER_LIVE",
        learning_store_enabled=True,
        paper_activation_enabled=True,
        paper_reduction_enabled=reduction,
        paper_block_enabled=block,
    )


def _shadow_decision(*, status="WOULD_REDUCE_SIZE", action="OPEN_LONG"):
    return LearningExecutionShadowDecision(
        symbol="BTCUSDT",
        action=action,
        status=status,
        suggested_action=(
            "BLOCK_EXECUTION_SHADOW" if status == "WOULD_BLOCK" else "REDUCE_SIZE_SHADOW"
        ),
        reason="test",
        plan_id="tp_test",
        original_quantity=Decimal("0.2"),
        shadow_quantity=Decimal("0") if status == "WOULD_BLOCK" else Decimal("0.1"),
        suggested_size_multiplier=(Decimal("0") if status == "WOULD_BLOCK" else Decimal("0.5")),
        would_block_trade=status == "WOULD_BLOCK",
        would_reduce_size=status == "WOULD_REDUCE_SIZE",
    )


def test_learning_paper_execution_stays_disabled_by_default():
    decision = LearningPaperExecutionPolicy().evaluate(
        shadow_decision=_shadow_decision(),
        activation_report_payload={"can_apply_reduction_in_paper": True},
    )

    assert decision.status == PAPER_EXECUTION_STATUS_DISABLED
    assert decision.applied is False
    assert decision.can_use_for_execution is False


def test_learning_paper_execution_applies_reduction_when_enabled_and_eligible():
    decision = LearningPaperExecutionPolicy(settings=_settings()).evaluate(
        shadow_decision=_shadow_decision(),
        activation_report_payload={"can_apply_reduction_in_paper": True},
    )

    assert decision.status == PAPER_EXECUTION_STATUS_REDUCTION_APPLIED
    assert decision.reduced is True
    assert decision.blocked is False
    assert decision.final_quantity == Decimal("0.1")
    assert decision.can_use_for_execution is True
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False


def test_learning_paper_execution_does_not_reduce_when_not_eligible():
    decision = LearningPaperExecutionPolicy(settings=_settings()).evaluate(
        shadow_decision=_shadow_decision(),
        activation_report_payload={"can_apply_reduction_in_paper": False},
    )

    assert decision.status == PAPER_EXECUTION_STATUS_REDUCTION_NOT_ELIGIBLE
    assert decision.reduced is False
    assert decision.final_quantity == Decimal("0.2")
    assert decision.can_use_for_execution is False


def test_learning_paper_execution_applies_block_when_enabled_and_eligible():
    decision = LearningPaperExecutionPolicy(settings=_settings(block=True)).evaluate(
        shadow_decision=_shadow_decision(status="WOULD_BLOCK"),
        activation_report_payload={"can_apply_block_in_paper": True},
    )

    assert decision.status == PAPER_EXECUTION_STATUS_BLOCK_APPLIED
    assert decision.blocked is True
    assert decision.reduced is False
    assert decision.final_quantity == Decimal("0")
    assert decision.can_use_for_execution is True


def test_learning_paper_execution_does_not_block_when_not_eligible():
    decision = LearningPaperExecutionPolicy(settings=_settings(block=True)).evaluate(
        shadow_decision=_shadow_decision(status="WOULD_BLOCK"),
        activation_report_payload={"can_apply_block_in_paper": False},
    )

    assert decision.status == PAPER_EXECUTION_STATUS_BLOCK_NOT_ELIGIBLE
    assert decision.blocked is False
    assert decision.final_quantity == Decimal("0.2")
    assert decision.can_use_for_execution is False


def test_learning_paper_execution_never_blocks_or_reduces_exit_actions():
    decision = LearningPaperExecutionPolicy(settings=_settings(block=True)).evaluate(
        shadow_decision=_shadow_decision(status="WOULD_BLOCK", action="CLOSE_LONG"),
        activation_report_payload={"can_apply_block_in_paper": True},
    )

    assert decision.status == PAPER_EXECUTION_STATUS_NOT_ENTRY_ACTION
    assert decision.blocked is False
    assert decision.final_quantity == Decimal("0.2")
