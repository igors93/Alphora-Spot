from decimal import Decimal

from trading.intelligent_order_builder import IntelligentOrderBuilder
from trading.intelligent_order_comparator import (
    INTELLIGENT_ORDER_COMPARISON_STATUS_COMPARED,
    INTELLIGENT_ORDER_COMPARISON_STATUS_NO_INTELLIGENT_PLAN,
    IntelligentOrderComparator,
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


def test_intelligent_order_comparator_handles_missing_intelligent_plan():
    trade_plan = _trade_plan()
    comparator = IntelligentOrderComparator()

    comparison = comparator.compare(
        trade_plan=trade_plan,
        intelligent_order_plan=None,
        action="OPEN_LONG",
        planned_quantity=None,
        planned_exit=None,
    )

    assert comparison.status == INTELLIGENT_ORDER_COMPARISON_STATUS_NO_INTELLIGENT_PLAN
    assert comparison.trade_plan_id == trade_plan.plan_id
    assert comparison.intelligent_order_plan_id is None
    assert comparison.execution_unchanged is True
    assert comparison.can_use_for_execution is False


def test_intelligent_order_comparator_records_shadow_differences():
    trade_plan = _trade_plan()
    builder = IntelligentOrderBuilder()
    intelligent_plan = builder.build_shadow_plan(
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
    comparator = IntelligentOrderComparator()

    comparison = comparator.compare(
        trade_plan=trade_plan,
        intelligent_order_plan=intelligent_plan,
        action="OPEN_LONG",
        planned_quantity=Decimal("0.25"),
        planned_exit={
            "stop_price": Decimal("98.5"),
            "take_profit_price": Decimal("103"),
        },
    )

    assert comparison.status == INTELLIGENT_ORDER_COMPARISON_STATUS_COMPARED
    assert comparison.comparison_id.startswith("ioc_")
    assert comparison.trade_plan_id == trade_plan.plan_id
    assert comparison.intelligent_order_plan_id == intelligent_plan.plan_id
    assert comparison.quantity_changed is True
    assert comparison.stop_changed is True
    assert comparison.take_profit_changed is True
    assert comparison.execution_unchanged is True
    assert comparison.shadow_only is True
    assert comparison.can_use_for_execution is False
    assert comparison.differences["quantity"]["recommended"] == "0.5"
    assert comparison.differences["quantity"]["execution_candidate"] == "0.25"
    assert comparison.differences["stop"]["recommended"] == "99"
    assert comparison.differences["stop"]["execution_candidate"] == "98.5"
    assert comparison.differences["take_profit"]["recommended"] == "102"
    assert comparison.differences["take_profit"]["execution_candidate"] == "103"
