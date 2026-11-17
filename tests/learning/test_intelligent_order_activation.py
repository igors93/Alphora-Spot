from decimal import Decimal

from trading.intelligent_order_activation import (
    INTELLIGENT_ORDER_ACTIVATION_STATUS_APPLIED,
    INTELLIGENT_ORDER_ACTIVATION_STATUS_MODULES_DISABLED,
    INTELLIGENT_ORDER_ACTIVATION_STATUS_NOT_PAPER_ACTIVE,
    IntelligentOrderActivationPolicy,
)
from trading.intelligent_order_builder import IntelligentOrderBuilder
from trading.plan import TradePlan


def _trade_plan() -> TradePlan:
    return TradePlan(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        status="ALLOW_NORMAL",
        strategy_name="tests.FakeStrategy",
        signal_confidence=95,
        quality_score=80,
        entry_reference_price=Decimal("100"),
        quantity=Decimal("0.5"),
        notional=Decimal("50"),
        stop_price=Decimal("99"),
        take_profit_price=Decimal("102"),
        estimated_entry_fee=Decimal("0.02"),
        estimated_exit_fee=Decimal("0.02"),
        estimated_spread_cost=Decimal("0.01"),
        estimated_slippage_cost=Decimal("0.01"),
        estimated_funding_cost=Decimal("0"),
        estimated_round_trip_cost=Decimal("0.06"),
        expected_net_gain=Decimal("0.94"),
        expected_net_loss=Decimal("1.06"),
        reward_risk=Decimal("0.8867924528"),
        size_multiplier=Decimal("1"),
        execution_style="MARKET",
        management_style="FIXED_PROTECTION",
        reason="test",
        block_reason=None,
    )


def _intelligent_plan():
    trade_plan = _trade_plan()
    return IntelligentOrderBuilder().build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_LONG",
        planned_quantity=trade_plan.quantity,
        planned_exit={
            "stop_price": trade_plan.stop_price,
            "take_profit_price": trade_plan.take_profit_price,
        },
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
    )


def test_intelligent_order_activation_applies_only_in_paper_active():
    decision = IntelligentOrderActivationPolicy().evaluate(
        intelligent_order_plan=_intelligent_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
    )

    assert decision.status == INTELLIGENT_ORDER_ACTIVATION_STATUS_APPLIED
    assert decision.applied is True
    assert decision.quantity_applied is True
    assert decision.exit_applied is True
    assert decision.can_use_for_execution is True
    assert decision.paper_only is True
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False
    assert decision.final_quantity == Decimal("0.5")
    assert decision.final_stop_price == Decimal("99")
    assert decision.final_take_profit_price == Decimal("102")


def test_intelligent_order_activation_applies_quantity_only_when_exit_module_disabled():
    decision = IntelligentOrderActivationPolicy().evaluate(
        intelligent_order_plan=_intelligent_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=False,
    )

    assert decision.status == INTELLIGENT_ORDER_ACTIVATION_STATUS_APPLIED
    assert decision.applied is True
    assert decision.quantity_applied is True
    assert decision.exit_applied is False
    assert decision.final_quantity == Decimal("0.5")
    assert decision.final_stop_price is None
    assert decision.final_take_profit_price is None
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False


def test_intelligent_order_activation_applies_exit_only_when_sizing_module_disabled():
    decision = IntelligentOrderActivationPolicy().evaluate(
        intelligent_order_plan=_intelligent_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=True,
    )

    assert decision.status == INTELLIGENT_ORDER_ACTIVATION_STATUS_APPLIED
    assert decision.applied is True
    assert decision.quantity_applied is False
    assert decision.exit_applied is True
    assert decision.final_quantity is None
    assert decision.final_stop_price == Decimal("99")
    assert decision.final_take_profit_price == Decimal("102")
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False


