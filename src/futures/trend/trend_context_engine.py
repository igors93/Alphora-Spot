from collections.abc import Sequence
from math import isfinite, sqrt
from typing import Any

from futures.trend.context import (
    MarketRegime,
    TrendContext,
    TrendDirection,
    TrendStrength,
)


class TrendContextEngine:
    def __init__(
        self,
        *,
        fast_ma_period: int = 20,
        slow_ma_period: int = 50,
        slope_lookback: int = 5,
        volatility_lookback: int = 20,
        high_volatility_threshold_pct: float = 3.0,
        chop_threshold: float = 0.6,
    ):
        self.fast_ma_period = max(1, int(fast_ma_period or 20))
        self.slow_ma_period = max(self.fast_ma_period + 1, int(slow_ma_period or 50))
        self.slope_lookback = max(1, int(slope_lookback or 5))
        self.volatility_lookback = max(1, int(volatility_lookback or 20))
        self.high_volatility_threshold_pct = max(
            0.0,
            float(high_volatility_threshold_pct or 0.0),
        )
        self.chop_threshold = min(1.0, max(0.0, float(chop_threshold or 0.0)))

    def required_price_window(self) -> int:
        required = max(
            self.slow_ma_period + self.slope_lookback,
            self.volatility_lookback + self.fast_ma_period,
            self.slow_ma_period + self.volatility_lookback,
        )
        return required + 10

    @classmethod
    def from_config(cls, config: Any) -> "TrendContextEngine":
        return cls(
            fast_ma_period=getattr(config, "trend_fast_ma_period", 20),
            slow_ma_period=getattr(config, "trend_slow_ma_period", 50),
            slope_lookback=getattr(config, "trend_slope_lookback", 5),
            volatility_lookback=getattr(config, "trend_volatility_lookback", 20),
            high_volatility_threshold_pct=getattr(
                config,
                "trend_high_volatility_threshold_pct",
                3.0,
            ),
            chop_threshold=getattr(config, "trend_chop_threshold", 0.6),
        )

    def evaluate_candles(
        self,
        *,
        symbol: str,
        candles: Sequence[Any],
        timestamp: str | float | None = None,
    ) -> TrendContext:
        """The trend of the closes of closed candles of one timeframe.

        The live Governor and the backtest both call this with the candles of the same
        timeframe, so the regime gate sees the same series in both. A window of evaluation
        samples would change its time scale with the evaluation frequency of each symbol.
        """

        closes = [candle.close for candle in candles if getattr(candle, "is_closed", True)]
        return self.evaluate(
            symbol=symbol,
            prices=closes[-self.required_price_window() :],
            timestamp=timestamp,
        )

    def evaluate(
        self,
        *,
        symbol: str,
        prices: Sequence[Any],
        timestamp: str | float | None = None,
    ) -> TrendContext:
        normalized_symbol = str(symbol or "").upper().strip() or "UNKNOWN"
        numeric_prices = _coerce_prices(prices)[-self.required_price_window() :]
        data_points = len(numeric_prices)
        min_points = self.slow_ma_period + self.slope_lookback

        if data_points < min_points:
            return self._low_data_context(
                symbol=normalized_symbol,
                timestamp=timestamp,
                data_points=data_points,
                reason="INSUFFICIENT_DATA_FOR_SLOW_MA_AND_SLOPE",
                min_points=min_points,
            )

        price = numeric_prices[-1]
        fast_ma = _simple_ma(numeric_prices, self.fast_ma_period)
        slow_ma = _simple_ma(numeric_prices, self.slow_ma_period)
        previous_fast_ma = _simple_ma(
            numeric_prices[: -self.slope_lookback],
            self.fast_ma_period,
        )
        previous_slow_ma = _simple_ma(
            numeric_prices[: -self.slope_lookback],
            self.slow_ma_period,
        )
        if (
            fast_ma is None
            or slow_ma is None
            or previous_fast_ma is None
            or previous_slow_ma is None
        ):
            return self._low_data_context(
                symbol=normalized_symbol,
                timestamp=timestamp,
                data_points=data_points,
                reason="MOVING_AVERAGE_UNAVAILABLE",
                min_points=min_points,
            )

        fast_slope = _pct_change(previous_fast_ma, fast_ma)
        slow_slope = _pct_change(previous_slow_ma, slow_ma)
        distance_fast = _pct_change(fast_ma, price)
        distance_slow = _pct_change(slow_ma, price)
        volatility_pct = _volatility_pct(numeric_prices, self.volatility_lookback)
        chop_score = self._chop_score(numeric_prices)

        price_above_fast = price > fast_ma
        price_above_slow = price > slow_ma
        direction = self._direction(
            price=price,
            fast_ma=fast_ma,
            slow_ma=slow_ma,
            fast_slope=fast_slope,
            slow_slope=slow_slope,
        )
        directional_regime = _directional_regime(direction)
        regime = self._regime(
            direction=direction,
            fast_ma=fast_ma,
            slow_ma=slow_ma,
            fast_slope=fast_slope,
            slow_slope=slow_slope,
            distance_fast=distance_fast,
            distance_slow=distance_slow,
            volatility_pct=volatility_pct,
            chop_score=chop_score,
        )
        volatility_state = (
            "HIGH_VOLATILITY" if volatility_pct >= self.high_volatility_threshold_pct else "NORMAL"
        )
        confidence = self._confidence(
            direction=direction,
            regime=regime,
            fast_ma=fast_ma,
            slow_ma=slow_ma,
            fast_slope=fast_slope,
            slow_slope=slow_slope,
            distance_fast=distance_fast,
            distance_slow=distance_slow,
            chop_score=chop_score,
        )
        strength = self._strength(
            direction=direction,
            confidence=confidence,
            fast_slope=fast_slope,
            slow_slope=slow_slope,
            distance_slow=distance_slow,
        )

        diagnostics = {
            "engine": "TrendContextEngine",
            "fast_ma_period": self.fast_ma_period,
            "slow_ma_period": self.slow_ma_period,
            "slope_lookback": self.slope_lookback,
            "volatility_lookback": self.volatility_lookback,
            "high_volatility_threshold_pct": self.high_volatility_threshold_pct,
            "chop_threshold": self.chop_threshold,
            "ma_spread_pct": abs(_pct_change(slow_ma, fast_ma)),
            "primary_regime": (directional_regime or regime.value),
            "volatility_state": volatility_state,
            "is_directional": directional_regime is not None,
            "directional_regime": directional_regime or "NONE",
        }
        diagnostics.update(_trend_alignment_diagnostics(direction, regime))

        return TrendContext(
            symbol=normalized_symbol,
            timestamp=timestamp,
            direction=direction,
            strength=strength,
            regime=regime,
            confidence=confidence,
            fast_ma=fast_ma,
            slow_ma=slow_ma,
            fast_ma_slope=fast_slope,
            slow_ma_slope=slow_slope,
            price_above_fast_ma=price_above_fast,
            price_above_slow_ma=price_above_slow,
            price_distance_to_fast_ma_pct=distance_fast,
            price_distance_to_slow_ma_pct=distance_slow,
            volatility_pct=volatility_pct,
            chop_score=chop_score,
            data_points=data_points,
            diagnostics=diagnostics,
        )

    def _low_data_context(
        self,
        *,
        symbol: str,
        timestamp: str | float | None,
        data_points: int,
        reason: str,
        min_points: int,
    ) -> TrendContext:
        return TrendContext(
            symbol=symbol,
            timestamp=timestamp,
            direction=TrendDirection.UNKNOWN,
            strength=TrendStrength.UNKNOWN,
            regime=MarketRegime.LOW_DATA,
            confidence=0.05,
            fast_ma=None,
            slow_ma=None,
            fast_ma_slope=None,
            slow_ma_slope=None,
            price_above_fast_ma=None,
            price_above_slow_ma=None,
            price_distance_to_fast_ma_pct=None,
            price_distance_to_slow_ma_pct=None,
            volatility_pct=None,
            chop_score=None,
            data_points=int(data_points),
            diagnostics={
                "engine": "TrendContextEngine",
                "reason": reason,
                "min_points": int(min_points),
                "fast_ma_period": self.fast_ma_period,
                "slow_ma_period": self.slow_ma_period,
                "slope_lookback": self.slope_lookback,
                "execution_changed": False,
                "order_mutation": False,
            },
        )

    def _direction(
        self,
        *,
        price: float,
        fast_ma: float,
        slow_ma: float,
        fast_slope: float,
        slow_slope: float,
    ) -> TrendDirection:
        slope_epsilon = 0.001
        if (
            price > fast_ma
            and fast_ma > slow_ma
            and fast_slope > slope_epsilon
            and slow_slope >= -slope_epsilon
        ):
            return TrendDirection.UP
        if (
            price < fast_ma
            and fast_ma < slow_ma
            and fast_slope < -slope_epsilon
            and slow_slope <= slope_epsilon
        ):
            return TrendDirection.DOWN
        if abs(_pct_change(slow_ma, fast_ma)) <= 0.25 and abs(fast_slope) <= 0.05:
            return TrendDirection.SIDEWAYS
        return TrendDirection.UNKNOWN

    def _regime(
        self,
        *,
        direction: TrendDirection,
        fast_ma: float,
        slow_ma: float,
        fast_slope: float,
        slow_slope: float,
        distance_fast: float,
        distance_slow: float,
        volatility_pct: float,
        chop_score: float,
    ) -> MarketRegime:
        ma_spread = abs(_pct_change(slow_ma, fast_ma))
        weak_slope = abs(fast_slope) <= 0.05 and abs(slow_slope) <= 0.05
        near_averages = abs(distance_fast) <= 0.35 and abs(distance_slow) <= 0.60

        if volatility_pct >= self.high_volatility_threshold_pct:
            return MarketRegime.HIGH_VOLATILITY
        if chop_score >= self.chop_threshold and direction is not TrendDirection.UP:
            if direction is not TrendDirection.DOWN:
                return MarketRegime.CHOP
        if direction is TrendDirection.UP:
            return MarketRegime.TREND_UP
        if direction is TrendDirection.DOWN:
            return MarketRegime.TREND_DOWN
        if ma_spread <= 0.30 and weak_slope and near_averages:
            return MarketRegime.RANGE
        if chop_score >= self.chop_threshold:
            return MarketRegime.CHOP
        return MarketRegime.UNKNOWN

    def _confidence(
        self,
        *,
        direction: TrendDirection,
        regime: MarketRegime,
        fast_ma: float,
        slow_ma: float,
        fast_slope: float,
        slow_slope: float,
        distance_fast: float,
        distance_slow: float,
        chop_score: float,
    ) -> float:
        ma_spread = abs(_pct_change(slow_ma, fast_ma))
        slope_score = min(0.30, (abs(fast_slope) + abs(slow_slope)) / 2.0)
        distance_score = min(0.20, (abs(distance_fast) + abs(distance_slow)) / 8.0)

        if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
            score = 0.45 + slope_score + distance_score + min(0.20, ma_spread / 3.0)
            score -= min(0.20, chop_score / 5.0)
            return _clamp(score, 0.0, 1.0)

        if regime is MarketRegime.HIGH_VOLATILITY:
            directional_bonus = (
                0.15 if direction in {TrendDirection.UP, TrendDirection.DOWN} else 0.0
            )
            return _clamp(0.45 + directional_bonus + min(0.25, ma_spread / 3.0), 0.0, 1.0)

        if regime in {MarketRegime.RANGE, MarketRegime.CHOP}:
            clarity_penalty = min(0.20, ma_spread / 5.0)
            return _clamp(0.55 + min(0.25, chop_score / 3.0) - clarity_penalty, 0.0, 1.0)

        return 0.20

    def _strength(
        self,
        *,
        direction: TrendDirection,
        confidence: float,
        fast_slope: float,
        slow_slope: float,
        distance_slow: float,
    ) -> TrendStrength:
        if direction not in {TrendDirection.UP, TrendDirection.DOWN}:
            return TrendStrength.WEAK if confidence >= 0.35 else TrendStrength.UNKNOWN

        momentum = abs(fast_slope) + abs(slow_slope) + abs(distance_slow) / 3.0
        if confidence >= 0.72 and momentum >= 0.30:
            return TrendStrength.STRONG
        if confidence >= 0.50:
            return TrendStrength.MODERATE
        return TrendStrength.WEAK

    def _chop_score(self, prices: list[float]) -> float:
        start = max(self.fast_ma_period, len(prices) - self.volatility_lookback)
        signs: list[int] = []
        for index in range(start, len(prices)):
            window = prices[index - self.fast_ma_period + 1 : index + 1]
            if len(window) < self.fast_ma_period:
                continue
            ma = sum(window) / len(window)
            distance = prices[index] - ma
            signs.append(1 if distance > 0 else -1 if distance < 0 else 0)

        clean_signs = [sign for sign in signs if sign != 0]
        if len(clean_signs) < 2:
            return 0.0

        crossings = sum(
            1
            for previous, current in zip(clean_signs, clean_signs[1:], strict=False)
            if previous != current
        )
        crossing_rate = crossings / max(1, len(clean_signs) - 1)
        recent_prices = prices[-min(len(prices), self.volatility_lookback) :]
        recent_range_pct = _pct_change(min(recent_prices), max(recent_prices))
        range_noise = 1.0 if recent_range_pct <= 1.0 else max(0.0, 1.0 - recent_range_pct / 5.0)
        return _clamp(crossing_rate * 0.75 + range_noise * 0.25, 0.0, 1.0)


