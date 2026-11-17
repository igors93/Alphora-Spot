from __future__ import annotations

from decimal import Decimal

from domain.candle import Candle
from market_data.candle_store import CandleDataStore

_BASE_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z
_MINUTE_MS = 60_000


def _candle(index: int, *, symbol: str = "BTCUSDT", close: str = "100") -> Candle:
    open_ms = _BASE_MS + index * _MINUTE_MS
    return Candle(
        symbol=symbol,
        interval="1m",
        open_time_ms=open_ms,
        close_time_ms=open_ms + _MINUTE_MS - 1,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("95"),
        close=Decimal(close),
        volume=Decimal("10"),
        is_closed=True,
        source="test",
    )


def test_series_limit_overrides_global_default():
    store = CandleDataStore(
        max_candles_per_series=100,
        series_limits={("btcusdt", "1m"): 5},
    )
    store.upsert_many([_candle(i) for i in range(20)])

    snapshot = store.snapshot("BTCUSDT", "1m", 20)
    assert len(snapshot) == 5
    assert snapshot[-1].open_time_ms == _candle(19).open_time_ms


def test_unregistered_series_uses_global_default():
    store = CandleDataStore(
        max_candles_per_series=8,
        series_limits={("BTCUSDT", "1m"): 5},
    )
    store.upsert_many([_candle(i, symbol="ETHUSDT") for i in range(20)])

    assert len(store.snapshot("ETHUSDT", "1m", 20)) == 8


def test_merge_appends_new_tail_candles():
    store = CandleDataStore()
    store.upsert_many([_candle(i) for i in range(5)])
    store.upsert_many([_candle(5), _candle(6)])

    snapshot = store.snapshot("BTCUSDT", "1m", 10)
    assert [c.open_time_ms for c in snapshot] == [_candle(i).open_time_ms for i in range(7)]


def test_merge_updates_last_candle_in_place():
    store = CandleDataStore()
    store.upsert_many([_candle(i) for i in range(3)])
    store.upsert_many([_candle(2, close="333"), _candle(3)])

    snapshot = store.snapshot("BTCUSDT", "1m", 10)
    assert len(snapshot) == 4
    assert snapshot[2].close == Decimal("333")
    assert snapshot[3].open_time_ms == _candle(3).open_time_ms


def test_merge_overlapping_history_still_dedupes_and_sorts():
    store = CandleDataStore()
    store.upsert_many([_candle(i) for i in (3, 4, 5)])
    store.upsert_many([_candle(i, close="222") for i in (1, 2, 3)])

    snapshot = store.snapshot("BTCUSDT", "1m", 10)
    assert [c.open_time_ms for c in snapshot] == [_candle(i).open_time_ms for i in range(1, 6)]
    assert snapshot[2].close == Decimal("222")  # new candle wins on duplicate
    assert snapshot[3].close == Decimal("100")


def test_merge_deduplicates_within_incoming_batch():
    store = CandleDataStore()
    store.upsert_many([_candle(0), _candle(0, close="111"), _candle(1)])

    snapshot = store.snapshot("BTCUSDT", "1m", 10)
    assert len(snapshot) == 2
    assert snapshot[0].close == Decimal("111")
