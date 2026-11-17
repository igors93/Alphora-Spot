from decimal import Decimal
from pathlib import Path

import pytest

from research.binance_klines_downloader import (
    BinanceKlinesDownloadError,
    build_download_request,
    default_output_path,
    klines_to_candles,
    write_candles_csv,
)
from research.data_loader import load_candles_from_csv


def test_klines_to_candles_converts_binance_payload():
    candles = klines_to_candles(
        [
            [
                1767225600000,
                "100.1",
                "101.2",
                "99.3",
                "100.5",
                "123.45",
                1767225659999,
            ]
        ]
    )

    assert len(candles) == 1
    assert candles[0].timestamp == "2026-01-01T00:00:00Z"
    assert candles[0].open == Decimal("100.1")
    assert candles[0].high == Decimal("101.2")
    assert candles[0].low == Decimal("99.3")
    assert candles[0].close == Decimal("100.5")
    assert candles[0].volume == Decimal("123.45")


def test_klines_to_candles_rejects_invalid_payload():
    with pytest.raises(BinanceKlinesDownloadError):
        klines_to_candles([[1767225600000, "100"]])


def test_write_candles_csv_uses_backtest_loader_format(tmp_path: Path):
    candles = klines_to_candles([[1767225600000, "100", "101", "99", "100.5", "10"]])
    output_path = tmp_path / "market" / "BTCUSDT_1m.csv"

    write_candles_csv(candles, output_path)
    loaded = load_candles_from_csv(str(output_path))

    assert output_path.read_text(encoding="utf-8").splitlines()[0] == (
        "timestamp,open,high,low,close,volume"
    )
    assert loaded[0].timestamp == "2026-01-01T00:00:00Z"
    assert loaded[0].close == Decimal("100.5")


def test_build_download_request_rejects_invalid_dates():
    with pytest.raises(ValueError, match="data inicial"):
        build_download_request(
            symbol="BTCUSDT",
            interval="1m",
            start="2026-02-01",
            end="2026-01-01",
        )


def test_build_download_request_rejects_empty_symbol_and_interval():
    with pytest.raises(ValueError, match="symbol"):
        build_download_request(symbol="", interval="1m", start="2026-01-01", end="2026-01-02")

    with pytest.raises(ValueError, match="interval"):
        build_download_request(symbol="BTCUSDT", interval="", start="2026-01-01", end="2026-01-02")


def test_default_output_path_matches_expected_format():
    path = default_output_path(
        symbol="btcusdt",
        interval="1m",
        start="2026-01-01",
        end="2026-01-31",
    )

    assert path == Path("data") / "market" / "BTCUSDT_1m_2026-01-01_2026-01-31.csv"


def test_drop_unclosed_klines_removes_forming_candle():
    from research.binance_klines_downloader import drop_unclosed_klines

    now_ms = 1_767_225_600_000 + 90_000
    closed = [1_767_225_600_000, "1", "1", "1", "1", "1", 1_767_225_659_999]
    forming = [1_767_225_660_000, "1", "1", "1", "1", "1", 1_767_225_719_999]

    assert drop_unclosed_klines([closed, forming], now_ms=now_ms) == [closed]
    # Payloads without close_time are kept untouched.
    assert drop_unclosed_klines([closed[:6]], now_ms=now_ms) == [closed[:6]]
