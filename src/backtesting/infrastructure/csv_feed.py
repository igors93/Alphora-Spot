from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from heapq import heappop, heappush
from itertools import count
from pathlib import Path
from typing import Any

from backtesting.domain.config import BacktestSymbolConfig, CandleEvent
from backtesting.infrastructure.candle_loader import Candle, load_candles_from_csv
from domain.timeframe import timeframe_duration_ms

TimestampSortKey = tuple[int, Decimal | str]
HeapItem = tuple[TimestampSortKey, str, int, Candle]


@dataclass(frozen=True)
class DataFeedDiagnostics:
    symbols: list[str]
    candles_by_symbol: dict[str, int]
    first_timestamp_by_symbol: dict[str, str | None]
    last_timestamp_by_symbol: dict[str, str | None]
    union_start_timestamp: str | None
    union_end_timestamp: str | None
    effective_start_timestamp: str | None
    effective_end_timestamp: str | None
    total_timestamps: int
    partial_timestamps: int
    missing_candles_by_symbol: dict[str, int]
    missing_candles_preview: dict[str, list[str]]
    duplicate_timestamps_by_symbol: dict[str, int]
    cadence_gaps_by_symbol: dict[str, int]
    cadence_gaps_preview: dict[str, list[str]]
    expected_candles_by_symbol: dict[str, int]
    coverage_by_symbol: dict[str, str]
    unreliable_symbols: list[str]
    quality_warnings: list[str]
    secondary_timeframes: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    secondary_quality_warnings: list[str] = field(default_factory=list)
    cross_timeframe_consistency: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbols": self.symbols,
            "candles_by_symbol": self.candles_by_symbol,
            "first_timestamp_by_symbol": self.first_timestamp_by_symbol,
            "last_timestamp_by_symbol": self.last_timestamp_by_symbol,
            "union_start_timestamp": self.union_start_timestamp,
            "union_end_timestamp": self.union_end_timestamp,
            "effective_start_timestamp": self.effective_start_timestamp,
            "effective_end_timestamp": self.effective_end_timestamp,
            "total_timestamps": self.total_timestamps,
            "partial_timestamps": self.partial_timestamps,
            "missing_candles_by_symbol": self.missing_candles_by_symbol,
            "missing_candles_preview": self.missing_candles_preview,
            "duplicate_timestamps_by_symbol": self.duplicate_timestamps_by_symbol,
            "cadence_gaps_by_symbol": self.cadence_gaps_by_symbol,
            "cadence_gaps_preview": self.cadence_gaps_preview,
            "expected_candles_by_symbol": self.expected_candles_by_symbol,
            "coverage_by_symbol": self.coverage_by_symbol,
            "unreliable_symbols": self.unreliable_symbols,
            "quality_warnings": self.quality_warnings,
            "secondary_timeframes": self.secondary_timeframes,
            "secondary_quality_warnings": self.secondary_quality_warnings,
            "cross_timeframe_consistency": self.cross_timeframe_consistency,
        }


