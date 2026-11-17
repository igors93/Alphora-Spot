from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from domain.candle import Candle

_DEFAULT_MAX_CANDLES = 2000


@dataclass(frozen=True)
class CandleSeriesDiagnostics:
    symbol: str
    interval: str
    requested_count: int
    available_count: int
    first_open_time_ms: int | None
    last_close_time_ms: int | None
    age_seconds: float | None
    gap_count: int
    duplicate_count: int
    stale: bool
    warmup_complete: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "requested_count": self.requested_count,
            "available_count": self.available_count,
            "first_open_time_ms": self.first_open_time_ms,
            "last_close_time_ms": self.last_close_time_ms,
            "age_seconds": self.age_seconds,
            "gap_count": self.gap_count,
            "duplicate_count": self.duplicate_count,
            "stale": self.stale,
            "warmup_complete": self.warmup_complete,
        }


class CandleDataStore:
    """Thread-safe in-memory candle store, partitioned by (symbol, interval).

    `series_limits` caps each series at what its consumer actually needs;
    unregistered series fall back to `max_candles_per_series`.
    """

    def __init__(
        self,
        *,
        max_candles_per_series: int = _DEFAULT_MAX_CANDLES,
        series_limits: dict[tuple[str, str], int] | None = None,
    ):
        self._max = max(1, int(max_candles_per_series))
        self._series_limits = {
            (symbol.upper(), interval): max(1, int(limit))
            for (symbol, interval), limit in (series_limits or {}).items()
        }
        self._data: dict[tuple[str, str], list[Candle]] = {}
        self._lock = threading.Lock()

    def _limit_for(self, key: tuple[str, str]) -> int:
        return self._series_limits.get(key, self._max)

    def upsert_many(self, candles: list[Candle]) -> None:
        if not candles:
            return
        by_key: dict[tuple[str, str], list[Candle]] = {}
        for candle in candles:
            key = (candle.symbol.upper(), candle.interval)
            by_key.setdefault(key, []).append(candle)

        with self._lock:
            for key, new_candles in by_key.items():
                existing = self._data.get(key, [])
                merged = _merge(existing, new_candles, max_count=self._limit_for(key))
                self._data[key] = merged

    def snapshot(self, symbol: str, interval: str, limit: int) -> tuple[Candle, ...]:
        """Return the most recent `limit` candles as an immutable tuple."""
        key = (symbol.upper(), interval)
        with self._lock:
            series = self._data.get(key, [])
            return tuple(series[-limit:]) if len(series) >= limit else tuple(series)

    def latest_closed(self, symbol: str, interval: str) -> Candle | None:
        key = (symbol.upper(), interval)
        with self._lock:
            series = self._data.get(key, [])
            for candle in reversed(series):
                if candle.is_closed:
                    return candle
            return None

    def diagnostics(
        self,
        symbol: str,
        interval: str,
        *,
        required: int,
        stale_threshold_seconds: float = 120.0,
    ) -> CandleSeriesDiagnostics:
        from domain.timeframe import timeframe_duration_ms

        key = (symbol.upper(), interval)
        with self._lock:
            series = list(self._data.get(key, []))

        available = len(series)
        first_open = series[0].open_time_ms if series else None
        last_close = series[-1].close_time_ms if series else None

        now_ms = int(time.time() * 1000)
        age_seconds: float | None = None
        stale = True
        if last_close is not None:
            age_seconds = max(0.0, (now_ms - last_close) / 1000.0)
            stale = age_seconds > stale_threshold_seconds

        gap_count = 0
        duplicate_count = 0
        if series:
            duration_ms = timeframe_duration_ms(interval)
            seen: set[int] = set()
            for i, candle in enumerate(series):
                if candle.open_time_ms in seen:
                    duplicate_count += 1
                seen.add(candle.open_time_ms)
                if i > 0:
                    expected_next = series[i - 1].open_time_ms + duration_ms
                    if candle.open_time_ms != expected_next:
                        gap_count += 1

        warmup_complete = (
            available >= required and gap_count == 0 and not stale and duplicate_count == 0
        )

        return CandleSeriesDiagnostics(
            symbol=symbol.upper(),
            interval=interval,
            requested_count=required,
            available_count=available,
            first_open_time_ms=first_open,
            last_close_time_ms=last_close,
            age_seconds=age_seconds,
            gap_count=gap_count,
            duplicate_count=duplicate_count,
            stale=stale,
            warmup_complete=warmup_complete,
        )


def _merge(
    existing: list[Candle],
    new_candles: list[Candle],
    *,
    max_count: int,
) -> list[Candle]:
    """Merge, deduplicate by open_time_ms (new wins), sort, trim to max_count."""
    incoming: dict[int, Candle] = {c.open_time_ms: c for c in new_candles}
    ordered_new = sorted(incoming.values(), key=lambda c: c.open_time_ms)

    if not existing:
        return ordered_new[-max_count:]

    # Fast path: incoming candles append to (or update) the series tail — the
    # common refresh case. Avoids rebuilding and re-sorting the whole series.
    last_time = existing[-1].open_time_ms
    if ordered_new[0].open_time_ms > last_time:
        merged = existing + ordered_new
        return merged[-max_count:]
    if ordered_new[0].open_time_ms == last_time:
        merged = existing[:-1] + ordered_new
        return merged[-max_count:]

    by_time: dict[int, Candle] = {c.open_time_ms: c for c in existing}
    by_time.update(incoming)
    merged = sorted(by_time.values(), key=lambda c: c.open_time_ms)
    return merged[-max_count:]
