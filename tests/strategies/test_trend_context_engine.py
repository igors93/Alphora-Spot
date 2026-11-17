import json
from decimal import Decimal

from futures.trend import (
    MarketRegime,
    TrendContextEngine,
    TrendDirection,
    trend_context_to_features,
)


def _engine() -> TrendContextEngine:
    return TrendContextEngine(
        fast_ma_period=2,
        slow_ma_period=3,
        slope_lookback=1,
        volatility_lookback=4,
        high_volatility_threshold_pct=10.0,
        chop_threshold=0.55,
    )


def test_trend_context_low_data_returns_safe_context():
    context = _engine().evaluate(symbol="BTCUSDT", prices=[Decimal("100")], timestamp="1")

    assert context.regime is MarketRegime.LOW_DATA
    assert context.direction is TrendDirection.UNKNOWN
    assert context.confidence < 0.10


def test_trend_context_required_price_window_is_sufficient():
    engine = TrendContextEngine(
        fast_ma_period=20,
        slow_ma_period=50,
        slope_lookback=5,
        volatility_lookback=20,
    )

    assert engine.required_price_window() >= 70


def test_trend_context_long_history_matches_recent_required_window():
    engine = _engine()
    prices = [Decimal(str(100 + index)) for index in range(80)]
    full = engine.evaluate(symbol="BTCUSDT", prices=prices)
    recent = engine.evaluate(
        symbol="BTCUSDT",
        prices=prices[-engine.required_price_window() :],
    )

    assert full.regime is recent.regime
    assert full.direction is recent.direction
    assert abs(full.confidence - recent.confidence) <= 0.000001


def test_trend_context_detects_clear_uptrend():
    context = _engine().evaluate(
        symbol="BTCUSDT",
        prices=[Decimal("100"), Decimal("101"), Decimal("102"), Decimal("103")],
    )

    assert context.direction is TrendDirection.UP
    assert context.regime is MarketRegime.TREND_UP
    assert context.price_above_fast_ma is True


def test_trend_context_detects_clear_downtrend():
    context = _engine().evaluate(
        symbol="BTCUSDT",
        prices=[Decimal("103"), Decimal("102"), Decimal("101"), Decimal("100")],
    )

    assert context.direction is TrendDirection.DOWN
    assert context.regime is MarketRegime.TREND_DOWN
    assert context.price_above_fast_ma is False


def test_trend_context_lateral_market_returns_range_or_chop():
    context = _engine().evaluate(
        symbol="BTCUSDT",
        prices=[
            Decimal("100"),
            Decimal("100.1"),
            Decimal("99.9"),
            Decimal("100.05"),
            Decimal("99.95"),
            Decimal("100.02"),
        ],
    )

    assert context.regime in {MarketRegime.RANGE, MarketRegime.CHOP}
    assert context.direction in {TrendDirection.SIDEWAYS, TrendDirection.UNKNOWN}


def test_trend_context_high_volatility_is_reported():
    engine = TrendContextEngine(
        fast_ma_period=2,
        slow_ma_period=3,
        slope_lookback=1,
        volatility_lookback=4,
        high_volatility_threshold_pct=2.0,
    )

    context = engine.evaluate(
        symbol="BTCUSDT",
        prices=[
            Decimal("100"),
            Decimal("112"),
            Decimal("90"),
            Decimal("118"),
            Decimal("85"),
            Decimal("122"),
        ],
    )

    assert context.regime is MarketRegime.HIGH_VOLATILITY
    assert context.diagnostics["volatility_state"] == "HIGH_VOLATILITY"


def test_high_volatility_directional_context_keeps_directional_regime():
    engine = TrendContextEngine(
        fast_ma_period=2,
        slow_ma_period=3,
        slope_lookback=1,
        volatility_lookback=4,
        high_volatility_threshold_pct=1.0,
    )

    context = engine.evaluate(
        symbol="BTCUSDT",
        prices=[
            Decimal("100"),
            Decimal("102"),
            Decimal("104"),
            Decimal("112"),
            Decimal("118"),
            Decimal("126"),
        ],
    )

    assert context.regime is MarketRegime.HIGH_VOLATILITY
    assert context.direction is TrendDirection.UP
    assert context.diagnostics["volatility_state"] == "HIGH_VOLATILITY"
    assert context.diagnostics["directional_regime"] == "TREND_UP"
    assert context.diagnostics["is_directional"] is True


def test_trend_context_to_dict_and_features_are_json_serializable():
    context = _engine().evaluate(
        symbol="BTCUSDT",
        prices=[Decimal("100"), Decimal("101"), Decimal("102"), Decimal("103")],
    )

    payload = context.to_dict()
    features = trend_context_to_features(context)

    json.dumps(payload)
    json.dumps(features)
    assert "trend_direction_encoded" in features
    assert "is_trend_up" in features
