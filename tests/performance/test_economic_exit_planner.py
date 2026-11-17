from dataclasses import dataclass, replace
from decimal import Decimal

from config import ExitModelType
from config.symbol_context import symbol_config_from_app_config
from domain.models import FuturesPosition, PositionDirection
from futures.costs.model import CostEstimate
from futures.planning.exit_planner import ExitPlanner
from trading.context import TradeContext
from trading.intent import TradeIntent


@dataclass(frozen=True)
class Candle:
    high: Decimal
    low: Decimal
    close: Decimal


def test_exit_planner_fixed_pct_with_sufficient_take_passes_without_adjustment(
    config_factory,
    symbol_rules,
):
    context = _context(config_factory(), symbol_rules)
    plan = ExitPlanner().plan(
        intent=_intent("OPEN_LONG"),
        context=context,
        quantity=Decimal("1"),
        cost=_cost(round_trip=Decimal("0.10")),
    )

    assert plan.take_profit_price == Decimal("102.00")
    assert plan.diagnostics["economic_exit_adjusted"] is False
    assert plan.block_reason is None
    assert plan.reward_risk >= Decimal("1.5")


def test_exit_planner_fixed_pct_adjusts_insufficient_take_to_economic_minimum(
    config_factory,
    symbol_rules,
):
    config = replace(config_factory(), stop_loss_pct=0.003, take_profit_pct=0.004)
    context = _context(config, symbol_rules)

    plan = ExitPlanner().plan(
        intent=_intent("OPEN_LONG"),
        context=context,
        quantity=Decimal("1"),
        cost=_cost(round_trip=Decimal("0.13")),
    )

    assert plan.stop_price == Decimal("99.70")
    assert plan.take_profit_price == Decimal("100.80")
    assert plan.expected_net_gain == Decimal("0.67")
    assert plan.expected_net_loss == Decimal("0.43")
    assert plan.reward_risk == plan.expected_net_gain / plan.expected_net_loss
    assert plan.diagnostics["economic_exit_adjusted"] is True
    assert plan.diagnostics["economic_exit_adjustment_reason"] == (
        "TAKE_PROFIT_RAISED_TO_ECONOMIC_MINIMUM"
    )


def test_exit_planner_fixed_pct_blocks_unrealistic_economic_take(
    config_factory,
    symbol_rules,
):
    config = replace(config_factory(), stop_loss_pct=0.003, take_profit_pct=0.004)
    context = _context(config, symbol_rules)
    planner = ExitPlanner(max_economic_take_bps=Decimal("20"))

    plan = planner.plan(
        intent=_intent("OPEN_LONG"),
        context=context,
        quantity=Decimal("1"),
        cost=_cost(round_trip=Decimal("0.13")),
    )

    assert plan.take_profit_price is None
    assert plan.block_reason == "ECONOMIC_TAKE_REQUIRED_TOO_FAR"
    assert plan.diagnostics["economic_exit_blocked_unrealistic_take"] is True
    assert plan.diagnostics["block_reason"] == "ECONOMIC_TAKE_REQUIRED_TOO_FAR"


def test_exit_planner_atr_uses_available_atr(config_factory, symbol_rules):
    config = replace(config_factory(), exit_model=ExitModelType.ATR, atr_period=2)
    context = replace(_context(config, symbol_rules), atr=Decimal("1"))
    planner = ExitPlanner(min_net_reward_risk=Decimal("1"))

    plan = planner.plan(
        intent=_intent("OPEN_LONG"),
        context=context,
        quantity=Decimal("1"),
        cost=_cost(round_trip=Decimal("0.10")),
    )

    assert plan.stop_price == Decimal("98.50")
    assert plan.take_profit_price == Decimal("102.00")
    assert plan.diagnostics["atr_available"] is True
    assert plan.diagnostics["exit_model_effective"] == "ATR"


def test_exit_planner_atr_falls_back_to_fixed_pct_without_atr(
    config_factory,
    symbol_rules,
):
    config = replace(config_factory(), exit_model=ExitModelType.ATR, atr_period=2)
    context = _context(config, symbol_rules)

    plan = ExitPlanner().plan(
        intent=_intent("OPEN_LONG"),
        context=context,
        quantity=Decimal("1"),
        cost=_cost(round_trip=Decimal("0.10")),
    )

    assert plan.diagnostics["atr_available"] is False
    assert plan.diagnostics["atr_fallback_reason"] == "ATR_NOT_AVAILABLE_IN_CONTEXT"
    assert plan.diagnostics["exit_model_effective"] == "FIXED_PCT_FALLBACK"


def test_exit_planner_atr_calculates_from_context_candles(config_factory, symbol_rules):
    config = replace(config_factory(), exit_model=ExitModelType.ATR, atr_period=2)
    context = replace(
        _context(config, symbol_rules),
        candles=(
            Candle(Decimal("101"), Decimal("99"), Decimal("100")),
            Candle(Decimal("103"), Decimal("100"), Decimal("102")),
            Candle(Decimal("104"), Decimal("101"), Decimal("103")),
        ),
    )
    planner = ExitPlanner(atr_period=2, min_net_reward_risk=Decimal("1"))

    plan = planner.plan(
        intent=_intent("OPEN_LONG"),
        context=context,
        quantity=Decimal("1"),
        cost=_cost(round_trip=Decimal("0.10")),
    )

    assert plan.diagnostics["atr_available"] is True
    assert plan.diagnostics["atr_source"] == "trade_context_candles"
    assert plan.diagnostics["exit_model_effective"] == "ATR"
    assert plan.stop_price == Decimal("95.50")


def test_exit_planner_keeps_stop_and_take_on_correct_side_for_short(
    config_factory,
    symbol_rules,
):
    context = _context(config_factory(), symbol_rules)

    plan = ExitPlanner().plan(
        intent=_intent("OPEN_SHORT"),
        context=context,
        quantity=Decimal("1"),
        cost=_cost(round_trip=Decimal("0.10")),
    )

    assert plan.stop_price > context.market_price
    assert plan.take_profit_price < context.market_price
    assert plan.expected_net_gain == (
        abs(plan.take_profit_price - context.market_price) - Decimal("0.10")
    )


def _context(config, symbol_rules) -> TradeContext:
    return TradeContext(
        symbol="BTCUSDT",
        market_price=Decimal("100"),
        mark_price=Decimal("100"),
        index_price=None,
        spread_pct=None,
        volatility=None,
        atr=None,
        funding_rate=None,
        next_funding_time=None,
        position=FuturesPosition(
            "BTCUSDT",
            PositionDirection.NONE,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        ),
        exchange_rules=symbol_rules,
        symbol_config=symbol_config_from_app_config(config),
        portfolio_snapshot={"available_balance_usdt": "1000"},
        data_age_seconds=0.0,
        stale=False,
    )


def _intent(action: str) -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT",
        action=action,
        confidence=95,
        reason="ready",
        strategy_name="tests.Strategy",
    )


def _cost(*, round_trip: Decimal) -> CostEstimate:
    return CostEstimate(
        entry_fee=round_trip / Decimal("4"),
        exit_fee=round_trip / Decimal("4"),
        spread_cost=round_trip / Decimal("4"),
        slippage_cost=round_trip / Decimal("4"),
        funding_cost=Decimal("0"),
        round_trip_cost=round_trip,
        minimum_profitable_move=round_trip,
        notional=Decimal("100"),
        round_trip_cost_bps=round_trip * Decimal("100"),
    )
