from decimal import Decimal

from futures.execution.order_policy import ExecutionStyle, OrderExecutionPolicy
from futures.planning.economic_gate import EconomicGateDecision


def test_market_safe_assumes_taker_entry_and_taker_exit():
    policy = OrderExecutionPolicy()
    plan = policy.build_entry_plan(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        market_price=Decimal("100"),
        reference_price=Decimal("100"),
        economic_decision=_allowed_gate_decision(),
    )

    assert plan.allowed is True
    assert plan.execution_style == ExecutionStyle.MARKET_SAFE.value
    assert plan.entry_order_type == "MARKET"
    assert plan.assumes_entry_liquidity == "TAKER"
    assert plan.assumes_exit_liquidity == "TAKER"
    assert plan.operational_entry_order_type == "MARKET"
    assert plan.executor_behavior_unchanged is True
    assert plan.diagnostics["cost_assumption"] == "TAKER_ENTRY_TAKER_EXIT"
    assert plan.diagnostics["operational_entry_order_type"] == "MARKET"


def test_limit_maker_first_is_not_default(config_factory):
    policy = OrderExecutionPolicy.from_config(config_factory())

    assert policy.execution_style == ExecutionStyle.MARKET_SAFE.value
    assert policy.limit_entry_enabled is False


def test_limit_maker_first_blocks_without_limit_execution_controls():
    policy = OrderExecutionPolicy(
        execution_style=ExecutionStyle.LIMIT_MAKER_FIRST.value,
        limit_entry_enabled=True,
    )
    plan = policy.build_entry_plan(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        market_price=Decimal("100"),
        reference_price=Decimal("100"),
        economic_decision=_allowed_gate_decision(),
    )

    assert plan.allowed is False
    assert plan.reason == "LIMIT_MAKER_FIRST_NOT_OPERATIONAL_WITHOUT_FILL_RECONCILIATION"
    assert plan.entry_order_type is None


def test_limit_with_market_fallback_does_not_return_market_safe_without_limit_runtime():
    policy = OrderExecutionPolicy(
        execution_style=ExecutionStyle.LIMIT_WITH_MARKET_FALLBACK.value,
        limit_entry_enabled=True,
        limit_entry_fallback_to_market=True,
    )
    plan = policy.build_entry_plan(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        market_price=Decimal("100"),
        reference_price=Decimal("100"),
        economic_decision=_allowed_gate_decision(),
    )

    assert plan.allowed is False
    assert plan.reason == "LIMIT_ENTRY_NOT_OPERATIONAL_WITHOUT_FILL_RECONCILIATION"
    assert plan.execution_style == ExecutionStyle.LIMIT_WITH_MARKET_FALLBACK.value
    assert plan.operational_entry_order_type is None
    assert plan.executor_behavior_unchanged is True


def _allowed_gate_decision() -> EconomicGateDecision:
    return EconomicGateDecision(
        allowed=True,
        reason="ECONOMIC_GATE_APPROVED",
        plan_id="tp_test",
        symbol="BTCUSDT",
        action="OPEN_LONG",
        expected_net_gain=Decimal("1"),
        expected_net_gain_bps=Decimal("100"),
        expected_net_loss=Decimal("0.5"),
        reward_risk=Decimal("2"),
        round_trip_cost=Decimal("0.1"),
        round_trip_cost_bps=Decimal("10"),
        minimum_profitable_move=Decimal("0.1"),
        diagnostics={},
    )
