from dataclasses import replace
from decimal import Decimal

import pytest

from backtesting.domain.realism import (
    BacktestFillSettings,
    BacktestValidationSettings,
    FundingRatePoint,
)
from backtesting.engine import run_backtest, run_robust_backtest
from backtesting.engine.decision_pipeline import _limit_touched
from backtesting.infrastructure.candle_loader import load_candles_from_csv
from backtesting.infrastructure.csv_feed import HistoricalDataFeed
from domain.candle import Candle
from domain.models import TradeAction
from tests.backtesting.test_backtesting_engine import (
    OpenLongOnceThenCloseStrategy,
    OpenLongThenCloseStrategy,
    OpenLongWithStrategyExitStrategy,
    _config,
    _patch_strategy,
    _write_candles,
    _write_candles_with_ohlc,
)


def test_next_open_checks_stop_inside_entry_candle(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongOnceThenCloseStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100", "100", "100"),
            ("2", "100", "101", "98", "100"),
        ],
    )
    config = replace(
        _config(
            tmp_path,
            csv_path,
            stop_loss_pct=Decimal("0.01"),
            take_profit_pct=Decimal("0.02"),
        ),
        market_entry_policy="NEXT_OPEN",
    )

    result = run_backtest(config, write_reports=False)

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_price == Decimal("99.0")
    assert result.trades[0].entry_time < result.trades[0].exit_time


def test_gap_stop_fills_at_open_not_at_unreachable_stop(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongWithStrategyExitStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100", "100", "100"),
            ("2", "90", "91", "89", "90"),
        ],
    )
    config = _config(
        tmp_path,
        csv_path,
        stop_loss_pct=Decimal("0.01"),
        take_profit_pct=Decimal("0.02"),
    )

    result = run_backtest(config, write_reports=False)

    assert result.trades[0].exit_reason == "gap_stop_loss"
    assert result.trades[0].exit_price == Decimal("90")
    assert result.summary["execution_diagnostics"]["gap_stop_count"] == 1


def test_fail_if_open_really_fails(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongWithStrategyExitStrategy)
    config = replace(
        _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100"])),
        end_of_data_policy="FAIL_IF_OPEN",
    )

    with pytest.raises(RuntimeError, match="END_OF_DATA_OPEN_POSITION"):
        run_backtest(config, write_reports=False)


def test_single_symbol_cadence_gap_is_detected():
    base = 1_577_836_800_000

    def candle(open_time_ms: int) -> Candle:
        return Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=open_time_ms,
            close_time_ms=open_time_ms + 59_999,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
            is_closed=True,
            source="test",
        )

    diagnostics = HistoricalDataFeed(
        {"BTCUSDT": [candle(base), candle(base + 120_000)]}
    ).diagnostics()

    assert diagnostics.cadence_gaps_by_symbol == {"BTCUSDT": 1}
    assert "CANDLE_CADENCE_GAPS_DETECTED" in diagnostics.quality_warnings


def test_robust_runner_produces_isolated_folds_and_sensitivity(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    config = _config(
        tmp_path,
        _write_candles(tmp_path / "candles.csv", [str(100 + index) for index in range(40)]),
    )

    result = run_robust_backtest(config, write_reports=False)

    assert len(result["folds"]) >= 2
    assert result["parameter_sensitivity"]
    assert result["operational_verdict"] in {
        "ROBUST_BACKTEST_OK",
        "ROBUST_BACKTEST_REJECTED",
    }


def test_out_of_sample_label_without_isolated_window_is_not_trusted(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    config = replace(
        _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "101"])),
        validation_settings=BacktestValidationSettings(evaluation_mode="OUT_OF_SAMPLE"),
    )

    result = run_backtest(config, write_reports=False)

    validation = result.summary["model_data_validation"]
    assert validation["evaluation_window_verified"] is False
    assert validation["out_of_sample_verified"] is False
    assert result.summary["reliability_gates"]["out_of_sample_verified"] is False


def test_limit_maker_requires_one_tick_of_trade_through():
    base = 1_577_836_800_000

    def candle(low: str) -> Candle:
        return Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=base,
            close_time_ms=base + 59_999,
            open=Decimal("101"),
            high=Decimal("102"),
            low=Decimal(low),
            close=Decimal("101"),
            volume=Decimal("10"),
            is_closed=True,
            source="test",
        )

    assert not _limit_touched(
        TradeAction.OPEN_LONG,
        candle("99.95"),
        Decimal("100"),
        require_trade_through=True,
        price_tick=Decimal("0.10"),
    )
    assert _limit_touched(
        TradeAction.OPEN_LONG,
        candle("99.90"),
        Decimal("100"),
        require_trade_through=True,
        price_tick=Decimal("0.10"),
    )


