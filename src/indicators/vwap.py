"""Volume-weighted average price over a rolling candle window.

Crypto futures trade 24/7, so there is no exchange session to anchor a VWAP to. A
rolling VWAP of the last ``window`` candles (24 x 1h = the last day) is the
market-wide average price paid by volume, the reference level that intraday
participants defend: price reclaiming it inside a trend marks the end of a pullback.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from indicators.common import to_decimal

_ZERO = Decimal("0")
_THREE = Decimal("3")


class VwapCandleLike(Protocol):
    @property
    def high(self) -> Decimal: ...
    @property
    def low(self) -> Decimal: ...
    @property
    def close(self) -> Decimal: ...
    @property
    def volume(self) -> Decimal: ...


def typical_price(candle: VwapCandleLike) -> Decimal:
    return (to_decimal(candle.high) + to_decimal(candle.low) + to_decimal(candle.close)) / _THREE


def rolling_vwap_series(
    candles: Sequence[VwapCandleLike],
    window: int,
) -> list[Decimal | None]:
    """Rolling VWAP ending at every candle (inclusive), from typical price and volume.

    An index is ``None`` while fewer than ``window`` candles exist or when the
    window traded no volume. Computed with prefix sums: O(n) for the whole series.
    """

    if window <= 0:
        raise ValueError("window precisa ser maior que zero")

    count = len(candles)
    result: list[Decimal | None] = [None] * count
    if count < window:
        return result

    price_volume_prefix = [_ZERO] * (count + 1)
    volume_prefix = [_ZERO] * (count + 1)
    for index, candle in enumerate(candles):
        volume = to_decimal(candle.volume)
        if volume < _ZERO:
            raise ValueError("candle volume cannot be negative")
        price_volume_prefix[index + 1] = price_volume_prefix[index] + typical_price(candle) * volume
        volume_prefix[index + 1] = volume_prefix[index] + volume

    for end in range(window, count + 1):
        start = end - window
        volume = volume_prefix[end] - volume_prefix[start]
        if volume > _ZERO:
            result[end - 1] = (price_volume_prefix[end] - price_volume_prefix[start]) / volume
    return result


def rolling_vwap(candles: Sequence[VwapCandleLike], window: int) -> Decimal | None:
    """Rolling VWAP of the last ``window`` candles."""

    if window <= 0:
        raise ValueError("window precisa ser maior que zero")
    if len(candles) < window:
        return None
    return rolling_vwap_series(candles[-window:], window)[-1]


__all__ = [
    "VwapCandleLike",
    "rolling_vwap",
    "rolling_vwap_series",
    "typical_price",
]
