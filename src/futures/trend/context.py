from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TrendDirection(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    SIDEWAYS = "SIDEWAYS"
    UNKNOWN = "UNKNOWN"


class TrendStrength(str, Enum):
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"
    UNKNOWN = "UNKNOWN"


class MarketRegime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    CHOP = "CHOP"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_DATA = "LOW_DATA"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TrendContext:
    symbol: str
    timestamp: str | float | None
    direction: TrendDirection
    strength: TrendStrength
    regime: MarketRegime
    confidence: float
    fast_ma: float | None
    slow_ma: float | None
    fast_ma_slope: float | None
    slow_ma_slope: float | None
    price_above_fast_ma: bool | None
    price_above_slow_ma: bool | None
    price_distance_to_fast_ma_pct: float | None
    price_distance_to_slow_ma_pct: float | None
    volatility_pct: float | None
    chop_score: float | None
    data_points: int
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "direction": self.direction.value,
            "strength": self.strength.value,
            "regime": self.regime.value,
            "confidence": _json_number(self.confidence),
            "fast_ma": _json_optional_number(self.fast_ma),
            "slow_ma": _json_optional_number(self.slow_ma),
            "fast_ma_slope": _json_optional_number(self.fast_ma_slope),
            "slow_ma_slope": _json_optional_number(self.slow_ma_slope),
            "price_above_fast_ma": self.price_above_fast_ma,
            "price_above_slow_ma": self.price_above_slow_ma,
            "price_distance_to_fast_ma_pct": _json_optional_number(
                self.price_distance_to_fast_ma_pct
            ),
            "price_distance_to_slow_ma_pct": _json_optional_number(
                self.price_distance_to_slow_ma_pct
            ),
            "volatility_pct": _json_optional_number(self.volatility_pct),
            "chop_score": _json_optional_number(self.chop_score),
            "data_points": int(self.data_points),
            "diagnostics": _json_safe_dict(self.diagnostics),
        }


def trend_context_to_features(context: TrendContext) -> dict[str, float | int | str]:
    direction = context.direction
    strength = context.strength
    regime = context.regime
    return {
        "trend_direction_encoded": {
            TrendDirection.DOWN: -1,
            TrendDirection.UNKNOWN: 0,
            TrendDirection.SIDEWAYS: 0,
            TrendDirection.UP: 1,
        }[direction],
        "trend_strength_score": {
            TrendStrength.UNKNOWN: 0,
            TrendStrength.WEAK: 1,
            TrendStrength.MODERATE: 2,
            TrendStrength.STRONG: 3,
        }[strength],
        "trend_confidence": _json_number(context.confidence),
        "regime_encoded": {
            MarketRegime.UNKNOWN: 0,
            MarketRegime.LOW_DATA: 0,
            MarketRegime.RANGE: 1,
            MarketRegime.CHOP: 2,
            MarketRegime.HIGH_VOLATILITY: 3,
            MarketRegime.TREND_UP: 4,
            MarketRegime.TREND_DOWN: 5,
        }[regime],
        "price_distance_to_fast_ma_pct": _json_number(context.price_distance_to_fast_ma_pct or 0.0),
        "price_distance_to_slow_ma_pct": _json_number(context.price_distance_to_slow_ma_pct or 0.0),
        "fast_ma_slope_pct": _json_number(context.fast_ma_slope or 0.0),
        "slow_ma_slope_pct": _json_number(context.slow_ma_slope or 0.0),
        "volatility_pct": _json_number(context.volatility_pct or 0.0),
        "chop_score": _json_number(context.chop_score or 0.0),
        "is_trend_up": int(regime is MarketRegime.TREND_UP or direction is TrendDirection.UP),
        "is_trend_down": int(regime is MarketRegime.TREND_DOWN or direction is TrendDirection.DOWN),
        "is_range": int(regime is MarketRegime.RANGE),
        "is_chop": int(regime is MarketRegime.CHOP),
        "is_high_volatility": int(regime is MarketRegime.HIGH_VOLATILITY),
        "trend_direction": direction.value,
        "trend_strength": strength.value,
        "market_regime": regime.value,
    }


def _json_number(value: float | int) -> float:
    return float(value)


def _json_optional_number(value: float | int | None) -> float | None:
    if value is None:
        return None
    return _json_number(value)


def _json_safe_dict(values: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values.items():
        text_key = str(key)
        if isinstance(value, Enum):
            result[text_key] = value.value
        elif isinstance(value, bool | int | float | str) or value is None:
            result[text_key] = value
        elif isinstance(value, list | tuple):
            result[text_key] = [
                item.value if isinstance(item, Enum) else item
                for item in value
                if isinstance(item, bool | int | float | str | Enum) or item is None
            ]
        else:
            result[text_key] = str(value)
    return result
