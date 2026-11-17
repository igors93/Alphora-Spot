"""Tests for multi-timeframe HistoricalDataFeed and CandleEvent.candle_snapshot."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backtesting.domain.config import BacktestSymbolConfig
from backtesting.infrastructure.csv_feed import HistoricalDataFeed
from domain.symbol_rules import SymbolRules

_BASE_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z UTC
_1M_MS = 60_000
_5M_MS = 300_000


def _symbol_rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_tick=Decimal("0.01"),
    )


def _write_1m_csv(path: Path, rows: int = 10, base_ms: int = _BASE_MS) -> None:
    base = datetime.fromtimestamp(base_ms / 1000, tz=timezone.utc)
    lines = ["timestamp,open,high,low,close,volume"]
    for i in range(rows):
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        price = 100 + i * 0.1
        lines.append(f"{ts},{price},{price + 0.5},{price - 0.5},{price},10")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_5m_csv(path: Path, rows: int = 5, base_ms: int = _BASE_MS) -> None:
    base = datetime.fromtimestamp(base_ms / 1000, tz=timezone.utc)
    lines = ["timestamp,open,high,low,close,volume"]
    for i in range(rows):
        ts = (base + timedelta(minutes=i * 5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        price = 200 + i * 0.5
        lines.append(f"{ts},{price},{price + 1},{price - 1},{price},50")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# BacktestSymbolConfig backward compatibility
# ---------------------------------------------------------------------------


def test_symbol_config_without_secondary_csvs_defaults_to_none(tmp_path):
    csv = tmp_path / "btc.csv"
    _write_1m_csv(csv)
    cfg = BacktestSymbolConfig(symbol="BTCUSDT", csv_path=str(csv), rules=_symbol_rules())
    assert cfg.csv_paths_by_timeframe is None


def test_symbol_config_with_secondary_csvs(tmp_path):
    csv_1m = tmp_path / "btc_1m.csv"
    csv_5m = tmp_path / "btc_5m.csv"
    _write_1m_csv(csv_1m)
    _write_5m_csv(csv_5m)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
        csv_paths_by_timeframe={"5m": str(csv_5m)},
    )
    assert cfg.csv_paths_by_timeframe == {"5m": str(csv_5m)}


# ---------------------------------------------------------------------------
# HistoricalDataFeed secondary candle loading
# ---------------------------------------------------------------------------


def test_feed_loads_secondary_candles_via_from_symbol_configs(tmp_path):
    csv_1m = tmp_path / "btc_1m.csv"
    csv_5m = tmp_path / "btc_5m.csv"
    _write_1m_csv(csv_1m)
    _write_5m_csv(csv_5m)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
        csv_paths_by_timeframe={"5m": str(csv_5m)},
    )

    feed = HistoricalDataFeed.from_symbol_configs([cfg])

    assert "BTCUSDT" in feed._secondary_candles
    assert "5m" in feed._secondary_candles["BTCUSDT"]
    assert len(feed._secondary_candles["BTCUSDT"]["5m"]) == 5


def test_feed_without_secondary_has_empty_secondary(tmp_path):
    csv_1m = tmp_path / "btc_1m.csv"
    _write_1m_csv(csv_1m)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
    )

    feed = HistoricalDataFeed.from_symbol_configs([cfg])

    assert feed._secondary_candles == {}


def test_feed_raises_on_missing_secondary_csv(tmp_path):
    csv_1m = tmp_path / "btc_1m.csv"
    _write_1m_csv(csv_1m)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
        csv_paths_by_timeframe={"5m": str(tmp_path / "nonexistent.csv")},
    )

    with pytest.raises(FileNotFoundError, match="BTCUSDT"):
        HistoricalDataFeed.from_symbol_configs([cfg])


# ---------------------------------------------------------------------------
# candle_snapshot_at anti-lookahead filter
# ---------------------------------------------------------------------------


def test_snapshot_at_excludes_candles_not_yet_closed(tmp_path):
    """Candles whose close_time_ms > primary_open_time_ms must NOT appear."""
    csv_1m = tmp_path / "btc_1m.csv"
    csv_5m = tmp_path / "btc_5m.csv"
    _write_1m_csv(csv_1m, rows=10)
    _write_5m_csv(csv_5m, rows=3)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
        csv_paths_by_timeframe={"5m": str(csv_5m)},
    )
    feed = HistoricalDataFeed.from_symbol_configs([cfg])

    # primary_open_time_ms = start of first 1m candle; no 5m candle is closed yet
    snapshot = feed.candle_snapshot_at("BTCUSDT", _BASE_MS)

    # First 5m candle closes at _BASE_MS + 5m - 1ms; that's > _BASE_MS, so nothing visible
    assert snapshot == {} or "5m" not in snapshot or len(snapshot.get("5m", ())) == 0


def test_snapshot_at_includes_fully_closed_candles(tmp_path):
    """Candles whose close_time_ms <= primary_open_time_ms are visible."""
    csv_1m = tmp_path / "btc_1m.csv"
    csv_5m = tmp_path / "btc_5m.csv"
    _write_1m_csv(csv_1m, rows=10)
    _write_5m_csv(csv_5m, rows=3)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
        csv_paths_by_timeframe={"5m": str(csv_5m)},
    )
    feed = HistoricalDataFeed.from_symbol_configs([cfg])

    # After the first 5m candle closes (close_time_ms = _BASE_MS + 5*60_000 - 1)
    # if primary opens at _BASE_MS + 5*60_000, that candle should be visible
    primary_open_time_ms = _BASE_MS + _5M_MS
    snapshot = feed.candle_snapshot_at("BTCUSDT", primary_open_time_ms)

    assert "5m" in snapshot
    visible = snapshot["5m"]
    assert len(visible) >= 1
    for c in visible:
        assert c.close_time_ms <= primary_open_time_ms


def test_snapshot_at_unknown_symbol_returns_empty(tmp_path):
    csv_1m = tmp_path / "btc_1m.csv"
    _write_1m_csv(csv_1m)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
    )
    feed = HistoricalDataFeed.from_symbol_configs([cfg])

    snapshot = feed.candle_snapshot_at("ETHUSDT", _BASE_MS)

    assert snapshot == {}


# ---------------------------------------------------------------------------
# iter_cycles populates candle_snapshot
# ---------------------------------------------------------------------------


def test_iter_cycles_populates_snapshot_when_secondary_loaded(tmp_path):
    csv_1m = tmp_path / "btc_1m.csv"
    csv_5m = tmp_path / "btc_5m.csv"
    _write_1m_csv(csv_1m, rows=10)
    _write_5m_csv(csv_5m, rows=3)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
        csv_paths_by_timeframe={"5m": str(csv_5m)},
    )
    feed = HistoricalDataFeed.from_symbol_configs([cfg])

    events_with_snapshot = []
    for _ts, events in feed.iter_cycles():
        for event in events:
            if event.candle_snapshot:
                events_with_snapshot.append(event)

    # After the first 5m candle closes (5m after start), subsequent 1m events
    # should see it in their snapshot
    assert len(events_with_snapshot) > 0
    for event in events_with_snapshot:
        assert "5m" in event.candle_snapshot
        for c in event.candle_snapshot["5m"]:
            assert c.close_time_ms <= event.candle.open_time_ms


def test_iter_cycles_snapshot_empty_without_secondary(tmp_path):
    csv_1m = tmp_path / "btc_1m.csv"
    _write_1m_csv(csv_1m, rows=5)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
    )
    feed = HistoricalDataFeed.from_symbol_configs([cfg])

    for _ts, events in feed.iter_cycles():
        for event in events:
            assert event.candle_snapshot == {}


def test_candle_event_snapshot_anti_lookahead_invariant(tmp_path):
    """No snapshot candle may have close_time_ms > the event's primary open_time_ms."""
    csv_1m = tmp_path / "btc_1m.csv"
    csv_5m = tmp_path / "btc_5m.csv"
    _write_1m_csv(csv_1m, rows=20)
    _write_5m_csv(csv_5m, rows=5)
    cfg = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(csv_1m),
        rules=_symbol_rules(),
        csv_paths_by_timeframe={"5m": str(csv_5m)},
    )
    feed = HistoricalDataFeed.from_symbol_configs([cfg])

    for _ts, events in feed.iter_cycles():
        for event in events:
            for _tf, candles in event.candle_snapshot.items():
                for c in candles:
                    assert c.close_time_ms <= event.candle.open_time_ms, (
                        f"Lookahead violation: candle close {c.close_time_ms} > "
                        f"primary open {event.candle.open_time_ms}"
                    )
