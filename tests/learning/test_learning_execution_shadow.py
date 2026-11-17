from decimal import Decimal

from trading.learning_execution_shadow import (
    LEARNING_EXECUTION_ACTION_ALLOW,
    LEARNING_EXECUTION_ACTION_BLOCK,
    LEARNING_EXECUTION_ACTION_REDUCE_SIZE,
    LEARNING_EXECUTION_STATUS_ALLOW,
    LEARNING_EXECUTION_STATUS_NO_GUARD,
    LEARNING_EXECUTION_STATUS_WOULD_BLOCK,
    LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE,
    LearningExecutionShadowPolicy,
)
from trading.plan import TradePlan


def _trade_plan(*, learning_guard=None) -> TradePlan:
    diagnostics = {}
    if learning_guard is not None:
        diagnostics["learning_guard"] = learning_guard

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
        diagnostics=diagnostics,
    )


def test_learning_execution_shadow_allows_when_guard_is_missing():
    decision = LearningExecutionShadowPolicy().evaluate(
        plan=_trade_plan(),
        action="OPEN_LONG",
        planned_quantity=Decimal("0.1"),
    )

    assert decision.status == LEARNING_EXECUTION_STATUS_NO_GUARD
    assert decision.suggested_action == LEARNING_EXECUTION_ACTION_ALLOW
    assert decision.shadow_quantity == Decimal("0.1")
    assert decision.can_use_for_execution is False


def test_learning_execution_shadow_would_block_when_guard_would_block():
    decision = LearningExecutionShadowPolicy().evaluate(
        plan=_trade_plan(
            learning_guard={
                "guard_id": "lg_test",
                "status": "WOULD_BLOCK",
                "suggested_action": "BLOCK_TRADE_SHADOW",
                "reason": "LEARNING_ADVISORY_WOULD_BLOCK_TRADE",
                "suggested_size_multiplier": "0",
                "would_block_trade": True,
            }
        ),
        action="OPEN_LONG",
        planned_quantity=Decimal("0.1"),
    )

    assert decision.status == LEARNING_EXECUTION_STATUS_WOULD_BLOCK
    assert decision.suggested_action == LEARNING_EXECUTION_ACTION_BLOCK
    assert decision.original_quantity == Decimal("0.1")
    assert decision.shadow_quantity == Decimal("0")
    assert decision.would_block_trade is True
    assert decision.can_use_for_execution is False


def test_learning_execution_shadow_would_reduce_size_when_guard_would_reduce():
    decision = LearningExecutionShadowPolicy().evaluate(
        plan=_trade_plan(
            learning_guard={
                "guard_id": "lg_test",
                "status": "WOULD_REDUCE_SIZE",
                "suggested_action": "REDUCE_SIZE_SHADOW",
                "reason": "LEARNING_ADVISORY_WOULD_REDUCE_SIZE",
                "suggested_size_multiplier": "0.5",
                "would_reduce_size": True,
            }
        ),
        action="OPEN_LONG",
        planned_quantity=Decimal("0.2"),
    )

    assert decision.status == LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE
    assert decision.suggested_action == LEARNING_EXECUTION_ACTION_REDUCE_SIZE
    assert decision.original_quantity == Decimal("0.2")
    assert decision.shadow_quantity == Decimal("0.10")
    assert decision.would_reduce_size is True
    assert decision.can_use_for_execution is False


def test_learning_execution_shadow_allows_when_guard_allows():
    decision = LearningExecutionShadowPolicy().evaluate(
        plan=_trade_plan(
            learning_guard={
                "guard_id": "lg_test",
                "status": "ALLOW",
                "suggested_action": "ALLOW_TRADE_SHADOW",
                "reason": "LEARNING_ADVISORY_DOES_NOT_RESTRICT_PLAN",
            }
        ),
        action="OPEN_LONG",
        planned_quantity=Decimal("0.1"),
    )

    assert decision.status == LEARNING_EXECUTION_STATUS_ALLOW
    assert decision.suggested_action == LEARNING_EXECUTION_ACTION_ALLOW
    assert decision.shadow_quantity == Decimal("0.1")
    assert decision.can_use_for_execution is False
