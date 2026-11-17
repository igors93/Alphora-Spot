from decimal import Decimal

from governor.adaptive_adjustment_activation import (
    ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_APPLIED,
    ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_DISABLED,
    ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_DYNAMIC_EXIT_DISABLED,
    ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_MANUAL_REVIEW_REQUIRED,
    ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_NOT_PAPER_ACTIVE,
    AdaptiveAdjustmentActivationPolicy,
)
from governor.adaptive_adjustment_plan import (
    PLAN_ACTION_PARTIAL_REDUCTION_REVIEW,
    PLAN_ACTION_TRAILING_STOP,
    PLAN_STATUS_SHADOW_READY,
    PLAN_STATUS_SHADOW_REVIEW_REQUIRED,
    AdaptiveAdjustmentPlan,
)


def _plan(
    *,
    action=PLAN_ACTION_TRAILING_STOP,
    status=PLAN_STATUS_SHADOW_READY,
    requires_order_change=True,
    requires_manual_review=False,
):
    return AdaptiveAdjustmentPlan(
        symbol="BTCUSDT",
        action=action,
        status=status,
        reason="test",
        confidence=80,
        current_price=Decimal("101.5"),
        entry_price=Decimal("100"),
        pnl_pct=Decimal("0.015"),
        current_stop_price=Decimal("100.2"),
        current_take_profit_price=Decimal("104"),
        target_stop_price=Decimal("101.1955"),
        target_take_profit_price=Decimal("104"),
        trade_plan_id="tp_test",
        requires_order_change=requires_order_change,
        requires_manual_review=requires_manual_review,
        execution_enabled=False,
        shadow_only=True,
    )


def test_adaptive_adjustment_activation_applies_trailing_in_paper_active():
    decision = AdaptiveAdjustmentActivationPolicy().evaluate(
        adjustment_plan=_plan(),
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        adaptive_execution_enabled=True,
        dynamic_exit_enabled=True,
    )

    assert decision.status == ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_APPLIED
    assert decision.execution_enabled is True
    assert decision.applied is True
    assert decision.can_use_for_execution is True
    assert decision.paper_only is True
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False


def test_adaptive_adjustment_activation_stays_disabled_when_flag_off():
    decision = AdaptiveAdjustmentActivationPolicy().evaluate(
        adjustment_plan=_plan(),
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        adaptive_execution_enabled=False,
        dynamic_exit_enabled=True,
    )

    assert decision.status == ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_DISABLED
    assert decision.execution_enabled is False
    assert decision.applied is False


def test_adaptive_adjustment_activation_requires_dynamic_exit():
    decision = AdaptiveAdjustmentActivationPolicy().evaluate(
        adjustment_plan=_plan(),
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        adaptive_execution_enabled=True,
        dynamic_exit_enabled=False,
    )

    assert decision.status == (ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_DYNAMIC_EXIT_DISABLED)
    assert decision.execution_enabled is False
    assert decision.applied is False


def test_adaptive_adjustment_activation_does_not_apply_in_testnet_or_real():
    for level, execution_mode in (
        ("TESTNET_BLOCK", "TESTNET_LIVE"),
        ("REAL_BLOCK", "REAL_LIVE"),
    ):
        decision = AdaptiveAdjustmentActivationPolicy().evaluate(
            adjustment_plan=_plan(),
            architecture_mode="INTELLIGENT_FLOW",
            intelligent_activation_level=level,
            execution_mode=execution_mode,
            adaptive_execution_enabled=True,
            dynamic_exit_enabled=True,
        )

        assert decision.status == (ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_NOT_PAPER_ACTIVE)
        assert decision.execution_enabled is False
        assert decision.applied is False
        assert decision.can_activate_testnet is False
        assert decision.can_activate_real is False


def test_adaptive_adjustment_activation_keeps_partial_reduction_in_review():
    decision = AdaptiveAdjustmentActivationPolicy().evaluate(
        adjustment_plan=_plan(
            action=PLAN_ACTION_PARTIAL_REDUCTION_REVIEW,
            status=PLAN_STATUS_SHADOW_REVIEW_REQUIRED,
            requires_order_change=False,
            requires_manual_review=True,
        ),
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        adaptive_execution_enabled=True,
        dynamic_exit_enabled=True,
    )

    assert decision.status == (ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_MANUAL_REVIEW_REQUIRED)
    assert decision.execution_enabled is False
    assert decision.applied is False
