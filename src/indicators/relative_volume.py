from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from indicators.common import to_decimal

_ZERO = Decimal("0")


class VolumeCandleLike(Protocol):
    @property
    def volume(self) -> Decimal: ...


@dataclass(frozen=True)
class RelativeVolume:
    current: Decimal
    average: Decimal
    ratio: Decimal | None
    required_multiplier: Decimal
    period: int

    @property
    def confirmed(self) -> bool:
        """Return whether current volume confirms the movement."""

        if self.average <= _ZERO:
            return self.current > _ZERO

        return self.ratio is not None and self.ratio >= self.required_multiplier


def average_volume(
    candles: Sequence[VolumeCandleLike],
    period: int = 20,
    *,
    exclude_current: bool = True,
) -> Decimal | None:
    """Calculate the arithmetic average volume for a candle window."""

    if period <= 0:
        raise ValueError("period must be greater than zero")

    required = period + 1 if exclude_current else period

    if len(candles) < required:
        return None

    if exclude_current:
        window = candles[-(period + 1) : -1]
    else:
        window = candles[-period:]

    volumes: list[Decimal] = []

    for candle in window:
        volume = to_decimal(candle.volume)

        if volume < _ZERO:
            raise ValueError("candle volume cannot be negative")

        volumes.append(volume)

    return sum(volumes, _ZERO) / Decimal(period)


def relative_volume(
    candles: Sequence[VolumeCandleLike],
    period: int = 20,
    *,
    required_multiplier: Decimal | int | float | str = Decimal("1.3"),
) -> RelativeVolume | None:
    """Compare the latest volume against the previous average window."""

    if period <= 0:
        raise ValueError("period must be greater than zero")

    multiplier = to_decimal(required_multiplier)

    if multiplier <= _ZERO:
        raise ValueError("required_multiplier must be greater than zero")

    if len(candles) < period + 1:
        return None

    current = to_decimal(candles[-1].volume)

    if current < _ZERO:
        raise ValueError("candle volume cannot be negative")

    reference_average = average_volume(
        candles,
        period,
        exclude_current=True,
    )

    if reference_average is None:
        return None

    ratio = current / reference_average if reference_average > _ZERO else None

    return RelativeVolume(
        current=current,
        average=reference_average,
        ratio=ratio,
        required_multiplier=multiplier,
        period=period,
    )


def minimum_relative_volume_candles(period: int = 20) -> int:
    """Return the minimum number of candles required for relative volume."""

    if period <= 0:
        raise ValueError("period must be greater than zero")

    return period + 1


__all__ = [
    "RelativeVolume",
    "VolumeCandleLike",
    "average_volume",
    "minimum_relative_volume_candles",
    "relative_volume",
]
