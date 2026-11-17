from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from indicators.common import to_decimal


class DonchianCandleLike(Protocol):
    @property
    def high(self) -> Decimal: ...
    @property
    def low(self) -> Decimal: ...


@dataclass(frozen=True)
class DonchianChannel:
    upper: Decimal
    lower: Decimal
    middle: Decimal
    period: int


def donchian_channel(
    candles: Sequence[DonchianCandleLike],
    period: int = 20,
    *,
    exclude_current: bool = True,
) -> DonchianChannel | None:
    """Calculate a Donchian channel from a historical candle window.

    The current candle is excluded by default so it cannot define and break
    the same channel simultaneously.
    """

    if period <= 0:
        raise ValueError("period must be greater than zero")

    required = period + 1 if exclude_current else period

    if len(candles) < required:
        return None

    if exclude_current:
        window = candles[-(period + 1) : -1]
    else:
        window = candles[-period:]

    upper = max(to_decimal(candle.high) for candle in window)
    lower = min(to_decimal(candle.low) for candle in window)

    return DonchianChannel(
        upper=upper,
        lower=lower,
        middle=(upper + lower) / Decimal("2"),
        period=period,
    )


def minimum_donchian_candles(
    period: int = 20,
    *,
    exclude_current: bool = True,
) -> int:
    """Return the minimum number of candles required for the channel."""

    if period <= 0:
        raise ValueError("period must be greater than zero")

    return period + 1 if exclude_current else period


__all__ = [
    "DonchianCandleLike",
    "DonchianChannel",
    "donchian_channel",
    "minimum_donchian_candles",
]
