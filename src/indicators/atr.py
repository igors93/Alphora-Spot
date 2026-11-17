from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from indicators.common import to_decimal


class CandleLike(Protocol):
    @property
    def high(self) -> Decimal: ...
    @property
    def low(self) -> Decimal: ...
    @property
    def close(self) -> Decimal: ...


@dataclass(frozen=True)
class PriceCandle:
    high: Decimal
    low: Decimal
    close: Decimal


def true_range(*, high: Decimal, low: Decimal, previous_close: Decimal) -> Decimal:
    high = to_decimal(high)
    low = to_decimal(low)
    previous_close = to_decimal(previous_close)
    return max(
        high - low,
        abs(high - previous_close),
        abs(low - previous_close),
    )


def average_true_range(candles: Sequence[CandleLike], period: int) -> Decimal | None:
    if period <= 0:
        raise ValueError("period precisa ser maior que zero")
    if len(candles) < period + 1:
        return None

    start = len(candles) - period
    total = Decimal("0")
    for index in range(start, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        total += true_range(
            high=current.high,
            low=current.low,
            previous_close=previous.close,
        )
    return total / Decimal(period)