def test_intelligent_order_activation_does_not_apply_when_all_modules_disabled():
    decision = IntelligentOrderActivationPolicy().evaluate(
        intelligent_order_plan=_intelligent_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=False,
    )

    assert decision.status == INTELLIGENT_ORDER_ACTIVATION_STATUS_MODULES_DISABLED
    assert decision.applied is False
    assert decision.quantity_applied is False
    assert decision.exit_applied is False
    assert decision.final_quantity is None
    assert decision.final_stop_price is None
    assert decision.final_take_profit_price is None
    assert decision.can_use_for_execution is False


def test_intelligent_order_activation_does_not_apply_in_testnet_observe():
    decision = IntelligentOrderActivationPolicy().evaluate(
        intelligent_order_plan=_intelligent_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="TESTNET_OBSERVE",
        execution_mode="TESTNET_LIVE",
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=False,
    )

    assert decision.status == INTELLIGENT_ORDER_ACTIVATION_STATUS_NOT_PAPER_ACTIVE
    assert decision.applied is False
    assert decision.quantity_applied is False
    assert decision.exit_applied is False
    assert decision.can_use_for_execution is False
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False


def test_intelligent_order_activation_applies_quantity_in_testnet_reduce():
    decision = IntelligentOrderActivationPolicy().evaluate(
        intelligent_order_plan=_intelligent_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="TESTNET_REDUCE",
        execution_mode="TESTNET_LIVE",
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=False,
    )

    assert decision.status == INTELLIGENT_ORDER_ACTIVATION_STATUS_APPLIED
    assert decision.applied is True
    assert decision.quantity_applied is True
    assert decision.exit_applied is False
    assert decision.final_quantity == Decimal("0.5")
    assert decision.final_stop_price is None
    assert decision.final_take_profit_price is None
    assert decision.paper_only is False
    assert decision.testnet_only is True
    assert decision.can_activate_testnet is True
    assert decision.can_activate_real is False


def test_intelligent_order_activation_applies_quantity_and_exit_in_testnet_block():
    decision = IntelligentOrderActivationPolicy().evaluate(
        intelligent_order_plan=_intelligent_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="TESTNET_BLOCK",
        execution_mode="TESTNET_LIVE",
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
    )

    assert decision.status == INTELLIGENT_ORDER_ACTIVATION_STATUS_APPLIED
    assert decision.applied is True
    assert decision.quantity_applied is True
    assert decision.exit_applied is True
    assert decision.final_quantity == Decimal("0.5")
    assert decision.final_stop_price == Decimal("99")
    assert decision.final_take_profit_price == Decimal("102")
    assert decision.paper_only is False
    assert decision.testnet_only is True
    assert decision.can_activate_testnet is True
    assert decision.can_activate_real is False


def test_intelligent_order_activation_never_applies_exit_action_in_testnet_block():
    decision = IntelligentOrderActivationPolicy().evaluate(
        intelligent_order_plan=_intelligent_plan(),
        action="CLOSE_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="TESTNET_BLOCK",
        execution_mode="TESTNET_LIVE",
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
    )

    assert decision.status == "NOT_ENTRY_ACTION"
    assert decision.applied is False
    assert decision.quantity_applied is False
    assert decision.exit_applied is False
    assert decision.can_use_for_execution is False
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False


def test_intelligent_order_activation_never_applies_in_real_live():
    for level in ("REAL_OBSERVE", "REAL_REDUCE", "REAL_BLOCK"):
        decision = IntelligentOrderActivationPolicy().evaluate(
            intelligent_order_plan=_intelligent_plan(),
            action="OPEN_LONG",
            architecture_mode="INTELLIGENT_FLOW",
            intelligent_activation_level=level,
            execution_mode="REAL_LIVE",
            dynamic_sizing_enabled=True,
            dynamic_exit_enabled=True,
        )

        assert decision.status == "NOT_PAPER_ACTIVE"
        assert decision.applied is False
        assert decision.quantity_applied is False
        assert decision.exit_applied is False
        assert decision.can_use_for_execution is False
        assert decision.can_activate_testnet is False
        assert decision.can_activate_real is False
