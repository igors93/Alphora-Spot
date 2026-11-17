from decimal import Decimal

from trading.learning_execution_shadow import LearningExecutionShadowDecision
from trading.learning_testnet_execution import (
    TESTNET_EXECUTION_STATUS_BLOCK_APPLIED,
    TESTNET_EXECUTION_STATUS_BLOCK_SIGNAL_NOT_APPLIED,
    TESTNET_EXECUTION_STATUS_NOT_ENTRY_ACTION,
    TESTNET_EXECUTION_STATUS_NOT_TESTNET_MODE,
    TESTNET_EXECUTION_STATUS_NOT_TESTNET_REDUCE_LEVEL,
    TESTNET_EXECUTION_STATUS_REDUCTION_APPLIED,
    TESTNET_EXECUTION_STATUS_REDUCTION_NOT_ELIGIBLE,
    TESTNET_EXECUTION_STATUS_STORE_DISABLED,
    LearningTestnetExecutionPolicy,
    LearningTestnetExecutionSettings,
)


def _settings(*, level="TESTNET_REDUCE", mode="TESTNET_LIVE"):
    return LearningTestnetExecutionSettings(
        execution_mode=mode,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level=level,
        learning_store_enabled=True,
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


def test_testnet_execution_stays_disabled_without_learning_store():
    decision = LearningTestnetExecutionPolicy().evaluate(
        shadow_decision=_shadow_decision(),
        activation_report_payload={"can_apply_reduction_in_paper": True},
    )

    assert decision.status == TESTNET_EXECUTION_STATUS_STORE_DISABLED
    assert decision.reduced is False
    assert decision.can_use_for_execution is False


def test_testnet_execution_requires_testnet_active_level():
    decision = LearningTestnetExecutionPolicy(
        settings=_settings(level="TESTNET_OBSERVE"),
    ).evaluate(
        shadow_decision=_shadow_decision(),
        activation_report_payload={"can_apply_reduction_in_paper": True},
    )

    assert decision.status == TESTNET_EXECUTION_STATUS_NOT_TESTNET_REDUCE_LEVEL
    assert decision.reduced is False
    assert decision.can_use_for_execution is False


def test_testnet_execution_applies_reduction_when_enabled_and_eligible():
    decision = LearningTestnetExecutionPolicy(settings=_settings()).evaluate(
        shadow_decision=_shadow_decision(),
        activation_report_payload={"can_apply_reduction_in_paper": True},
    )

    assert decision.status == TESTNET_EXECUTION_STATUS_REDUCTION_APPLIED
    assert decision.reduced is True
    assert decision.blocked is False
    assert decision.original_quantity == Decimal("0.2")
    assert decision.final_quantity == Decimal("0.1")
    assert decision.can_use_for_execution is True
    assert decision.can_activate_testnet is True
    assert decision.can_activate_real is False


def test_testnet_execution_does_not_reduce_when_not_eligible():
    decision = LearningTestnetExecutionPolicy(settings=_settings()).evaluate(
        shadow_decision=_shadow_decision(),
        activation_report_payload={"can_apply_reduction_in_paper": False},
    )

    assert decision.status == TESTNET_EXECUTION_STATUS_REDUCTION_NOT_ELIGIBLE
    assert decision.reduced is False
    assert decision.final_quantity == Decimal("0.2")
    assert decision.can_use_for_execution is False


def test_testnet_execution_does_not_block_in_reduce_stage():
    decision = LearningTestnetExecutionPolicy(settings=_settings()).evaluate(
        shadow_decision=_shadow_decision(status="WOULD_BLOCK"),
        activation_report_payload={"can_apply_block_in_paper": True},
    )

    assert decision.status == TESTNET_EXECUTION_STATUS_BLOCK_SIGNAL_NOT_APPLIED
    assert decision.blocked is False
    assert decision.reduced is False
    assert decision.final_quantity == Decimal("0.2")
    assert decision.can_use_for_execution is False


def test_testnet_execution_blocks_in_testnet_block_when_eligible():
    decision = LearningTestnetExecutionPolicy(
        settings=_settings(level="TESTNET_BLOCK"),
    ).evaluate(
        shadow_decision=_shadow_decision(status="WOULD_BLOCK"),
        activation_report_payload={"can_apply_block_in_paper": True},
    )

    assert decision.status == TESTNET_EXECUTION_STATUS_BLOCK_APPLIED
    assert decision.blocked is True
    assert decision.applied is True
    assert decision.final_quantity == Decimal("0")
    assert decision.can_use_for_execution is True
    assert decision.can_activate_testnet is True
    assert decision.can_activate_real is False


def test_testnet_execution_never_reduces_exit_actions():
    decision = LearningTestnetExecutionPolicy(settings=_settings()).evaluate(
        shadow_decision=_shadow_decision(action="CLOSE_LONG"),
        activation_report_payload={"can_apply_reduction_in_paper": True},
    )

    assert decision.status == TESTNET_EXECUTION_STATUS_NOT_ENTRY_ACTION
    assert decision.reduced is False
    assert decision.final_quantity == Decimal("0.2")


def test_testnet_execution_never_blocks_exit_actions_even_in_testnet_block():
    decision = LearningTestnetExecutionPolicy(
        settings=_settings(level="TESTNET_BLOCK"),
    ).evaluate(
        shadow_decision=_shadow_decision(
            status="WOULD_BLOCK",
            action="CLOSE_LONG",
        ),
        activation_report_payload={"can_apply_block_in_paper": True},
    )

    assert decision.status == TESTNET_EXECUTION_STATUS_NOT_ENTRY_ACTION
    assert decision.blocked is False
    assert decision.applied is False
    assert decision.can_use_for_execution is False
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False


def test_testnet_execution_never_applies_in_real_mode():
    decision = LearningTestnetExecutionPolicy(
        settings=_settings(level="TESTNET_BLOCK", mode="REAL_LIVE"),
    ).evaluate(
        shadow_decision=_shadow_decision(status="WOULD_BLOCK"),
        activation_report_payload={"can_apply_block_in_paper": True},
    )

    assert decision.status == TESTNET_EXECUTION_STATUS_NOT_TESTNET_MODE
    assert decision.blocked is False
    assert decision.applied is False
    assert decision.can_use_for_execution is False
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False
