from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from indicators.adx import (
    DirectionalMovementIndex,
    directional_movement_index,
    minimum_adx_candles,
)
from indicators.atr import average_true_range
from indicators.common import to_decimal
from indicators.donchian import (
    DonchianChannel,
    donchian_channel,
    minimum_donchian_candles,
)
from indicators.moving_average import exponential_moving_average
from indicators.relative_volume import (
    RelativeVolume,
    minimum_relative_volume_candles,
    relative_volume,
)

_ZERO = Decimal("0")


class BreakoutCandleLike(Protocol):
    @property
    def high(self) -> Decimal: ...
    @property
    def low(self) -> Decimal: ...
    @property
    def close(self) -> Decimal: ...
    @property
    def volume(self) -> Decimal: ...


@dataclass(frozen=True)
class DonchianAdxBreakoutIndicators:
    close: Decimal
    ema: Decimal
    atr: Decimal
    donchian: DonchianChannel
    dmi: DirectionalMovementIndex
    relative_volume: RelativeVolume
    adx_threshold: Decimal

    @property
    def bullish_breakout(self) -> bool:
        return self.close > self.donchian.upper

    @property
    def bearish_breakout(self) -> bool:
        return self.close < self.donchian.lower

    @property
    def above_ema(self) -> bool:
        return self.close > self.ema

    @property
    def below_ema(self) -> bool:
        return self.close < self.ema

    @property
    def adx_confirmed(self) -> bool:
        return self.dmi.adx >= self.adx_threshold and self.dmi.adx_rising

    @property
    def bullish_setup(self) -> bool:
        return (
            self.bullish_breakout
            and self.above_ema
            and self.dmi.bullish_bias
            and self.adx_confirmed
            and self.relative_volume.confirmed
        )

    @property
    def bearish_setup(self) -> bool:
        return (
            self.bearish_breakout
            and self.below_ema
            and self.dmi.bearish_bias
            and self.adx_confirmed
            and self.relative_volume.confirmed
        )


def calculate_donchian_adx_breakout_indicators(
    candles: Sequence[BreakoutCandleLike],
    *,
    donchian_period: int = 20,
    adx_period: int = 14,
    ema_period: int = 50,
    atr_period: int = 14,
    volume_period: int = 20,
    volume_multiplier: Decimal | int | float | str = Decimal("1.3"),
    adx_threshold: Decimal | int | float | str = Decimal("25"),
    require_closed_candle: bool = True,
) -> DonchianAdxBreakoutIndicators | None:
    """Calculate all indicators required by the breakout strategy."""

    required = minimum_required_candles(
        donchian_period=donchian_period,
        adx_period=adx_period,
        ema_period=ema_period,
        atr_period=atr_period,
        volume_period=volume_period,
    )

    if len(candles) < required:
        return None

    current = candles[-1]

    if require_closed_candle and not bool(getattr(current, "is_closed", True)):
        raise ValueError("the latest candle must be closed")

    threshold = to_decimal(adx_threshold)

    if threshold < _ZERO:
        raise ValueError("adx_threshold cannot be negative")

    channel = donchian_channel(
        candles,
        donchian_period,
        exclude_current=True,
    )

    dmi = directional_movement_index(
        candles,
        adx_period,
    )

    ema = exponential_moving_average(
        [to_decimal(candle.close) for candle in candles],
        ema_period,
    )

    atr = average_true_range(
        candles,
        atr_period,
    )

    volume = relative_volume(
        candles,
        volume_period,
        required_multiplier=volume_multiplier,
    )

    if (
        channel is None
        or dmi is None
        or dmi.previous_adx is None
        or ema is None
        or atr is None
        or volume is None
    ):
        return None

    return DonchianAdxBreakoutIndicators(
        close=to_decimal(current.close),
        ema=ema,
        atr=atr,
        donchian=channel,
        dmi=dmi,
        relative_volume=volume,
        adx_threshold=threshold,
    )


def minimum_required_candles(
    *,
    donchian_period: int = 20,
    adx_period: int = 14,
    ema_period: int = 50,
    atr_period: int = 14,
    volume_period: int = 20,
) -> int:
    """Return the minimum warm-up required by the complete setup."""

    for name, value in (
        ("donchian_period", donchian_period),
        ("adx_period", adx_period),
        ("ema_period", ema_period),
        ("atr_period", atr_period),
        ("volume_period", volume_period),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero")

    return max(
        minimum_donchian_candles(
            donchian_period,
            exclude_current=True,
        ),
        minimum_adx_candles(
            adx_period,
            require_previous_adx=True,
        ),
        ema_period,
        atr_period + 1,
        minimum_relative_volume_candles(volume_period),
    )


__all__ = [
    "BreakoutCandleLike",
    "DonchianAdxBreakoutIndicators",
    "calculate_donchian_adx_breakout_indicators",
    "minimum_required_candles",
]
