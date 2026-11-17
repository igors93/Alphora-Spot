from decimal import Decimal

from trading.learning_advisory import LearningAdvisoryEvaluation
from trading.learning_plan_guard import LearningPlanGuardEvaluation
from trading.learning_trade_plan import (
    LEARNING_ADVISORY_DIAGNOSTIC_KEY,
    LEARNING_GUARD_DIAGNOSTIC_KEY,
    attach_learning_advisory_to_trade_plan,
    attach_learning_guard_to_trade_plan,
)
from trading.plan import TradePlan


def _trade_plan() -> TradePlan:
    return TradePlan(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        status="ALLOW_NORMAL",
        strategy_name="tests.strategy",
        signal_confidence=80,
        quality_score=70,
        entry_reference_price=Decimal("100"),
        quantity=Decimal("0.1"),
        notional=Decimal("10"),
        stop_price=Decimal("98"),
        take_profit_price=Decimal("104"),
        estimated_entry_fee=Decimal("0.01"),
        estimated_exit_fee=Decimal("0.01"),
        estimated_spread_cost=Decimal("0.01"),
        estimated_slippage_cost=Decimal("0.01"),
        estimated_funding_cost=Decimal("0"),
        estimated_round_trip_cost=Decimal("0.04"),
        expected_net_gain=Decimal("0.36"),
        expected_net_loss=Decimal("-0.24"),
        reward_risk=Decimal("1.5"),
        size_multiplier=Decimal("1"),
        execution_style="MARKET",
        management_style="STATIC",
        reason="test",
        block_reason=None,
        diagnostics={"existing": True},
    )


def test_attach_learning_advisory_to_trade_plan_adds_diagnostics():
    advisory = LearningAdvisoryEvaluation(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        status="WARNING",
        severity="WARNING",
        reason="LEARNING_RECOMMENDATIONS_REQUIRE_ATTENTION",
        recommendations_count=1,
        relevant_recommendations_count=1,
        total_outcomes=25,
        would_block_trade=True,
        would_reduce_priority=True,
        can_use_for_execution=False,
        shadow_only=True,
    )

    plan = attach_learning_advisory_to_trade_plan(
        plan=_trade_plan(),
        advisory=advisory,
    )

    assert plan.diagnostics["existing"] is True
    assert LEARNING_ADVISORY_DIAGNOSTIC_KEY in plan.diagnostics
    assert plan.diagnostics[LEARNING_ADVISORY_DIAGNOSTIC_KEY]["status"] == "WARNING"
    assert plan.diagnostics[LEARNING_ADVISORY_DIAGNOSTIC_KEY]["would_block_trade"] is True
    assert plan.diagnostics[LEARNING_ADVISORY_DIAGNOSTIC_KEY]["can_use_for_execution"] is False


def test_attach_learning_advisory_to_trade_plan_returns_same_plan_without_advisory():
    original = _trade_plan()

    plan = attach_learning_advisory_to_trade_plan(
        plan=original,
        advisory=None,
    )

    assert plan is original


def test_attach_learning_guard_to_trade_plan_adds_diagnostics():
    guard = LearningPlanGuardEvaluation(
        symbol="BTCUSDT",
        plan_id="tp_test",
        plan_action="OPEN_LONG",
        status="WOULD_BLOCK",
        suggested_action="BLOCK_TRADE_SHADOW",
        reason="LEARNING_ADVISORY_WOULD_BLOCK_TRADE",
        severity="WARNING",
        advisory_status="WARNING",
        advisory_id="adv_test",
        would_block_trade=True,
        suggested_size_multiplier=Decimal("0"),
        can_use_for_execution=False,
        shadow_only=True,
    )

    plan = attach_learning_guard_to_trade_plan(
        plan=_trade_plan(),
        guard=guard,
    )

    assert plan.diagnostics["existing"] is True
    assert LEARNING_GUARD_DIAGNOSTIC_KEY in plan.diagnostics
    assert plan.diagnostics[LEARNING_GUARD_DIAGNOSTIC_KEY]["status"] == "WOULD_BLOCK"
    assert (
        plan.diagnostics[LEARNING_GUARD_DIAGNOSTIC_KEY]["suggested_action"] == "BLOCK_TRADE_SHADOW"
    )
    assert plan.diagnostics[LEARNING_GUARD_DIAGNOSTIC_KEY]["can_use_for_execution"] is False


def test_attach_learning_guard_to_trade_plan_returns_same_plan_without_guard():
    original = _trade_plan()

    plan = attach_learning_guard_to_trade_plan(
        plan=original,
        guard=None,
    )

    assert plan is original
