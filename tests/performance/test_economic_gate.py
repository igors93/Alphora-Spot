from dataclasses import replace
from decimal import Decimal

from futures.planning.economic_gate import EconomicTradeGate
from trading.plan import TradePlan


def test_economic_trade_gate_blocks_open_long_with_non_positive_expected_gain():
    plan = replace(_plan("OPEN_LONG"), expected_net_gain=Decimal("0"))
    decision = EconomicTradeGate().evaluate(plan, "OPEN_LONG")

    assert decision.allowed is False
    assert decision.reason == "EXPECTED_NET_GAIN_NOT_POSITIVE"


def test_economic_trade_gate_blocks_open_short_with_non_positive_expected_gain():
    plan = replace(_plan("OPEN_SHORT"), expected_net_gain=Decimal("-0.01"))
    decision = EconomicTradeGate().evaluate(plan, "OPEN_SHORT")

    assert decision.allowed is False
    assert decision.reason == "EXPECTED_NET_GAIN_NOT_POSITIVE"


def test_economic_trade_gate_blocks_when_reward_risk_is_below_minimum():
    plan = replace(_plan("OPEN_LONG"), reward_risk=Decimal("1.0"))
    decision = EconomicTradeGate(min_net_reward_risk=Decimal("1.5")).evaluate(
        plan,
        "OPEN_LONG",
    )

    assert decision.allowed is False
    assert decision.reason == "REWARD_RISK_BELOW_MINIMUM"


def test_economic_trade_gate_blocks_when_round_trip_cost_bps_is_above_maximum():
    plan = replace(
        _plan("OPEN_LONG"),
        estimated_round_trip_cost=Decimal("0.25"),
        diagnostics=_diagnostics(round_trip_cost_bps=Decimal("25")),
    )
    decision = EconomicTradeGate(max_round_trip_cost_bps=Decimal("20")).evaluate(
        plan,
        "OPEN_LONG",
    )

    assert decision.allowed is False
    assert decision.reason == "ROUND_TRIP_COST_BPS_ABOVE_MAXIMUM"
    assert decision.round_trip_cost_bps == Decimal("25")


def test_economic_trade_gate_allows_positive_gain_reward_and_cost():
    plan = _plan("OPEN_LONG")
    decision = EconomicTradeGate().evaluate(plan, "OPEN_LONG")

    assert decision.allowed is True
    assert decision.reason == "ECONOMIC_GATE_APPROVED"
    assert decision.expected_net_gain == Decimal("1.00")
    assert decision.expected_net_gain_bps == Decimal("100.00")
    assert decision.reward_risk == Decimal("2.0")
    assert decision.round_trip_cost_bps == Decimal("10")


def test_economic_trade_gate_blocks_block_economic_status():
    plan = replace(_plan("OPEN_LONG"), status="BLOCK_ECONOMIC", block_reason="bad")
    decision = EconomicTradeGate().evaluate(plan, "OPEN_LONG")

    assert decision.allowed is False
    assert decision.reason == "PLAN_STATUS_NOT_EXECUTABLE"
    assert decision.diagnostics["plan_block_reason"] == "bad"


def test_economic_trade_gate_blocks_block_safety_status():
    plan = replace(_plan("OPEN_LONG"), status="BLOCK_SAFETY", block_reason="STALE_DATA")
    decision = EconomicTradeGate().evaluate(plan, "OPEN_LONG")

    assert decision.allowed is False
    assert decision.reason == "PLAN_STATUS_NOT_EXECUTABLE"
    assert decision.diagnostics["plan_block_reason"] == "STALE_DATA"


def test_economic_trade_gate_allows_executable_status_when_metrics_pass():
    plan = replace(_plan("OPEN_LONG"), status="ALLOW_NORMAL")
    decision = EconomicTradeGate().evaluate(plan, "OPEN_LONG")

    assert decision.allowed is True
    assert decision.reason == "ECONOMIC_GATE_APPROVED"


def test_economic_trade_gate_does_not_block_close_position():
    decision = EconomicTradeGate().evaluate(None, "CLOSE_POSITION")

    assert decision.allowed is True
    assert decision.reason == "CLOSE_POSITION_BYPASSES_ECONOMIC_GATE"


def _plan(action: str) -> TradePlan:
    if action == "OPEN_SHORT":
        stop_price = Decimal("100.50")
        take_profit_price = Decimal("98.90")
    else:
        stop_price = Decimal("99.50")
        take_profit_price = Decimal("101.10")

    return TradePlan(
        symbol="BTCUSDT",
        action=action,
        status="ALLOW_NORMAL",
        strategy_name="tests.Strategy",
        signal_confidence=95,
        quality_score=95,
        entry_reference_price=Decimal("100"),
        quantity=Decimal("1"),
        notional=Decimal("100"),
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        estimated_entry_fee=Decimal("0.04"),
        estimated_exit_fee=Decimal("0.04"),
        estimated_spread_cost=Decimal("0.01"),
        estimated_slippage_cost=Decimal("0.01"),
        estimated_funding_cost=Decimal("0"),
        estimated_round_trip_cost=Decimal("0.10"),
        expected_net_gain=Decimal("1.00"),
        expected_net_loss=Decimal("0.50"),
        reward_risk=Decimal("2.0"),
        size_multiplier=Decimal("1"),
        execution_style="MARKET",
        management_style="PROTECTION_POLICY_ONLY",
        reason="technical signal",
        block_reason=None,
        diagnostics=_diagnostics(round_trip_cost_bps=Decimal("10")),
    )


def _diagnostics(*, round_trip_cost_bps: Decimal) -> dict:
    return {
        "cost_summary": {
            "round_trip_cost_bps": str(round_trip_cost_bps),
            "minimum_profitable_move": "0.10",
        },
        "cost": {
            "round_trip_cost_bps": str(round_trip_cost_bps),
            "minimum_profitable_move": "0.10",
            "components": {
                "entry_fee": "0.04",
                "exit_fee": "0.04",
                "spread_cost": "0.01",
                "slippage_cost": "0.01",
                "funding_cost": "0",
            },
        },
    }
