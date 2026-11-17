"""Bollinger bands, band width and the volatility "squeeze".

Volatility clusters: quiet periods are followed by expansion. A band width in the
lowest percentiles of its own recent history (a squeeze) marks compression; a close
outside the band after it is the expansion starting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from indicators.common import to_decimal

_ZERO = Decimal("0")


@dataclass(frozen=True)
class BollingerBands:
    middle: Decimal
    upper: Decimal
    lower: Decimal
    std_dev: Decimal

    @property
    def bandwidth(self) -> Decimal:
        """Band width relative to the middle band: (upper - lower) / middle."""

        if self.middle <= _ZERO:
            return _ZERO
        return (self.upper - self.lower) / self.middle


def bollinger_bands(
    closes: Sequence[Decimal],
    period: int = 20,
    num_std: Decimal | int | float | str = Decimal("2"),
) -> BollingerBands | None:
    """Bands of the last ``period`` closes (population standard deviation)."""

    if period < 2:
        raise ValueError("period must be at least 2")
    if len(closes) < period:
        return None

    multiplier = to_decimal(num_std)
    if multiplier <= _ZERO:
        raise ValueError("num_std must be greater than zero")

    window = [to_decimal(value) for value in closes[-period:]]
    count = Decimal(period)
    middle = sum(window, _ZERO) / count
    variance = sum(((value - middle) ** 2 for value in window), _ZERO) / count
    std_dev = variance.sqrt()
    return BollingerBands(
        middle=middle,
        upper=middle + std_dev * multiplier,
        lower=middle - std_dev * multiplier,
        std_dev=std_dev,
    )


def bollinger_bandwidth_series(
    closes: Sequence[Decimal],
    period: int = 20,
    num_std: Decimal | int | float | str = Decimal("2"),
) -> list[Decimal | None]:
    """Band width at every index (``None`` before the first full window)."""

    values = [to_decimal(value) for value in closes]
    result: list[Decimal | None] = [None] * len(values)
    for end in range(period, len(values) + 1):
        bands = bollinger_bands(values[end - period : end], period, num_std)
        if bands is not None:
            result[end - 1] = bands.bandwidth
    return result


def percentile_rank(history: Sequence[Decimal], value: Decimal) -> Decimal | None:
    """Fraction of ``history`` at or below ``value`` (0 = lowest, 1 = highest)."""

    if not history:
        return None
    at_or_below = sum(1 for item in history if item <= value)
    return Decimal(at_or_below) / Decimal(len(history))


__all__ = [
    "BollingerBands",
    "bollinger_bands",
    "bollinger_bandwidth_series",
    "percentile_rank",
]