def _coerce_prices(prices: Sequence[Any]) -> list[float]:
    result: list[float] = []
    for value in prices:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(number) and number > 0:
            result.append(number)
    return result


def _simple_ma(prices: Sequence[float], period: int) -> float | None:
    if len(prices) < period:
        return None
    window = prices[-period:]
    return sum(window) / period


def _pct_change(previous: float, current: float) -> float:
    if previous == 0:
        return 0.0
    return (current - previous) / previous * 100.0


def _volatility_pct(prices: Sequence[float], lookback: int) -> float:
    if len(prices) < 2:
        return 0.0
    returns: list[float] = []
    start = max(1, len(prices) - lookback)
    for index in range(start, len(prices)):
        previous = prices[index - 1]
        current = prices[index]
        if previous == 0:
            continue
        returns.append((current - previous) / previous * 100.0)
    if not returns:
        return 0.0
    average = sum(returns) / len(returns)
    variance = sum((item - average) ** 2 for item in returns) / len(returns)
    return sqrt(variance)


def _trend_alignment_diagnostics(
    direction: TrendDirection,
    regime: MarketRegime,
) -> dict[str, str | float]:
    if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
        return {
            "trend_alignment_score": 1.0,
            "trend_alignment_reason": f"DIRECTIONAL_{direction.value}",
        }
    if regime in {MarketRegime.CHOP, MarketRegime.RANGE, MarketRegime.HIGH_VOLATILITY}:
        return {
            "trend_alignment_score": 0.0,
            "trend_alignment_reason": regime.value,
        }
    return {
        "trend_alignment_score": 0.0,
        "trend_alignment_reason": "UNCLEAR",
    }


def _directional_regime(direction: TrendDirection) -> str | None:
    if direction is TrendDirection.UP:
        return MarketRegime.TREND_UP.value
    if direction is TrendDirection.DOWN:
        return MarketRegime.TREND_DOWN.value
    return None


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
