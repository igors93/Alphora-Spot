from decimal import Decimal

from trading.intelligent_exit_builder import IntelligentExitBuilder
from trading.intelligent_exit_comparator import (
    INTELLIGENT_EXIT_COMPARISON_STATUS_COMPARED,
    INTELLIGENT_EXIT_COMPARISON_STATUS_NO_EXIT_PLAN,
    IntelligentExitComparator,
)
from trading.plan import TradePlan


def _trade_plan(
    *,
    action="OPEN_LONG",
    entry=Decimal("100"),
    stop=Decimal("99"),
    take=Decimal("102"),
    reward_risk=Decimal("2"),
) -> TradePlan:
    return TradePlan(
        symbol="BTCUSDT",
        action=action,
        status="ALLOW_NORMAL",
        strategy_name="tests.FakeStrategy",
        signal_confidence=95,
        quality_score=80,
        entry_reference_price=entry,
        quantity=Decimal("0.5"),
        notional=Decimal("50"),
        stop_price=stop,
        take_profit_price=take,
        estimated_entry_fee=Decimal("0.02"),
        estimated_exit_fee=Decimal("0.02"),
        estimated_spread_cost=Decimal("0.01"),
        estimated_slippage_cost=Decimal("0.01"),
        estimated_funding_cost=Decimal("0"),
        estimated_round_trip_cost=Decimal("0.06"),
        expected_net_gain=Decimal("0.94"),
        expected_net_loss=Decimal("1.06"),
        reward_risk=reward_risk,
        size_multiplier=Decimal("1"),
        execution_style="MARKET",
        management_style="FIXED_PROTECTION",
        reason="test",
        block_reason=None,
    )


def test_intelligent_exit_comparator_handles_missing_exit_plan():
    trade_plan = _trade_plan()

    comparison = IntelligentExitComparator().compare(
        trade_plan=trade_plan,
        intelligent_exit_plan=None,
        action="OPEN_LONG",
        planned_exit=None,
    )

    assert comparison.status == INTELLIGENT_EXIT_COMPARISON_STATUS_NO_EXIT_PLAN
    assert comparison.trade_plan_id == trade_plan.plan_id
    assert comparison.intelligent_exit_plan_id is None
    assert comparison.execution_unchanged is True
    assert comparison.can_use_for_execution is False


def test_intelligent_exit_comparator_compares_equal_shadow_protection():
    trade_plan = _trade_plan()
    exit_plan = IntelligentExitBuilder().build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_LONG",
        planned_exit=None,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="SHADOW_ONLY",
        dynamic_exit_enabled=False,
    )

    comparison = IntelligentExitComparator().compare(
        trade_plan=trade_plan,
        intelligent_exit_plan=exit_plan,
        action="OPEN_LONG",
        planned_exit=None,
    )

    assert comparison.status == INTELLIGENT_EXIT_COMPARISON_STATUS_COMPARED
    assert comparison.comparison_id.startswith("iexitcmp_")
    assert comparison.trade_plan_id == trade_plan.plan_id
    assert comparison.intelligent_exit_plan_id == exit_plan.plan_id
    assert comparison.stop_changed is False
    assert comparison.take_profit_changed is False
    assert comparison.risk_reward_changed is False
    assert comparison.protection_complete is True
    assert comparison.execution_unchanged is True
    assert comparison.shadow_only is True
    assert comparison.can_use_for_execution is False
    assert comparison.differences["stop"]["trade_plan"] == "99"
    assert comparison.differences["stop"]["recommended"] == "99"
    assert comparison.differences["take_profit"]["trade_plan"] == "102"
    assert comparison.differences["take_profit"]["recommended"] == "102"


def test_intelligent_exit_comparator_detects_changed_planned_exit():
    trade_plan = _trade_plan()
    planned_exit = {
        "stop_price": Decimal("98.5"),
        "take_profit_price": Decimal("102.5"),
    }
    exit_plan = IntelligentExitBuilder().build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_LONG",
        planned_exit=planned_exit,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        dynamic_exit_enabled=True,
    )

    comparison = IntelligentExitComparator().compare(
        trade_plan=trade_plan,
        intelligent_exit_plan=exit_plan,
        action="OPEN_LONG",
        planned_exit=planned_exit,
    )

    assert comparison.status == INTELLIGENT_EXIT_COMPARISON_STATUS_COMPARED
    assert comparison.stop_changed is True
    assert comparison.take_profit_changed is True
    assert comparison.risk_reward_changed is True
    assert comparison.differences["stop"]["trade_plan"] == "99"
    assert comparison.differences["stop"]["recommended"] == "98.5"
    assert comparison.differences["stop"]["execution_candidate"] == "98.5"
    assert comparison.differences["take_profit"]["trade_plan"] == "102"
    assert comparison.differences["take_profit"]["recommended"] == "102.5"
    assert comparison.differences["take_profit"]["execution_candidate"] == "102.5"
