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
class VolumeZScore:
    """Statistical volume anomaly for the most recent candle.

    Unlike a plain relative-volume ratio, the z-score measures how many
    standard deviations the current volume sits above (or below) its own
    recent history, so a "climax" candle is detected relative to the symbol's
    normal dispersion instead of a fixed multiplier.
    """

    current: Decimal
    mean: Decimal
    std_dev: Decimal
    zscore: Decimal | None
    period: int

    def anomalous(self, threshold: Decimal) -> bool:
        return self.zscore is not None and self.zscore >= threshold


def minimum_volume_zscore_candles(period: int) -> int:
    return period + 1


def volume_zscore(
    candles: Sequence[VolumeCandleLike],
    period: int = 30,
) -> VolumeZScore | None:
    """Z-score of the last candle's volume against the previous ``period`` candles.

    The current candle is always excluded from the mean/deviation window so a
    volume spike cannot dilute its own baseline. Returns ``None`` while there
    is not enough history. ``zscore`` is ``None`` when the window has zero
    dispersion (flat volume history), in which case no anomaly is claimed.
    """

    if period < 2:
        raise ValueError("period must be at least 2")

    if len(candles) < minimum_volume_zscore_candles(period):
        return None

    window = candles[-(period + 1) : -1]
    volumes: list[Decimal] = []
    for candle in window:
        volume = to_decimal(candle.volume)
        if volume < _ZERO:
            return None
        volumes.append(volume)

    current = to_decimal(candles[-1].volume)
    if current < _ZERO:
        return None

    count = Decimal(len(volumes))
    mean = sum(volumes, _ZERO) / count
    variance = sum(((volume - mean) ** 2 for volume in volumes), _ZERO) / count
    std_dev = variance.sqrt()

    zscore = (current - mean) / std_dev if std_dev > _ZERO else None

    return VolumeZScore(
        current=current,
        mean=mean,
        std_dev=std_dev,
        zscore=zscore,
        period=period,
    )


__all__ = [
    "VolumeCandleLike",
    "VolumeZScore",
    "minimum_volume_zscore_candles",
    "volume_zscore",
]
