from __future__ import annotations

import threading
import time
from decimal import Decimal

from domain.candle import Candle
from market_data.candle_store import CandleDataStore

_BASE_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z


def _candle(
    index: int = 0, *, interval: str = "1m", symbol: str = "BTCUSDT", is_closed: bool = True
) -> Candle:
    duration = 60_000 if interval == "1m" else 300_000  # 1m or 5m
    open_ms = _BASE_MS + index * duration
    return Candle(
        symbol=symbol,
        interval=interval,
        open_time_ms=open_ms,
        close_time_ms=open_ms + duration - 1,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("95"),
        close=Decimal("100"),
        volume=Decimal("10"),
        is_closed=is_closed,
        source="test",
    )


class TestUpsertAndSnapshot:
    def test_insert_and_retrieve(self):
        store = CandleDataStore()
        store.upsert_many([_candle(0), _candle(1)])
        snap = store.snapshot("BTCUSDT", "1m", 10)
        assert len(snap) == 2

    def test_snapshot_is_tuple(self):
        store = CandleDataStore()
        store.upsert_many([_candle(0)])
        snap = store.snapshot("BTCUSDT", "1m", 10)
        assert isinstance(snap, tuple)

    def test_snapshot_sorted_by_open_time(self):
        store = CandleDataStore()
        store.upsert_many([_candle(2), _candle(0), _candle(1)])
        snap = store.snapshot("BTCUSDT", "1m", 10)
        times = [c.open_time_ms for c in snap]
        assert times == sorted(times)

    def test_snapshot_returns_most_recent(self):
        store = CandleDataStore()
        store.upsert_many([_candle(i) for i in range(10)])
        snap = store.snapshot("BTCUSDT", "1m", 3)
        assert len(snap) == 3
        assert snap[-1].open_time_ms == _BASE_MS + 9 * 60_000

    def test_snapshot_empty_for_unknown_symbol(self):
        store = CandleDataStore()
        snap = store.snapshot("UNKNOWN", "1m", 10)
        assert snap == ()

    def test_symbol_is_uppercased(self):
        store = CandleDataStore()
        store.upsert_many([_candle(0, symbol="btcusdt")])
        assert len(store.snapshot("BTCUSDT", "1m", 10)) == 1

    def test_different_intervals_isolated(self):
        store = CandleDataStore()
        store.upsert_many([_candle(0, interval="1m"), _candle(0, interval="5m")])
        assert len(store.snapshot("BTCUSDT", "1m", 10)) == 1
        assert len(store.snapshot("BTCUSDT", "5m", 10)) == 1


class TestDeduplication:
    def test_same_open_time_new_wins(self):
        store = CandleDataStore()
        original = _candle(0)
        updated = Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=_BASE_MS,
            close_time_ms=_BASE_MS + 60_000 - 1,
            open=Decimal("200"),
            high=Decimal("210"),
            low=Decimal("195"),
            close=Decimal("205"),
            volume=Decimal("20"),
            is_closed=True,
            source="test",
        )
        store.upsert_many([original])
        store.upsert_many([updated])
        snap = store.snapshot("BTCUSDT", "1m", 10)
        assert len(snap) == 1
        assert snap[0].close == Decimal("205")

    def test_no_duplicate_open_times_in_snapshot(self):
        store = CandleDataStore()
        c1 = _candle(0)
        c2 = _candle(0)
        store.upsert_many([c1, c2])
        snap = store.snapshot("BTCUSDT", "1m", 10)
        assert len(snap) == 1


class TestMaxCandlesLimit:
    def test_trims_to_max(self):
        store = CandleDataStore(max_candles_per_series=5)
        store.upsert_many([_candle(i) for i in range(10)])
        snap = store.snapshot("BTCUSDT", "1m", 10)
        assert len(snap) == 5

    def test_keeps_most_recent_on_trim(self):
        store = CandleDataStore(max_candles_per_series=3)
        store.upsert_many([_candle(i) for i in range(5)])
        snap = store.snapshot("BTCUSDT", "1m", 3)
        times = [c.open_time_ms for c in snap]
        assert min(times) == _BASE_MS + 2 * 60_000


