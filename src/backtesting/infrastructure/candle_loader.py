from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from domain.candle import Candle
from domain.timeframe import (
    expected_close_time_ms,
    expected_open_time_ms,
    is_supported_timeframe,
    normalize_timeframe,
    timeframe_duration_ms,
)

REQUIRED_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}

__all__ = ["Candle", "load_candles_from_csv"]


def load_candles_from_csv(
    path: str,
    *,
    interval: str = "1m",
    symbol: str = "",
) -> list[Candle]:
    if not is_supported_timeframe(interval):
        raise ValueError(f"Timeframe nao suportado no CSV de backtest: {interval}")
    normalized_interval = normalize_timeframe(interval)
    normalized_symbol = str(symbol or "").upper().strip()

    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV nao encontrado: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError("CSV sem cabecalho")

        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(f"CSV sem colunas obrigatorias: {sorted(missing)}")

        candles = [
            _row_to_candle(
                row,
                line_number=index + 2,
                interval=normalized_interval,
                symbol=normalized_symbol,
            )
            for index, row in enumerate(reader)
        ]

    if not candles:
        raise ValueError("CSV nao contem candles")

    candles = sorted(candles, key=lambda c: c.open_time_ms)
    _validate_candle_sequence(candles, interval=normalized_interval)
    return candles


def _row_to_candle(
    row: dict[str, str],
    *,
    line_number: int,
    interval: str,
    symbol: str,
) -> Candle:
    try:
        raw_ts = str(row["timestamp"]).strip()
        if not raw_ts:
            raise ValueError(f"Linha {line_number} sem timestamp")

        duration_ms = timeframe_duration_ms(interval)
        open_time_ms = _iso_to_ms(raw_ts, line_number=line_number, duration_ms=duration_ms)
        close_time_ms = expected_close_time_ms(open_time_ms, interval)

        candle = Candle(
            symbol=symbol,
            interval=interval,
            open_time_ms=open_time_ms,
            close_time_ms=close_time_ms,
            open=Decimal(str(row["open"]).strip()),
            high=Decimal(str(row["high"]).strip()),
            low=Decimal(str(row["low"]).strip()),
            close=Decimal(str(row["close"]).strip()),
            volume=Decimal(str(row["volume"]).strip()),
            is_closed=True,
            source="csv",
        )
    except (InvalidOperation, KeyError) as error:
        raise ValueError(f"Linha {line_number} possui valor invalido") from error

    numeric_values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
    if any(not value.is_finite() for value in numeric_values):
        raise ValueError(f"Linha {line_number} possui OHLCV nao finito")
    if min(candle.open, candle.high, candle.low, candle.close) <= 0:
        raise ValueError(f"Linha {line_number} possui preco menor ou igual a zero")
    if candle.volume < 0:
        raise ValueError(f"Linha {line_number} possui volume negativo")
    if candle.high < max(candle.open, candle.close, candle.low):
        raise ValueError(f"Linha {line_number} possui high inconsistente com OHLC")
    if candle.low > min(candle.open, candle.close, candle.high):
        raise ValueError(f"Linha {line_number} possui low inconsistente com OHLC")
    if candle.open_time_ms != expected_open_time_ms(candle.open_time_ms, interval):
        raise ValueError(
            f"Linha {line_number} possui timestamp desalinhado para timeframe {interval}"
        )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if candle.close_time_ms >= now_ms:
        raise ValueError(f"Linha {line_number} representa candle ainda aberto ou no futuro")

    return candle


def _validate_candle_sequence(candles: list[Candle], *, interval: str) -> None:
    previous_open: int | None = None
    for index, candle in enumerate(candles, start=2):
        if previous_open is not None and candle.open_time_ms == previous_open:
            raise ValueError(
                f"Timestamp duplicado na linha normalizada {index}: {candle.timestamp}"
            )
        if previous_open is not None and candle.open_time_ms < previous_open:
            raise ValueError("Candles fora de ordem cronologica")
        previous_open = candle.open_time_ms


# Base epoch used when CSV timestamps are plain integers (test shorthand).
# 2020-01-01T00:00:00Z aligns cleanly to all supported timeframe boundaries.
_INTEGER_EPOCH_MS = 1_577_836_800_000


def _iso_to_ms(value: str, *, line_number: int, duration_ms: int = 60_000) -> int:
    """Parse a timestamp string to Unix milliseconds.

    Supports ISO-8601 strings and plain integers (treated as N * duration_ms
    after the test epoch — convenient for test fixtures).
    """
    stripped = value.strip()
    # Integer shorthand: "0", "1", "24", etc.
    if stripped.lstrip("-").isdigit():
        try:
            index = int(stripped)
            if index < 0:
                raise ValueError("index negativo")
            return _INTEGER_EPOCH_MS + index * duration_ms
        except ValueError as error:
            raise ValueError(f"Linha {line_number} com timestamp invalido: {value!r}") from error
    # ISO-8601 string
    try:
        normalized = stripped.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError as error:
        raise ValueError(f"Linha {line_number} com timestamp invalido: {value!r}") from error