def test_exit_above_liquidity_cap_receives_stressed_impact(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text(
        "timestamp,open,high,low,close,volume\n1,100,100,100,100,1\n2,101,101,101,101,0.01\n",
        encoding="utf-8",
    )
    config = replace(
        _config(tmp_path, csv_path),
        fill_settings=BacktestFillSettings(
            max_volume_participation=Decimal("0.05"),
            reliable_volume_participation=Decimal("0.05"),
            require_limit_trade_through=True,
            market_impact_bps_at_full_participation=Decimal("5"),
        ),
    )

    result = run_backtest(config, write_reports=False)

    diagnostics = result.summary["execution_diagnostics"]
    assert diagnostics["exit_liquidity_breach_count"] == 1
    assert Decimal(diagnostics["max_exit_market_impact_bps"]) > Decimal("100")
    assert result.summary["reliability_gates"]["exit_liquidity_within_cap"] is False


def test_secondary_timeframe_cadence_gap_is_a_data_quality_failure():
    base = 1_577_836_800_000

    def candle(open_time_ms: int, interval: str) -> Candle:
        duration = 300_000 if interval == "5m" else 60_000
        return Candle(
            symbol="BTCUSDT",
            interval=interval,
            open_time_ms=open_time_ms,
            close_time_ms=open_time_ms + duration - 1,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
            is_closed=True,
            source="test",
        )

    feed = HistoricalDataFeed(
        {"BTCUSDT": [candle(base, "1m"), candle(base + 60_000, "1m")]},
        {"BTCUSDT": {"5m": [candle(base, "5m"), candle(base + 600_000, "5m")]}},
    )

    diagnostics = feed.diagnostics()
    assert diagnostics.secondary_timeframes["BTCUSDT"]["5m"]["cadence_gaps_by_symbol"] == {
        "BTCUSDT": 1
    }
    assert any(
        warning.startswith("SECONDARY_CANDLE_CADENCE_GAPS_DETECTED")
        for warning in diagnostics.quality_warnings
    )


def test_cross_timeframe_ohlcv_mismatch_is_reported():
    base = 1_577_836_800_000

    def candle(
        open_time_ms: int,
        interval: str,
        *,
        open_: str,
        high: str,
        low: str,
        close: str,
        volume: str,
    ) -> Candle:
        duration = 300_000 if interval == "5m" else 60_000
        return Candle(
            symbol="BTCUSDT",
            interval=interval,
            open_time_ms=open_time_ms,
            close_time_ms=open_time_ms + duration - 1,
            open=Decimal(open_),
            high=Decimal(high),
            low=Decimal(low),
            close=Decimal(close),
            volume=Decimal(volume),
            is_closed=True,
            source="test",
        )

    primary = [
        candle(
            base + index * 60_000,
            "1m",
            open_=str(100 + index),
            high=str(101 + index),
            low=str(99 + index),
            close=str(100 + index),
            volume="10",
        )
        for index in range(5)
    ]
    inconsistent = candle(
        base,
        "5m",
        open_="100",
        high="999",
        low="99",
        close="104",
        volume="50",
    )

    diagnostics = HistoricalDataFeed(
        {"BTCUSDT": primary},
        {"BTCUSDT": {"5m": [inconsistent]}},
    ).diagnostics()

    consistency = diagnostics.cross_timeframe_consistency["BTCUSDT"]["5m"]
    assert consistency["passed"] is False
    assert consistency["mismatch_count"] == 1
    assert any(
        warning.startswith("CROSS_TIMEFRAME_OHLCV_MISMATCH")
        for warning in diagnostics.quality_warnings
    )


def test_unsupported_csv_timeframe_is_rejected(tmp_path):
    csv_path = _write_candles(tmp_path / "candles.csv", ["100", "101"])

    with pytest.raises(ValueError, match="Timeframe nao suportado"):
        load_candles_from_csv(str(csv_path), interval="2m", symbol="BTCUSDT")


def test_missing_funding_boundary_blocks_reliability(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text(
        "timestamp,open,high,low,close,volume\n0,100,100,100,100,10\n480,100,100,100,100,10\n",
        encoding="utf-8",
    )
    config = _config(tmp_path, csv_path)

    missing = run_backtest(config, write_reports=False)

    diagnostics = missing.summary["execution_diagnostics"]
    assert diagnostics["funding_expected_periods"] == 1
    assert diagnostics["funding_missing_periods"] == 1
    assert missing.summary["reliability_gates"]["funding_coverage_passed"] is False

    covered = run_backtest(
        replace(
            config,
            funding_rates_by_symbol={
                "BTCUSDT": (FundingRatePoint("2020-01-01T08:00:00Z", Decimal("0.001")),)
            },
        ),
        write_reports=False,
    )

    covered_diagnostics = covered.summary["execution_diagnostics"]
    assert covered_diagnostics["funding_expected_periods"] == 1
    assert covered_diagnostics["funding_missing_periods"] == 0
    assert covered.trades[0].funding_cost > 0
