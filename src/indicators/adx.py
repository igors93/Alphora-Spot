from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from indicators.common import to_decimal

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


class AdxCandleLike(Protocol):
    @property
    def high(self) -> Decimal: ...
    @property
    def low(self) -> Decimal: ...
    @property
    def close(self) -> Decimal: ...


@dataclass(frozen=True)
class DirectionalMovementIndex:
    plus_di: Decimal
    minus_di: Decimal
    dx: Decimal
    adx: Decimal
    previous_adx: Decimal | None
    period: int

    @property
    def adx_rising(self) -> bool:
        """Return whether ADX increased from its previous value."""

        return self.previous_adx is not None and self.adx > self.previous_adx

    @property
    def bullish_bias(self) -> bool:
        """Return whether positive directional movement is dominant."""

        return self.plus_di > self.minus_di

    @property
    def bearish_bias(self) -> bool:
        """Return whether negative directional movement is dominant."""

        return self.minus_di > self.plus_di

    def is_strong(
        self,
        threshold: Decimal | int | float | str = Decimal("25"),
    ) -> bool:
        """Return whether ADX satisfies the configured strength threshold."""

        value = to_decimal(threshold)

        if value < _ZERO:
            raise ValueError("threshold cannot be negative")

        return self.adx >= value


def directional_movement_index(
    candles: Sequence[AdxCandleLike],
    period: int = 14,
) -> DirectionalMovementIndex | None:
    """Calculate Wilder's latest +DI, -DI, DX and ADX values."""

    if period <= 0:
        raise ValueError("period must be greater than zero")

    if len(candles) < period * 2:
        return None

    true_ranges: list[Decimal] = []
    plus_movements: list[Decimal] = []
    minus_movements: list[Decimal] = []

    for index in range(1, len(candles)):
        previous = candles[index - 1]
        current = candles[index]

        previous_high = to_decimal(previous.high)
        previous_low = to_decimal(previous.low)
        previous_close = to_decimal(previous.close)

        current_high = to_decimal(current.high)
        current_low = to_decimal(current.low)

        upward_move = current_high - previous_high
        downward_move = previous_low - current_low

        plus_dm = upward_move if upward_move > downward_move and upward_move > _ZERO else _ZERO

        minus_dm = downward_move if downward_move > upward_move and downward_move > _ZERO else _ZERO

        true_range = max(
            current_high - current_low,
            abs(current_high - previous_close),
            abs(current_low - previous_close),
        )

        true_ranges.append(true_range)
        plus_movements.append(plus_dm)
        minus_movements.append(minus_dm)

    period_decimal = Decimal(period)

    smoothed_true_range = sum(true_ranges[:period], _ZERO)
    smoothed_plus_dm = sum(plus_movements[:period], _ZERO)
    smoothed_minus_dm = sum(minus_movements[:period], _ZERO)

    directional_values: list[tuple[Decimal, Decimal, Decimal]] = [
        _directional_values(
            smoothed_true_range=smoothed_true_range,
            smoothed_plus_dm=smoothed_plus_dm,
            smoothed_minus_dm=smoothed_minus_dm,
        )
    ]

    for index in range(period, len(true_ranges)):
        smoothed_true_range = (
            smoothed_true_range - smoothed_true_range / period_decimal + true_ranges[index]
        )

        smoothed_plus_dm = (
            smoothed_plus_dm - smoothed_plus_dm / period_decimal + plus_movements[index]
        )

        smoothed_minus_dm = (
            smoothed_minus_dm - smoothed_minus_dm / period_decimal + minus_movements[index]
        )

        directional_values.append(
            _directional_values(
                smoothed_true_range=smoothed_true_range,
                smoothed_plus_dm=smoothed_plus_dm,
                smoothed_minus_dm=smoothed_minus_dm,
            )
        )

    dx_values = [item[2] for item in directional_values]

    if len(dx_values) < period:
        return None

    adx = sum(dx_values[:period], _ZERO) / period_decimal
    previous_adx: Decimal | None = None

    for dx_value in dx_values[period:]:
        previous_adx = adx
        adx = (adx * Decimal(period - 1) + dx_value) / period_decimal

    latest_plus_di, latest_minus_di, latest_dx = directional_values[-1]

    return DirectionalMovementIndex(
        plus_di=latest_plus_di,
        minus_di=latest_minus_di,
        dx=latest_dx,
        adx=adx,
        previous_adx=previous_adx,
        period=period,
    )


def minimum_adx_candles(
    period: int = 14,
    *,
    require_previous_adx: bool = True,
) -> int:
    """Return the minimum number of candles required for ADX."""

    if period <= 0:
        raise ValueError("period must be greater than zero")

    minimum = period * 2

    if require_previous_adx:
        minimum += 1

    return minimum


def _directional_values(
    *,
    smoothed_true_range: Decimal,
    smoothed_plus_dm: Decimal,
    smoothed_minus_dm: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    if smoothed_true_range <= _ZERO:
        return _ZERO, _ZERO, _ZERO

    plus_di = _HUNDRED * smoothed_plus_dm / smoothed_true_range

    minus_di = _HUNDRED * smoothed_minus_dm / smoothed_true_range

    directional_sum = plus_di + minus_di

    if directional_sum <= _ZERO:
        return plus_di, minus_di, _ZERO

    dx = _HUNDRED * abs(plus_di - minus_di) / directional_sum

    return plus_di, minus_di, dx


__all__ = [
    "AdxCandleLike",
    "DirectionalMovementIndex",
    "directional_movement_index",
    "minimum_adx_candles",
]