class HistoricalDataFeed:
    def __init__(
        self,
        candles_by_symbol: dict[str, list[Candle]],
        secondary_candles: dict[str, dict[str, list[Candle]]] | None = None,
    ):
        if not candles_by_symbol:
            raise ValueError("Data feed de backtesting exige candles")
        self.candles_by_symbol = candles_by_symbol
        # secondary_candles[symbol][timeframe] = sorted list of candles
        self._secondary_candles: dict[str, dict[str, list[Candle]]] = secondary_candles or {}
        self._primary_close_times = {
            symbol: [candle.close_time_ms for candle in candles]
            for symbol, candles in candles_by_symbol.items()
        }
        self._secondary_close_times = {
            symbol: {
                timeframe: [candle.close_time_ms for candle in candles]
                for timeframe, candles in by_timeframe.items()
            }
            for symbol, by_timeframe in self._secondary_candles.items()
        }
        primary_diagnostics = _build_diagnostics(candles_by_symbol)
        secondary_diagnostics: dict[str, dict[str, dict[str, Any]]] = {}
        cross_timeframe_consistency: dict[str, dict[str, dict[str, Any]]] = {}
        secondary_warnings: list[str] = []
        for symbol, by_timeframe in sorted(self._secondary_candles.items()):
            secondary_diagnostics[symbol] = {}
            cross_timeframe_consistency[symbol] = {}
            for timeframe, candles in sorted(by_timeframe.items()):
                diagnostics = _build_diagnostics({symbol: candles})
                secondary_diagnostics[symbol][timeframe] = diagnostics.as_dict()
                secondary_warnings.extend(
                    f"SECONDARY_{warning}:{symbol}:{timeframe}"
                    for warning in diagnostics.quality_warnings
                )
                consistency = _cross_timeframe_consistency(
                    self.candles_by_symbol.get(symbol, []),
                    candles,
                )
                cross_timeframe_consistency[symbol][timeframe] = consistency
                if consistency.get("applicable") and not consistency.get("passed"):
                    secondary_warnings.append(
                        f"CROSS_TIMEFRAME_OHLCV_MISMATCH:{symbol}:{timeframe}"
                    )
        combined_warnings = list(
            dict.fromkeys(primary_diagnostics.quality_warnings + secondary_warnings)
        )
        self._diagnostics = replace(
            primary_diagnostics,
            quality_warnings=combined_warnings,
            secondary_timeframes=secondary_diagnostics,
            secondary_quality_warnings=secondary_warnings,
            cross_timeframe_consistency=cross_timeframe_consistency,
        )

    @classmethod
    def from_symbol_configs(
        cls,
        symbols: Iterable[BacktestSymbolConfig],
        *,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> "HistoricalDataFeed":
        candles_by_symbol: dict[str, list[Candle]] = {}
        secondary_candles: dict[str, dict[str, list[Candle]]] = {}
        for symbol_config in symbols:
            path = Path(symbol_config.csv_path)
            try:
                loaded_primary = load_candles_from_csv(
                    str(path),
                    interval=symbol_config.timeframe,
                    symbol=symbol_config.symbol,
                )
                candles_by_symbol[symbol_config.symbol] = _filter_candles(
                    loaded_primary,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                )
                if not candles_by_symbol[symbol_config.symbol]:
                    raise ValueError("intervalo solicitado nao possui candles")
            except FileNotFoundError as error:
                raise FileNotFoundError(
                    f"CSV nao encontrado para {symbol_config.symbol}: {path}"
                ) from error
            except ValueError as error:
                raise ValueError(
                    f"CSV invalido para {symbol_config.symbol} ({path}): {error}"
                ) from error

            if symbol_config.csv_paths_by_timeframe:
                tf_map: dict[str, list[Candle]] = {}
                for timeframe, tf_csv_path in symbol_config.csv_paths_by_timeframe.items():
                    tf_path = Path(tf_csv_path)
                    try:
                        loaded_secondary = load_candles_from_csv(
                            str(tf_path),
                            interval=timeframe,
                            symbol=symbol_config.symbol,
                        )
                        tf_map[timeframe] = _filter_candles(
                            loaded_secondary,
                            start_timestamp=start_timestamp,
                            end_timestamp=end_timestamp,
                        )
                    except FileNotFoundError as error:
                        raise FileNotFoundError(
                            f"CSV multi-timeframe nao encontrado para {symbol_config.symbol}"
                            f" [{timeframe}]: {tf_path}"
                        ) from error
                    except ValueError as error:
                        raise ValueError(
                            f"CSV multi-timeframe invalido para {symbol_config.symbol}"
                            f" [{timeframe}] ({tf_path}): {error}"
                        ) from error
                if tf_map:
                    secondary_candles[symbol_config.symbol] = tf_map

        return cls(candles_by_symbol, secondary_candles)

    def diagnostics(self) -> DataFeedDiagnostics:
        return self._diagnostics

    def candle_snapshot_at(
        self,
        symbol: str,
        as_of_time_ms: int,
        *,
        include_primary: bool = False,
    ) -> dict[str, tuple[Candle, ...]]:
        """Return all candles fully closed at ``as_of_time_ms``.

        The primary series is included as well.  At ON_CLOSE this exposes the
        candle that just closed; at ON_OPEN it only exposes prior candles.
        """
        snapshot: dict[str, tuple[Candle, ...]] = {}
        primary = self.candles_by_symbol.get(symbol, [])
        if include_primary and primary:
            primary_index = bisect_right(
                self._primary_close_times.get(symbol, []),
                as_of_time_ms,
            )
            if primary_index:
                snapshot[primary[0].interval] = tuple(primary[:primary_index])
        tf_candles = self._secondary_candles.get(symbol, {})
        for timeframe, candles in tf_candles.items():
            visible_count = bisect_right(
                self._secondary_close_times.get(symbol, {}).get(timeframe, []),
                as_of_time_ms,
            )
            if visible_count:
                snapshot[timeframe] = tuple(candles[:visible_count])
        return snapshot

    def iter_cycles(
        self,
        *,
        signal_timing_policy: str = "ON_OPEN",
        include_primary: bool = False,
    ) -> Iterable[tuple[str, list[CandleEvent]]]:
        heap: list[HeapItem] = []
        tie_breaker = count()
        iterators = {
            symbol: iter(candles) for symbol, candles in sorted(self.candles_by_symbol.items())
        }
        for symbol, iterator in iterators.items():
            try:
                candle = next(iterator)
            except StopIteration:
                continue
            heappush(
                heap, (_timestamp_sort_key(candle.timestamp), symbol, next(tie_breaker), candle)
            )

        while heap:
            _sort_key, _symbol, _tie, first_candle = heappop(heap)
            timestamp = first_candle.timestamp
            as_of_ms = (
                first_candle.open_time_ms
                if str(signal_timing_policy).upper().strip() == "ON_OPEN"
                else first_candle.close_time_ms
            )
            snapshot = self.candle_snapshot_at(
                _symbol,
                as_of_ms,
                include_primary=include_primary,
            )
            events = [
                CandleEvent(
                    timestamp=_event_timestamp(first_candle, signal_timing_policy),
                    symbol=_symbol,
                    candle=first_candle,
                    candle_snapshot=snapshot,
                ),
            ]
            _push_next(heap, iterators, _symbol, tie_breaker)

            while heap and heap[0][0] == _timestamp_sort_key(timestamp):
                _same_sort_key, same_symbol, _same_tie, same_candle = heappop(heap)
                same_as_of_ms = (
                    same_candle.open_time_ms
                    if str(signal_timing_policy).upper().strip() == "ON_OPEN"
                    else same_candle.close_time_ms
                )
                same_snapshot = self.candle_snapshot_at(
                    same_symbol,
                    same_as_of_ms,
                    include_primary=include_primary,
                )
                events.append(
                    CandleEvent(
                        timestamp=_event_timestamp(same_candle, signal_timing_policy),
                        symbol=same_symbol,
                        candle=same_candle,
                        candle_snapshot=same_snapshot,
                    )
                )
                _push_next(heap, iterators, same_symbol, tie_breaker)

            yield timestamp, sorted(events, key=lambda event: event.symbol)


def _push_next(
    heap: list[HeapItem],
    iterators: dict[str, Iterator[Candle]],
    symbol: str,
    tie_breaker: Iterator[int],
) -> None:
    try:
        candle = next(iterators[symbol])
    except StopIteration:
        return
    heappush(heap, (_timestamp_sort_key(candle.timestamp), symbol, next(tie_breaker), candle))


def _timestamp_sort_key(value: str) -> tuple[int, Decimal | str]:
    try:
        return (0, Decimal(str(value)))
    except InvalidOperation:
        return (1, str(value))


def _build_diagnostics(candles_by_symbol: dict[str, list[Candle]]) -> DataFeedDiagnostics:
    symbols = list(candles_by_symbol)
    timestamps_by_symbol = {
        symbol: {candle.timestamp for candle in candles}
        for symbol, candles in candles_by_symbol.items()
    }
    all_timestamps = sorted(
        {timestamp for timestamps in timestamps_by_symbol.values() for timestamp in timestamps},
        key=_timestamp_sort_key,
    )
    missing_by_symbol: dict[str, list[str]] = {}
    for symbol in symbols:
        missing = [
            timestamp
            for timestamp in all_timestamps
            if timestamp not in timestamps_by_symbol[symbol]
        ]
        missing_by_symbol[symbol] = missing
    duplicate_timestamps_by_symbol = {
        symbol: _duplicate_timestamp_count(candles) for symbol, candles in candles_by_symbol.items()
    }
    cadence_gaps_by_symbol: dict[str, int] = {}
    cadence_gaps_preview: dict[str, list[str]] = {}
    expected_candles_by_symbol: dict[str, int] = {}
    for symbol, candles in candles_by_symbol.items():
        gap_count, gap_preview, expected = _cadence_gaps(candles)
        cadence_gaps_by_symbol[symbol] = gap_count
        expected_candles_by_symbol[symbol] = expected
        if gap_preview:
            cadence_gaps_preview[symbol] = gap_preview
    total_timestamps = len(all_timestamps)
    coverage_by_symbol = {
        symbol: str(
            _safe_decimal_div(
                Decimal(str(len(timestamps_by_symbol[symbol]))),
                Decimal(str(total_timestamps)),
            )
        )
        for symbol in symbols
    }
    unreliable_symbols = sorted(
        {
            symbol
            for symbol in symbols
            if len(candles_by_symbol[symbol]) < 2
            or missing_by_symbol[symbol]
            or duplicate_timestamps_by_symbol[symbol] > 0
            or cadence_gaps_by_symbol[symbol] > 0
        }
    )
    quality_warnings: list[str] = []
    if unreliable_symbols:
        quality_warnings.append("SYMBOLS_WITH_PARTIAL_OR_DUPLICATED_DATA")
    if any(len(candles) < 2 for candles in candles_by_symbol.values()):
        quality_warnings.append("INSUFFICIENT_CANDLES_FOR_SOME_SYMBOLS")
    if any(duplicate_timestamps_by_symbol.values()):
        quality_warnings.append("DUPLICATED_TIMESTAMPS_DETECTED")
    if any(cadence_gaps_by_symbol.values()):
        quality_warnings.append("CANDLE_CADENCE_GAPS_DETECTED")
    first_timestamps = [candles[0].timestamp for candles in candles_by_symbol.values() if candles]
    last_timestamps = [candles[-1].timestamp for candles in candles_by_symbol.values() if candles]

    return DataFeedDiagnostics(
        symbols=symbols,
        candles_by_symbol={symbol: len(candles) for symbol, candles in candles_by_symbol.items()},
        first_timestamp_by_symbol={
            symbol: candles[0].timestamp if candles else None
            for symbol, candles in candles_by_symbol.items()
        },
        last_timestamp_by_symbol={
            symbol: candles[-1].timestamp if candles else None
            for symbol, candles in candles_by_symbol.items()
        },
        union_start_timestamp=all_timestamps[0] if all_timestamps else None,
        union_end_timestamp=all_timestamps[-1] if all_timestamps else None,
        effective_start_timestamp=(
            max(first_timestamps, key=_timestamp_sort_key) if first_timestamps else None
        ),
        effective_end_timestamp=(
            min(last_timestamps, key=_timestamp_sort_key) if last_timestamps else None
        ),
        total_timestamps=total_timestamps,
        partial_timestamps=sum(
            1
            for timestamp in all_timestamps
            if any(timestamp not in timestamps for timestamps in timestamps_by_symbol.values())
        ),
        missing_candles_by_symbol={
            symbol: len(missing) for symbol, missing in missing_by_symbol.items()
        },
        missing_candles_preview={
            symbol: missing[:10] for symbol, missing in missing_by_symbol.items() if missing
        },
        duplicate_timestamps_by_symbol=duplicate_timestamps_by_symbol,
        cadence_gaps_by_symbol=cadence_gaps_by_symbol,
        cadence_gaps_preview=cadence_gaps_preview,
        expected_candles_by_symbol=expected_candles_by_symbol,
        coverage_by_symbol=coverage_by_symbol,
        unreliable_symbols=unreliable_symbols,
        quality_warnings=quality_warnings,
    )


def _duplicate_timestamp_count(candles: list[Candle]) -> int:
    seen: set[str] = set()
    duplicates = 0
    for candle in candles:
        if candle.timestamp in seen:
            duplicates += 1
        seen.add(candle.timestamp)
    return duplicates


def _safe_decimal_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return numerator / denominator


def _event_timestamp(candle: Candle, signal_timing_policy: str) -> str:
    timestamp_ms = (
        candle.open_time_ms
        if str(signal_timing_policy).upper().strip() == "ON_OPEN"
        else candle.close_time_ms
    )
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _cadence_gaps(candles: list[Candle]) -> tuple[int, list[str], int]:
    if not candles:
        return 0, [], 0
    duration = timeframe_duration_ms(candles[0].interval)
    expected = ((candles[-1].open_time_ms - candles[0].open_time_ms) // duration) + 1
    present = {candle.open_time_ms for candle in candles}
    gap_count = max(0, int(expected) - len(present))
    preview: list[str] = []
    current = candles[0].open_time_ms
    while current <= candles[-1].open_time_ms and len(preview) < 10:
        if current not in present:
            preview.append(str(current))
        current += duration
    return gap_count, preview, int(expected)


def _filter_candles(
    candles: list[Candle],
    *,
    start_timestamp: str | None,
    end_timestamp: str | None,
) -> list[Candle]:
    start_ms = _parse_filter_timestamp(start_timestamp)
    end_ms = _parse_filter_timestamp(end_timestamp, end_of_day=True)
    return [
        candle
        for candle in candles
        if (start_ms is None or candle.open_time_ms >= start_ms)
        and (end_ms is None or candle.open_time_ms <= end_ms)
    ]


def _cross_timeframe_consistency(
    primary: list[Candle],
    secondary: list[Candle],
) -> dict[str, Any]:
    if not primary or not secondary:
        return {"applicable": False, "passed": False, "reason": "MISSING_SERIES"}
    primary_duration = timeframe_duration_ms(primary[0].interval)
    secondary_duration = timeframe_duration_ms(secondary[0].interval)
    if secondary_duration <= primary_duration or secondary_duration % primary_duration != 0:
        return {
            "applicable": False,
            "passed": True,
            "reason": "TIMEFRAME_NOT_AGGREGATABLE",
        }

    expected_group_size = secondary_duration // primary_duration
    grouped: dict[int, list[Candle]] = {}
    for candle in primary:
        bucket = candle.open_time_ms - candle.open_time_ms % secondary_duration
        grouped.setdefault(bucket, []).append(candle)
    secondary_by_open = {candle.open_time_ms: candle for candle in secondary}
    checked = 0
    mismatch_count = 0
    mismatch_preview: list[str] = []
    tolerance = Decimal("0.00000001")
    for open_time_ms, candles in sorted(grouped.items()):
        target = secondary_by_open.get(open_time_ms)
        if target is None or len(candles) != expected_group_size:
            continue
        ordered = sorted(candles, key=lambda item: item.open_time_ms)
        checked += 1
        expected = {
            "open": ordered[0].open,
            "high": max(item.high for item in ordered),
            "low": min(item.low for item in ordered),
            "close": ordered[-1].close,
            "volume": sum((item.volume for item in ordered), Decimal("0")),
        }
        actual = {
            "open": target.open,
            "high": target.high,
            "low": target.low,
            "close": target.close,
            "volume": target.volume,
        }
        mismatched_fields = [
            field_name
            for field_name in expected
            if abs(expected[field_name] - actual[field_name]) > tolerance
        ]
        if mismatched_fields:
            mismatch_count += 1
            if len(mismatch_preview) < 10:
                mismatch_preview.append(f"{open_time_ms}:{','.join(mismatched_fields)}")

    return {
        "applicable": True,
        "passed": checked > 0 and mismatch_count == 0,
        "primary_timeframe": primary[0].interval,
        "secondary_timeframe": secondary[0].interval,
        "expected_primary_candles_per_secondary": expected_group_size,
        "checked_groups": checked,
        "mismatch_count": mismatch_count,
        "mismatch_preview": mismatch_preview,
    }


def _parse_filter_timestamp(
    value: str | None,
    *,
    end_of_day: bool = False,
) -> int | None:
    if not value:
        return None
    normalized = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if end_of_day and len(str(value).strip()) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return int(parsed.timestamp() * 1000)
