from decimal import Decimal

from trading.intelligent_order_builder import (
    INTELLIGENT_ORDER_STATUS_NO_TRADE_PLAN,
    INTELLIGENT_ORDER_STATUS_SHADOW_READY,
    IntelligentOrderBuilder,
)
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


def test_intelligent_order_builder_returns_no_plan_when_trade_plan_missing():
    builder = IntelligentOrderBuilder()

    plan = builder.build_shadow_plan(
        trade_plan=None,
        action="OPEN_LONG",
        planned_quantity=None,
        planned_exit=None,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=False,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="SHADOW_ONLY",
    )

    assert plan.status == INTELLIGENT_ORDER_STATUS_NO_TRADE_PLAN
    assert plan.shadow_only is True
    assert plan.can_use_for_execution is False
    assert plan.trade_plan_id is None
    assert plan.recommended_quantity is None


def test_intelligent_order_builder_mirrors_trade_plan_in_shadow():
    builder = IntelligentOrderBuilder()
    trade_plan = _trade_plan()

    plan = builder.build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_LONG",
        planned_quantity=Decimal("0.25"),
        planned_exit={
            "stop_price": Decimal("98.5"),
            "take_profit_price": Decimal("103"),
        },
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
    )

    assert plan.status == INTELLIGENT_ORDER_STATUS_SHADOW_READY
    assert plan.plan_id.startswith("iop_")
    assert plan.trade_plan_id == trade_plan.plan_id
    assert plan.recommended_quantity == Decimal("0.5")
    assert plan.recommended_stop_price == Decimal("99")
    assert plan.recommended_take_profit_price == Decimal("102")
    assert plan.execution_quantity_candidate == Decimal("0.25")
    assert plan.execution_stop_price_candidate == Decimal("98.5")
    assert plan.execution_take_profit_price_candidate == Decimal("103")
    assert plan.shadow_only is True
    assert plan.can_use_for_execution is False
    assert plan.diagnostics["dynamic_sizing_enabled"] is True
    assert plan.diagnostics["dynamic_exit_enabled"] is True