class TestLatestClosed:
    def test_returns_last_closed(self):
        store = CandleDataStore()
        store.upsert_many([_candle(0), _candle(1)])
        latest = store.latest_closed("BTCUSDT", "1m")
        assert latest is not None
        assert latest.open_time_ms == _BASE_MS + 60_000

    def test_skips_open_candle(self):
        store = CandleDataStore()
        store.upsert_many([_candle(0, is_closed=True), _candle(1, is_closed=False)])
        latest = store.latest_closed("BTCUSDT", "1m")
        assert latest is not None
        assert latest.open_time_ms == _BASE_MS

    def test_returns_none_when_empty(self):
        store = CandleDataStore()
        assert store.latest_closed("BTCUSDT", "1m") is None


class TestDiagnostics:
    def test_warmup_complete_when_enough_fresh_candles(self):
        store = CandleDataStore()
        now_ms = int(time.time() * 1000)
        # Use real now-based timestamps aligned to 1m boundary
        base = (now_ms // 60_000) * 60_000 - 5 * 60_000
        candles = [
            Candle(
                symbol="BTCUSDT",
                interval="1m",
                open_time_ms=base + i * 60_000,
                close_time_ms=base + i * 60_000 + 60_000 - 1,
                open=Decimal("100"),
                high=Decimal("105"),
                low=Decimal("95"),
                close=Decimal("100"),
                volume=Decimal("10"),
                is_closed=True,
                source="test",
            )
            for i in range(5)
        ]
        store.upsert_many(candles)
        diag = store.diagnostics("BTCUSDT", "1m", required=5, stale_threshold_seconds=120.0)
        assert diag.available_count == 5
        assert diag.gap_count == 0
        assert not diag.stale
        assert diag.warmup_complete is True

    def test_not_warmup_when_insufficient(self):
        store = CandleDataStore()
        now_ms = int(time.time() * 1000)
        base = (now_ms // 60_000) * 60_000 - 2 * 60_000
        candles = [
            Candle(
                symbol="BTCUSDT",
                interval="1m",
                open_time_ms=base + i * 60_000,
                close_time_ms=base + i * 60_000 + 60_000 - 1,
                open=Decimal("100"),
                high=Decimal("105"),
                low=Decimal("95"),
                close=Decimal("100"),
                volume=Decimal("10"),
                is_closed=True,
                source="test",
            )
            for i in range(2)
        ]
        store.upsert_many(candles)
        diag = store.diagnostics("BTCUSDT", "1m", required=10)
        assert diag.warmup_complete is False

    def test_stale_when_old_candles(self):
        store = CandleDataStore()
        store.upsert_many([_candle(0)])  # very old candles
        diag = store.diagnostics("BTCUSDT", "1m", required=1, stale_threshold_seconds=1.0)
        assert diag.stale is True
        assert diag.warmup_complete is False

    def test_gap_detected(self):
        store = CandleDataStore()
        now_ms = int(time.time() * 1000)
        base = (now_ms // 60_000) * 60_000 - 5 * 60_000

        def _fresh_candle(index: int) -> Candle:
            return Candle(
                symbol="BTCUSDT",
                interval="1m",
                open_time_ms=base + index * 60_000,
                close_time_ms=base + index * 60_000 + 60_000 - 1,
                open=Decimal("100"),
                high=Decimal("105"),
                low=Decimal("95"),
                close=Decimal("100"),
                volume=Decimal("10"),
                is_closed=True,
                source="test",
            )

        # Insert candles 0 and 2 — gap at index 1
        store.upsert_many([_fresh_candle(0), _fresh_candle(2)])
        diag = store.diagnostics("BTCUSDT", "1m", required=2)
        assert diag.gap_count >= 1

    def test_diagnostics_empty_series(self):
        store = CandleDataStore()
        diag = store.diagnostics("BTCUSDT", "1m", required=10)
        assert diag.available_count == 0
        assert diag.warmup_complete is False
        assert diag.first_open_time_ms is None
        assert diag.last_close_time_ms is None


class TestThreadSafety:
    def test_concurrent_upsert_no_exception(self):
        store = CandleDataStore(max_candles_per_series=500)
        errors: list[Exception] = []

        def writer(start: int) -> None:
            try:
                store.upsert_many([_candle(i) for i in range(start, start + 50)])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i * 50,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        snap = store.snapshot("BTCUSDT", "1m", 500)
        assert len(snap) > 0
