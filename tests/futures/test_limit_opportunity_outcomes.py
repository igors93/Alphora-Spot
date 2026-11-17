"""Tests for limit-entry opportunities, NOT_FILLED outcomes and p_fill (OPP-024)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from config.symbol_context import symbol_config_from_app_config
from domain.candle import Candle
from domain.models import FuturesPosition, PositionDirection
from domain.symbol_rules import SymbolRules
from futures.trading_policy import (
    NOT_FILLED,
    OPEN,
    SL_FIRST,
    TIMEOUT,
    TP_FIRST,
    TradingPolicy,
    summarize_outcomes,
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
        funding_rate=D("0"),
        next_funding_time=28_800_000,
        position=FuturesPosition("BTCUSDT", PositionDirection.NONE, D("0"), D("0"), D("0")),
        exchange_rules=_rules(),
        symbol_config=profile,
        portfolio_snapshot={"available_balance_usdt": "1000"},
        data_age_seconds=0.0,
        stale=False,
        candles=(decision_candle,),
    )


def test_limit_policy_records_not_filled_when_price_does_not_touch_within_timeout(config_factory):
    config = config_factory()
    policy = TradingPolicy.from_config(
        config,
        entry_order_type="LIMIT",
        limit_entry_timeout_candles=2,
        limit_fallback_to_market=False,
        require_limit_trade_through=False,
        max_holding_bars=4,
    )
    context = _context(config)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 90, "pullback_limit", "tests.Strategy")
    opportunity = policy.opportunity_at(intent=intent, context=context, limit_price=D("99.50"))

    assert opportunity.executable is True
    assert opportunity.plan.entry_order_type == "LIMIT"
    assert opportunity.plan.limit_price == D("99.50")

    # 1. Only 1 candle available (< timeout=2) and not touched -> OPEN / LIMIT_PENDING
    pending = policy.outcome(opportunity, [_bar(100, 101, 99.6, 100.5, 0)])
    assert pending.outcome == OPEN
    assert pending.filled is False
    assert pending.fill_status == "LIMIT_PENDING"

    # 2. Both timeout candles stay above 99.50 -> NOT_FILLED / LIMIT_EXPIRED
    expired = policy.outcome(
        opportunity,
        [
            _bar(100, 101, 99.6, 100.5, 0),
            _bar(100.5, 101.2, 99.7, 101.0, 1),
            _bar(101.0, 103.0, 99.0, 102.5, 2),
        ],
    )
    assert expired.outcome == NOT_FILLED
    assert expired.filled is False
    assert expired.fill_status == "LIMIT_EXPIRED"
    assert expired.wait_candles == 2
    assert expired.bars_held == 0
    assert expired.net_r == D("0")


def test_limit_policy_resolves_same_candle_and_multi_candle_fills(config_factory):
    config = config_factory()
    policy = TradingPolicy.from_config(
        config,
        entry_order_type="LIMIT",
        limit_entry_timeout_candles=2,
        limit_fallback_to_market=False,
        require_limit_trade_through=False,
        max_holding_bars=4,
    )
    context = _context(config)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 90, "pullback_limit", "tests.Strategy")
    opportunity = policy.opportunity_at(intent=intent, context=context, limit_price=D("99.80"))
    stop = opportunity.plan.stop_price
    take = opportunity.plan.take_profit_price

    # 1. Fills on candle 0 and hits stop on same candle
    same_bar_stop = policy.outcome(
        opportunity,
        [_bar(100, 100.2, stop - D("0.10"), 99.5, 0)],
    )
    assert same_bar_stop.outcome == SL_FIRST
    assert same_bar_stop.filled is True
    assert same_bar_stop.fill_status == "LIMIT_FILLED"
    assert same_bar_stop.entry_maker is True
    assert same_bar_stop.bars_held == 1
    assert same_bar_stop.exit_reason == "limit_entry_same_candle_stop_loss"

    # 2. Fills on candle 0 and confirms take at close on same candle
    same_bar_take = policy.outcome(
        opportunity,
        [_bar(100, take + D("0.20"), 99.70, take + D("0.10"), 0)],
    )
    assert same_bar_take.outcome == TP_FIRST
    assert same_bar_take.filled is True
    assert same_bar_take.maker_exit is True
    assert same_bar_take.bars_held == 1
    assert same_bar_take.exit_reason == "limit_entry_same_candle_take_profit_confirmed"

    # 3. Fills on candle 0 with unconfirmed intrabar high, then hits take on candle 1
    multi_bar_take = policy.outcome(
        opportunity,
        [
            _bar(100, take + D("0.20"), 99.70, take - D("0.30"), 0),
            _bar(take - D("0.30"), take + D("0.20"), 100.0, take, 1),
        ],
    )
    assert multi_bar_take.outcome == TP_FIRST
    assert multi_bar_take.bars_held == 2
    assert multi_bar_take.exit_index == 1


def test_limit_policy_with_market_fallback_fills_at_timeout_close(config_factory):
    config = config_factory()
    policy = TradingPolicy.from_config(
        config,
        entry_order_type="LIMIT",
        limit_entry_timeout_candles=1,
        limit_fallback_to_market=True,
        require_limit_trade_through=False,
        max_holding_bars=2,
    )
    context = _context(config)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 90, "pullback_limit", "tests.Strategy")
    opportunity = policy.opportunity_at(intent=intent, context=context, limit_price=D("99.50"))
    take = opportunity.plan.take_profit_price

    outcome = policy.outcome(
        opportunity,
        [
            _bar(100, 100.4, 99.7, 100.1, 0),
            _bar(100.1, take + D("0.20"), 99.9, take, 1),
        ],
    )
    assert outcome.filled is True
    assert outcome.fill_status == "LIMIT_FALLBACK_TO_MARKET"
    assert outcome.entry_maker is False
    assert outcome.entry_price == D("100.1")
    assert outcome.outcome == TP_FIRST


def test_summarize_outcomes_and_expected_r_with_fill_probability(config_factory):
    config = config_factory()
    policy = TradingPolicy.from_config(
        config,
        entry_order_type="LIMIT",
        limit_entry_timeout_candles=1,
        limit_fallback_to_market=False,
        require_limit_trade_through=False,
        max_holding_bars=2,
    )
    context = _context(config)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 90, "pullback_limit", "tests.Strategy")
    opportunity = policy.opportunity_at(intent=intent, context=context, limit_price=D("99.80"))
    stop = opportunity.plan.stop_price
    take = opportunity.plan.take_profit_price

    outcomes = [
        # 1. NOT_FILLED
        policy.outcome(opportunity, [_bar(100, 100.5, 99.9, 100.2, 0)]),
        # 2. TP_FIRST
        policy.outcome(
            opportunity,
            [
                _bar(100, 100.2, 99.75, 100.0, 0),
                _bar(100, take + D("0.20"), 99.9, take, 1),
            ],
        ),
        # 3. SL_FIRST
        policy.outcome(
            opportunity,
            [
                _bar(100, 100.2, 99.75, 99.9, 0),
                _bar(99.9, 100.0, stop - D("0.10"), stop, 1),
            ],
        ),
        # 4. TIMEOUT
        policy.outcome(
            opportunity,
            [
                _bar(100, 100.2, 99.75, 99.9, 0),
                _bar(99.9, 100.1, 99.6, 99.7, 1),
            ],
        ),
    ]
    dist = summarize_outcomes(outcomes)

    assert outcomes[3].outcome == TIMEOUT
    assert dist.total_resolved == 4
    assert dist.filled_count == 3
    assert dist.not_filled_count == 1
    assert dist.p_fill == D("3") / D("4")
    assert dist.p_tp + dist.p_sl + dist.p_timeout == D("1")
    assert dist.mean_r_timeout < D("0")

    costs = policy.outcome_costs(
        direction="LONG",
        entry_price=D("99.80"),
        stop_price=stop,
        take_price=take,
        context=context,
        market_entry=False,
    )
    assert costs is not None
    ev_without_fill = costs.expected_r(
        p_take=dist.p_tp,
        p_stop=dist.p_sl,
        p_timeout=dist.p_timeout,
        r_timeout=dist.mean_r_timeout,
    )
    ev_with_fill = costs.expected_r(
        p_take=dist.p_tp,
        p_stop=dist.p_sl,
        p_timeout=dist.p_timeout,
        r_timeout=dist.mean_r_timeout,
        p_fill=dist.p_fill,
    )
    assert ev_with_fill == pytest.approx(dist.p_fill * ev_without_fill, abs=D("1e-9"))
