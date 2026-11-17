from decimal import Decimal

from trading.learning_advisory import LearningAdvisoryEvaluation
from trading.learning_plan_guard import (
    LEARNING_GUARD_ACTION_ALLOW,
    LEARNING_GUARD_ACTION_BLOCK,
    LEARNING_GUARD_ACTION_REDUCE_SIZE,
    LEARNING_GUARD_STATUS_NO_ADVISORY,
    LEARNING_GUARD_STATUS_WOULD_BLOCK,
    LEARNING_GUARD_STATUS_WOULD_REDUCE_SIZE,
    LearningPlanGuard,
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
        diagnostics={},
    )


def test_learning_plan_guard_allows_when_advisory_is_missing():
    guard = LearningPlanGuard().evaluate(
        plan=_trade_plan(),
        advisory=None,
    )

    assert guard.status == LEARNING_GUARD_STATUS_NO_ADVISORY
    assert guard.suggested_action == LEARNING_GUARD_ACTION_ALLOW
    assert guard.would_block_trade is False
    assert guard.can_use_for_execution is False
    assert guard.shadow_only is True


def test_learning_plan_guard_would_block_warning_advisory():
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
        would_reduce_priority=False,
        can_use_for_execution=False,
        shadow_only=True,
    )

    guard = LearningPlanGuard().evaluate(
        plan=_trade_plan(),
        advisory=advisory,
    )

    assert guard.status == LEARNING_GUARD_STATUS_WOULD_BLOCK
    assert guard.suggested_action == LEARNING_GUARD_ACTION_BLOCK
    assert guard.suggested_size_multiplier == Decimal("0")
    assert guard.would_block_trade is True
    assert guard.can_use_for_execution is False


def test_learning_plan_guard_would_reduce_size_for_symbol_priority_warning():
    advisory = LearningAdvisoryEvaluation(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        status="WARNING",
        severity="WARNING",
        reason="LEARNING_RECOMMENDATIONS_REQUIRE_ATTENTION",
        recommendations_count=1,
        relevant_recommendations_count=1,
        total_outcomes=25,
        would_block_trade=False,
        would_reduce_priority=True,
        can_use_for_execution=False,
        shadow_only=True,
    )

    guard = LearningPlanGuard().evaluate(
        plan=_trade_plan(),
        advisory=advisory,
    )

    assert guard.status == LEARNING_GUARD_STATUS_WOULD_REDUCE_SIZE
    assert guard.suggested_action == LEARNING_GUARD_ACTION_REDUCE_SIZE
    assert guard.suggested_size_multiplier == Decimal("0.5")
    assert guard.would_reduce_size is True
    assert guard.can_use_for_execution is False
