from __future__ import annotations

import time
from decimal import Decimal

from domain.candle import Candle
from domain.timeframe import (
    SUPPORTED_TIMEFRAMES,
    expected_open_time_ms,
    timeframe_duration_ms,
)

MAX_FUTURE_TOLERANCE_MS = 5_000


class CandleValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def validate_candle(candle: Candle, *, now_ms: int | None = None) -> None:
    """Raise CandleValidationError on any invariant violation."""
    if not candle.symbol:
        raise CandleValidationError("CANDLE_INVALID_OHLC", "symbol vazio")
    if not candle.interval:
        raise CandleValidationError("CANDLE_INVALID_OHLC", "interval vazio")
    if candle.interval not in SUPPORTED_TIMEFRAMES:
        raise CandleValidationError(
            "CANDLE_TIMEFRAME_MISMATCH",
            f"interval nao suportado: {candle.interval!r}",
        )
    if candle.open_time_ms <= 0:
        raise CandleValidationError("CANDLE_INVALID_OHLC", "open_time_ms invalido")
    if candle.close_time_ms <= candle.open_time_ms:
        raise CandleValidationError(
            "CANDLE_INVALID_OHLC",
            "close_time_ms nao e posterior a open_time_ms",
        )
    _now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if candle.open_time_ms > _now_ms + MAX_FUTURE_TOLERANCE_MS:
        raise CandleValidationError(
            "CANDLE_TIMESTAMP_IN_FUTURE",
            f"open_time_ms {candle.open_time_ms} esta no futuro",
        )
    for field_name, value in (
        ("open", candle.open),
        ("high", candle.high),
        ("low", candle.low),
        ("close", candle.close),
    ):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise CandleValidationError(
                "CANDLE_INVALID_OHLC",
                f"{field_name} deve ser Decimal positivo finito",
            )
    if candle.volume < 0:
        raise CandleValidationError("CANDLE_INVALID_OHLC", "volume negativo")
    if candle.high < candle.low:
        raise CandleValidationError("CANDLE_INVALID_OHLC", "high menor que low")
    if candle.high < candle.open:
        raise CandleValidationError("CANDLE_INVALID_OHLC", "high menor que open")
    if candle.high < candle.close:
        raise CandleValidationError("CANDLE_INVALID_OHLC", "high menor que close")
    if candle.low > candle.open:
        raise CandleValidationError("CANDLE_INVALID_OHLC", "low maior que open")
    if candle.low > candle.close:
        raise CandleValidationError("CANDLE_INVALID_OHLC", "low maior que close")
    aligned_open = expected_open_time_ms(candle.open_time_ms, candle.interval)
    if candle.open_time_ms != aligned_open:
        raise CandleValidationError(
            "CANDLE_INVALID_OHLC",
            f"open_time_ms {candle.open_time_ms} nao alinhado ao timeframe {candle.interval}",
        )
    expected_duration = timeframe_duration_ms(candle.interval) - 1
    actual_duration = candle.close_time_ms - candle.open_time_ms
    if abs(actual_duration - expected_duration) > 1:
        raise CandleValidationError(
            "CANDLE_INVALID_OHLC",
            f"duracao {actual_duration}ms incompativel com timeframe {candle.interval} "
            f"(esperado {expected_duration}ms)",
        )


def is_valid_candle(candle: Candle, *, now_ms: int | None = None) -> bool:
    try:
        validate_candle(candle, now_ms=now_ms)
        return True
    except CandleValidationError:
        return False
