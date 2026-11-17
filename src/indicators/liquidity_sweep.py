from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from indicators.atr import average_true_range
from indicators.common import to_decimal
from indicators.volume_zscore import (
    VolumeZScore,
    minimum_volume_zscore_candles,
    volume_zscore,
)

_ZERO = Decimal("0")


class SweepCandleLike(Protocol):
    @property
    def open(self) -> Decimal: ...
    @property
    def high(self) -> Decimal: ...
    @property
    def low(self) -> Decimal: ...
    @property
    def close(self) -> Decimal: ...
    @property
    def volume(self) -> Decimal: ...


@dataclass(frozen=True)
class LiquiditySweepIndicators:
    """Failed-breakout ("liquidity sweep" / Wyckoff spring) detection.

    The signal candle is the last candle of the series. The swing reference is
    the highest high / lowest low of the ``swing_period`` candles immediately
    BEFORE the signal candle, so the sweep is always measured against prior
    structure — the level where resting stop orders cluster.

    A bullish sweep: the signal candle's low pierces the prior swing low but
    the candle closes back above it (sell-side liquidity taken, breakdown
    failed). A bearish sweep mirrors it at the prior swing high.
    """

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    atr: Decimal
    swing_low: Decimal
    swing_high: Decimal
    lower_wick_fraction: Decimal | None
    upper_wick_fraction: Decimal | None
    bullish_sweep_depth_atr: Decimal | None
    bearish_sweep_depth_atr: Decimal | None
    volume: VolumeZScore

    @property
    def bullish_swept(self) -> bool:
        return self.low < self.swing_low

    @property
    def bullish_reclaimed(self) -> bool:
        return self.bullish_swept and self.close > self.swing_low

    @property
    def bearish_swept(self) -> bool:
        return self.high > self.swing_high

    @property
    def bearish_reclaimed(self) -> bool:
        return self.bearish_swept and self.close < self.swing_high


def minimum_liquidity_sweep_candles(
    *,
    swing_period: int,
    atr_period: int,
    volume_period: int,
) -> int:
    return max(
        swing_period + 1,
        atr_period + 1,
        minimum_volume_zscore_candles(volume_period),
    )


def calculate_liquidity_sweep_indicators(
    candles: Sequence[SweepCandleLike],
    *,
    swing_period: int,
    atr_period: int,
    volume_period: int,
) -> LiquiditySweepIndicators | None:
    """Compute sweep metrics for the last candle of ``candles``.

    Returns ``None`` while there is not enough history or when the data does
    not allow a meaningful reading (non-positive ATR or an empty-range candle).
    """

    if swing_period < 2:
        raise ValueError("swing_period must be at least 2")

    required = minimum_liquidity_sweep_candles(
        swing_period=swing_period,
        atr_period=atr_period,
        volume_period=volume_period,
    )
    if len(candles) < required:
        return None

    signal = candles[-1]
    reference = candles[-(swing_period + 1) : -1]

    swing_low = min(to_decimal(candle.low) for candle in reference)
    swing_high = max(to_decimal(candle.high) for candle in reference)

    atr = average_true_range(candles, atr_period)
    if atr is None or atr <= _ZERO:
        return None

    volume = volume_zscore(candles, volume_period)
    if volume is None:
        return None

    open_price = to_decimal(signal.open)
    high = to_decimal(signal.high)
    low = to_decimal(signal.low)
    close = to_decimal(signal.close)

    candle_range = high - low
    if candle_range <= _ZERO:
        return None

    body_top = max(open_price, close)
    body_bottom = min(open_price, close)
    lower_wick_fraction = (body_bottom - low) / candle_range
    upper_wick_fraction = (high - body_top) / candle_range

    bullish_sweep_depth_atr = (swing_low - low) / atr if low < swing_low else None
    bearish_sweep_depth_atr = (high - swing_high) / atr if high > swing_high else None

    return LiquiditySweepIndicators(
        open=open_price,
        high=high,
        low=low,
        close=close,
        atr=atr,
        swing_low=swing_low,
        swing_high=swing_high,
        lower_wick_fraction=lower_wick_fraction,
        upper_wick_fraction=upper_wick_fraction,
        bullish_sweep_depth_atr=bullish_sweep_depth_atr,
        bearish_sweep_depth_atr=bearish_sweep_depth_atr,
        volume=volume,
    )


__all__ = [
    "LiquiditySweepIndicators",
    "SweepCandleLike",
    "calculate_liquidity_sweep_indicators",
    "minimum_liquidity_sweep_candles",
]
