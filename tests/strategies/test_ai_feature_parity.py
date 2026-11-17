"""IA0.1: training, backtest and live compute the same AI features from the same candles."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backtesting.infrastructure.csv_feed import HistoricalDataFeed
from domain.ai.model import AI_FEATURES, AI_REQUIRED_HISTORY
from domain.ai.probabilistic import predict_probabilistic_event
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection
from research.ai.model import save_ai_model
from research.ai.probabilistic import (
    PROBABILISTIC_SCOPE_GLOBAL,
    _probabilistic_training_rows_from_values,
    train_probabilistic_linear_ai_model_from_price_series,
)
from research.data_loader import load_candles_from_csv
from strategies.futures.ai.probabilistic_strategy import ProbabilisticAIStrategy

INTERVAL = "15m"
HORIZON = 3


def _write_csv(path: Path, rows: int = 160) -> Path:
    """Candles with a real range, body, close location and changing volume."""

    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    lines = ["timestamp,open,high,low,close,volume"]
    close = 100.0
    for index in range(rows):
        open_ = close
        close = open_ * (1.0 + 0.004 * math.sin(index / 3.0) + 0.0007 * ((index % 5) - 2))
        high = max(open_, close) * (1.0 + 0.0015 + 0.0005 * (index % 3))
        low = min(open_, close) * (1.0 - 0.0012 - 0.0004 * (index % 4))
        volume = 1000.0 + 150.0 * math.cos(index / 2.0) + 40.0 * (index % 7)
        timestamp = (base + timedelta(minutes=15 * index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{timestamp},{open_:.6f},{high:.6f},{low:.6f},{close:.6f},{volume:.4f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def candles(tmp_path: Path) -> list:
    return load_candles_from_csv(str(_write_csv(tmp_path / "btc.csv")), interval=INTERVAL)


@pytest.fixture
def strategy(tmp_path: Path, candles: list) -> ProbabilisticAIStrategy:
    model = train_probabilistic_linear_ai_model_from_price_series(
        [candles],
        symbol="GLOBAL",
        interval=INTERVAL,
        scope=PROBABILISTIC_SCOPE_GLOBAL,
        horizon_candles=HORIZON,
        threshold_bps=2.0,
        estimated_round_trip_cost_bps=1.0,
        min_training_rows=20,
        trained_symbols=["BTCUSDT"],
    )
    path = save_ai_model(model, tmp_path / "global.json")
    return ProbabilisticAIStrategy.from_params({"global_model_path": str(path)})


def _flat() -> FuturesPosition:
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def _strategy_features(strategy: ProbabilisticAIStrategy, candle_series) -> dict:
    market = MarketSnapshot(
        symbol="BTCUSDT",
        price=candle_series[-1].close,
        prices=[candle.close for candle in candle_series],
        candles={INTERVAL: tuple(candle_series)},
    )
    score = strategy.score_symbol("BTCUSDT", market, _flat(), {})
    assert score.metadata["model_available"] is True
    prediction = strategy._predict(market)
    assert prediction["data_source"] == f"candle_ohlcv_{INTERVAL}"
    return prediction["features"]


def test_training_backtest_and_live_features_are_identical(candles, strategy):
    rows, _long, _short, _returns = _probabilistic_training_rows_from_values(
        candles, horizon_candles=HORIZON, threshold_bps=2.0
    )
    feed = HistoricalDataFeed({"BTCUSDT": candles})

    for offset, index in enumerate(range(AI_REQUIRED_HISTORY - 1, len(candles) - HORIZON)):
        training = rows[offset]
        live = _strategy_features(strategy, candles[: index + 1])
        snapshot = feed.candle_snapshot_at(
            "BTCUSDT", candles[index].close_time_ms, include_primary=True
        )
        backtest = _strategy_features(strategy, snapshot[INTERVAL])

        for feature in AI_FEATURES:
            assert live[feature] == pytest.approx(training[feature], abs=1e-12), (index, feature)
            assert backtest[feature] == pytest.approx(training[feature], abs=1e-12), (
                index,
                feature,
            )


def test_ohlcv_features_are_informative_in_the_live_path(candles, strategy):
    features = _strategy_features(strategy, candles[:60])

    for feature in ("range_pct_1", "body_pct_1", "close_location_1", "volume_zscore_20"):
        assert features[feature] != 0.0, feature


def test_prediction_refuses_bare_prices(candles, strategy):
    model = strategy.model_resolver.resolve("BTCUSDT").model

    prediction = predict_probabilistic_event(model, [candle.close for candle in candles[:40]])

    assert prediction["reason"] == "AI_OHLCV_REQUIRED"
    assert prediction["action_candidate"] == "NO_ACTION"


def test_strategy_without_candles_does_not_fall_back_to_prices(strategy):
    prices = [Decimal("100") + Decimal(index) / 10 for index in range(40)]
    market = MarketSnapshot(symbol="BTCUSDT", price=prices[-1], prices=prices)

    score = strategy.score_symbol("BTCUSDT", market, _flat(), {})

    assert score.reason == "AI_OHLCV_UNAVAILABLE"
    assert score.action == "NO_ACTION"


def test_training_refuses_bare_prices():
    with pytest.raises(ValueError, match="OHLCV"):
        train_probabilistic_linear_ai_model_from_price_series(
            [[100.0 + index for index in range(200)]],
            symbol="GLOBAL",
            interval=INTERVAL,
            horizon_candles=HORIZON,
            threshold_bps=2.0,
            estimated_round_trip_cost_bps=1.0,
            min_training_rows=20,
        )
