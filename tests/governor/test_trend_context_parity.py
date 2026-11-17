"""IA0.4: the regime gate sees the same trend in the Governor and in the backtest."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from backtesting.adapters.signal import HistoricalSignalPort
from backtesting.domain.config import CandleEvent
from domain.models import MarketSnapshot
from futures.trend import TrendContextEngine
from governor.services.trend_context import TrendContextService
from tests.fakes import candles_from_closes

INTERVAL = "15m"


def _candles(count: int = 140) -> list:
    closes = [100 + index * 0.35 + (1.5 if index % 7 == 0 else 0.0) for index in range(count)]
    return candles_from_closes(closes, interval=INTERVAL)


def _live(engine: TrendContextEngine, market: MarketSnapshot):
    service = TrendContextService(
        config=SimpleNamespace(trend_context_enabled=True, trend_context_mode="OBSERVE"),
        engine=engine,
        journal=SimpleNamespace(append=lambda *args, **kwargs: None),
        timeframe_for=lambda _symbol: INTERVAL,
    )
    return service.for_market(symbol="BTCUSDT", market=market, timestamp=1.0)


def _backtest(engine: TrendContextEngine, candles: list):
    port = HistoricalSignalPort(
        runtime=SimpleNamespace(),
        execution=SimpleNamespace(),
        clock=SimpleNamespace(),
        trend_engine=engine,
        trend_enabled=True,
        trend_mode="OBSERVE",
        price_window=300,
    )
    event = CandleEvent(
        timestamp=candles[-1].timestamp,
        symbol="BTCUSDT",
        candle=candles[-1],
        candle_snapshot={INTERVAL: tuple(candles)},
    )
    return port._trend_context(event)


def _market(candles: list, prices: list[Decimal]) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        price=candles[-1].close,
        prices=prices,
        candles={INTERVAL: tuple(candles)},
    )


def test_live_and_backtest_compute_the_same_trend_from_the_same_candles():
    engine = TrendContextEngine()
    candles = _candles()

    live = _live(engine, _market(candles, [candle.close for candle in candles]))
    backtest = _backtest(engine, candles)

    assert backtest is not None
    assert live is not None
    for field in ("direction", "strength", "regime", "data_points"):
        assert getattr(live, field) == getattr(backtest, field), field
    assert live.fast_ma_slope == backtest.fast_ma_slope
    assert live.chop_score == backtest.chop_score


def test_live_trend_does_not_depend_on_how_often_the_symbol_was_sampled():
    engine = TrendContextEngine()
    candles = _candles()
    sparse = [candles[-1].close] * 3
    dense = [candles[-1].close + Decimal(index % 5) for index in range(120)]

    first = _live(engine, _market(candles, sparse))
    second = _live(engine, _market(candles, dense))

    assert first is not None and second is not None
    assert first.to_dict()["regime"] == second.to_dict()["regime"]
    assert first.fast_ma_slope == second.fast_ma_slope
    assert first.data_points == second.data_points == engine.required_price_window()


def test_live_trend_without_candles_reports_low_data():
    engine = TrendContextEngine()
    market = MarketSnapshot(symbol="BTCUSDT", price=Decimal("100"), prices=[Decimal("100")] * 120)

    context = _live(engine, market)

    assert context is not None
    assert context.regime.value == "LOW_DATA"
