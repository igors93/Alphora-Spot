"""Tests for the versioned TradingPolicy and its outcome resolution (OPP-022)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from config import ExecutionMode
from config.symbol_context import symbol_config_from_app_config
from domain.candle import Candle
from domain.models import FuturesPosition, PositionDirection
from domain.symbol_rules import SymbolRules
from futures.trading_policy import (
    SL_FIRST,
    TIMEOUT,
    TP_FIRST,
    TradingPolicy,
)
from trading.context import TradeContext
from trading.intent import TradeIntent

D = Decimal


def _bar(open_, high, low, close, index=0):
    return Candle(
        symbol="BTCUSDT",
        interval="15m",
        open_time_ms=(index + 1) * 900_000,
        close_time_ms=(index + 2) * 900_000 - 1,
        open=D(str(open_)),
        high=D(str(high)),
        low=D(str(low)),
        close=D(str(close)),
        volume=D("100"),
        is_closed=True,
    )


def _rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        quantity_step=D("0.001"),
        min_quantity=D("0.001"),
        min_notional=D("5"),
        price_tick=D("0.10"),
    )


def _context(config) -> TradeContext:
    profile = symbol_config_from_app_config(config)
    decision_candle = Candle(
        symbol="BTCUSDT",
        interval="15m",
        open_time_ms=0,
        close_time_ms=899_999,
        open=D("99.5"),
        high=D("100.5"),
        low=D("99.0"),
        close=D("100.0"),
        volume=D("100"),
        is_closed=True,
    )
    return TradeContext(
        symbol="BTCUSDT",
        market_price=D("100"),
        mark_price=D("100"),
        index_price=D("100"),
        spread_pct=D("0.0001"),
        volatility=None,
        atr=D("1.0"),
        funding_rate=D("0.0001"),
        next_funding_time=28_800_000,
        position=FuturesPosition("BTCUSDT", PositionDirection.NONE, D("0"), D("0"), D("0")),
        exchange_rules=_rules(),
        symbol_config=profile,
        portfolio_snapshot={"available_balance_usdt": "1000"},
        data_age_seconds=0.0,
        stale=False,
        candles=(decision_candle,),
    )


def test_trading_policy_version_is_stable_and_mode_independent(config_factory):
    paper_cfg = replace(config_factory(), execution_mode=ExecutionMode.PAPER_LIVE)
    real_cfg = replace(config_factory(), execution_mode=ExecutionMode.REAL_LIVE)

    paper_policy = TradingPolicy.from_config(paper_cfg)
    real_policy = TradingPolicy.from_config(real_cfg)

    assert paper_policy.version.startswith("trading-policy@")
    assert paper_policy.version == real_policy.version

    # Changing any trading rule parameter changes the version hash.
    atr_variant = paper_policy.with_variant(
        "atr_wide",
        exit_model="ATR",
        atr_stop_multiplier=D("2.0"),
        atr_take_multiplier=D("3.0"),
    )
    assert atr_variant.version != paper_policy.version
    assert atr_variant.variant == "atr_wide"


def test_trading_policy_builds_plan_with_policy_version(config_factory):
    config = config_factory()
    policy = TradingPolicy.from_config(config)
    context = _context(config)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 90, "breakout", "tests.Strategy")

    plan = policy.build_plan(intent=intent, context=context)

    assert plan.diagnostics["policy_version"] == policy.version
    assert plan.stop_price == D("99.0")
    assert plan.take_profit_price == D("102.0")


def test_trading_policy_outcome_resolves_tp_sl_and_timeout_with_net_r(config_factory):
    config = config_factory()
    policy = TradingPolicy.from_config(config, max_holding_bars=3)
    context = _context(config)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 90, "breakout", "tests.Strategy")
    opportunity = policy.opportunity_at(intent=intent, context=context)

    assert opportunity.executable is True
    assert opportunity.policy_version == policy.version

    tp_candles = [
        _bar(100, 101, 99.5, 100.8, 0),
        _bar(100.8, 102.2, 100.5, 102.0, 1),
    ]
    tp_outcome = policy.outcome(
        opportunity,
        tp_candles,
        funding_rate=D("0.0001"),
        spread_bps=D("1"),
    )
    assert tp_outcome.outcome == TP_FIRST
    assert tp_outcome.filled is True
    assert tp_outcome.bars_held == 2
    assert tp_outcome.maker_exit is True
    assert tp_outcome.net_r == pytest.approx(opportunity.plan.r_win, abs=D("0.0001"))

    sl_candles = [
        _bar(100, 100.4, 98.8, 99.1, 0),
    ]
    sl_outcome = policy.outcome(
        opportunity,
        sl_candles,
        funding_rate=D("0.0001"),
        spread_bps=D("1"),
    )
    assert sl_outcome.outcome == SL_FIRST
    assert sl_outcome.bars_held == 1
    assert sl_outcome.maker_exit is False
    assert sl_outcome.net_r == pytest.approx(-opportunity.plan.r_loss, abs=D("0.0001"))

    timeout_candles = [
        _bar(100, 100.5, 99.5, 100.1, 0),
        _bar(100.1, 100.6, 99.6, 100.2, 1),
        _bar(100.2, 100.4, 99.7, 100.0, 2),
    ]
    timeout_outcome = policy.outcome(
        opportunity,
        timeout_candles,
        funding_rate=D("0.0001"),
        spread_bps=D("1"),
    )
    assert timeout_outcome.outcome == TIMEOUT
    assert timeout_outcome.bars_held == 3
    assert timeout_outcome.gross_r == D("0")
    assert timeout_outcome.net_r < D("0")


def test_trading_policy_outcome_matches_execution_simulator_for_market_and_limit_paths(
    config_factory,
):
    from backtesting.domain.config import BacktestSymbolConfig
    from backtesting.engine.execution import ExecutionSimulator
    from domain.models import TradeAction
    from futures.performance.cost_model import CostModel as BacktestCostModel

    config = config_factory()
    policy = TradingPolicy.from_config(config, max_holding_bars=4)
    context = _context(config)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 90, "breakout", "tests.Strategy")
    opportunity = policy.opportunity_at(intent=intent, context=context)

    scenarios = [
        # Intrabar take on second bar
        [_bar(100, 101.2, 99.4, 100.8, 0), _bar(100.8, 102.5, 100.2, 102.1, 1)],
        # Intrabar stop on first bar
        [_bar(100, 100.5, 98.8, 99.2, 0)],
        # Gap stop at open of second bar
        [_bar(100, 100.6, 99.4, 99.6, 0), _bar(98.5, 99.2, 98.2, 98.8, 1)],
        # Ambiguous candle resolved conservatively as stop first
        [_bar(100, 103.0, 98.5, 101.0, 0)],
    ]
    for candles in scenarios:
        sim = ExecutionSimulator(
            symbols=[
                BacktestSymbolConfig(
                    symbol="BTCUSDT",
                    csv_path="unused.csv",
                    position_notional_usdt=D("100"),
                    stop_loss_pct=D("0.01"),
                    take_profit_pct=D("0.02"),
                    rules=_rules(),
                )
            ],
            cost_model=BacktestCostModel(),
            same_candle_exit_policy=policy.ambiguity_policy,
        )
        sim.open_position(
            symbol="BTCUSDT",
            action=TradeAction.OPEN_LONG,
            timestamp="2026-11-17T00:15:00+00:00",
            price=candles[0].open,
            reason="breakout",
            stop_price=opportunity.plan.stop_price,
            take_profit_price=opportunity.plan.take_profit_price,
            dynamic_exit_used=True,
            candle_volume=candles[0].volume,
        )
        sim_result = None
        for idx, candle in enumerate(candles):
            sim_result = sim.maybe_close_by_stop_or_take(
                symbol="BTCUSDT",
                candle=candle,
                include_open_gap=(idx > 0),
            )
            if sim_result is not None:
                break

        outcome = policy.outcome(opportunity, candles)
        assert sim_result is not None and sim_result.trade is not None
        assert outcome.exit_price == sim_result.trade.exit_price
        assert outcome.exit_reason == sim_result.trade.exit_reason
        assert outcome.maker_exit == sim_result.trade.exit_maker
