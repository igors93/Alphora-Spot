from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeDecision
from domain.strategy_requirements import (
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    StrategyDataRequirements,
)
from indicators.common import to_decimal
from indicators.donchian_adx_breakout import (
    DonchianAdxBreakoutIndicators,
    calculate_donchian_adx_breakout_indicators,
    minimum_required_candles,
)
from indicators.moving_average import exponential_moving_average
from scanner.signal_score import SignalScore
from strategies.futures.base import FuturesStrategyV2, StrategyMetadata
from trading.intent import TradeIntent

_ZERO = Decimal("0")
_ONE_HUNDRED = Decimal("100")
_BASIS_POINTS = Decimal("10000")


@dataclass(frozen=True)
class HigherTimeframeTrend:
    close: Decimal
    ema: Decimal
    previous_ema: Decimal
    bullish: bool
    bearish: bool


@dataclass(frozen=True)
class PlannedExit:
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    risk_abs: Decimal
    reward_abs: Decimal
    estimated_cost_abs: Decimal
    net_reward_abs: Decimal
    net_risk_abs: Decimal
    liquid_reward_risk: Decimal
    raw_reward_risk: Decimal
    cost_to_reward_ratio: Decimal
    risk_pct: Decimal
    reward_pct: Decimal

    def as_metadata(self, *, side: str) -> dict[str, Any]:
        return {
            "recommended_stop_price": str(self.stop_price),
            "recommended_take_profit_price": str(self.take_profit_price),
            "entry_price": str(self.entry_price),
            "risk_abs": str(self.risk_abs),
            "reward_abs": str(self.reward_abs),
            "estimated_cost_abs": str(self.estimated_cost_abs),
            "net_reward_abs": str(self.net_reward_abs),
            "net_risk_abs": str(self.net_risk_abs),
            "reward_risk_liquido": str(self.liquid_reward_risk),
            "raw_reward_risk": str(self.raw_reward_risk),
            "cost_to_reward_ratio": str(self.cost_to_reward_ratio),
            "risk_pct": str(self.risk_pct),
            "reward_pct": str(self.reward_pct),
            "setup_math_side": side,
        }


class DonchianAdxBreakoutStrategy(FuturesStrategyV2):
    """Five-minute Donchian breakout strategy with ADX, volume, and 1h trend filters."""

    strategy_category = "FIXED_BREAKOUT"

    metadata = StrategyMetadata(
        strategy_name="DonchianAdxBreakoutStrategy",
        supports_scoring=True,
        supports_long=True,
        supports_short=True,
        required_history=300,
        required_timeframes=["5m", "1h"],
        required_indicators=[
            "DONCHIAN",
            "ADX",
            "ATR",
            "EMA",
            "RELATIVE_VOLUME",
        ],
    )

    def __init__(
        self,
        *,
        primary_timeframe: str = "5m",
        higher_timeframe: str = "1h",
        primary_history: int = 300,
        higher_timeframe_history: int = 200,
        donchian_period: int = 20,
        adx_period: int = 14,
        adx_threshold: Decimal | int | float | str = Decimal("25"),
        primary_ema_period: int = 50,
        higher_timeframe_ema_period: int = 50,
        atr_period: int = 14,
        volume_period: int = 20,
        volume_multiplier: Decimal | int | float | str = Decimal("1.3"),
        stop_atr_multiplier: Decimal | int | float | str = Decimal("1.5"),
        reward_risk_ratio: Decimal | int | float | str = Decimal("2"),
        minimum_liquid_reward_risk: Decimal | int | float | str = Decimal("1.5"),
        estimated_round_trip_cost_bps: Decimal | int | float | str = Decimal("15"),
        enable_long: bool = True,
        enable_short: bool = True,
    ) -> None:
        self.primary_timeframe = _non_empty_text(primary_timeframe, "primary_timeframe")
        self.higher_timeframe = _non_empty_text(higher_timeframe, "higher_timeframe")

        if self.primary_timeframe == self.higher_timeframe:
            raise ValueError("primary_timeframe and higher_timeframe must be different")

        self.donchian_period = _positive_int(donchian_period, "donchian_period")
        self.adx_period = _positive_int(adx_period, "adx_period")
        self.primary_ema_period = _positive_int(primary_ema_period, "primary_ema_period")
        self.higher_timeframe_ema_period = _positive_int(
            higher_timeframe_ema_period,
            "higher_timeframe_ema_period",
        )
        self.atr_period = _positive_int(atr_period, "atr_period")
        self.volume_period = _positive_int(volume_period, "volume_period")

        self.adx_threshold = _non_negative_decimal(adx_threshold, "adx_threshold")
        self.volume_multiplier = _positive_decimal(volume_multiplier, "volume_multiplier")
        self.stop_atr_multiplier = _positive_decimal(
            stop_atr_multiplier,
            "stop_atr_multiplier",
        )
        self.reward_risk_ratio = _positive_decimal(
            reward_risk_ratio,
            "reward_risk_ratio",
        )
        self.minimum_liquid_reward_risk = _positive_decimal(
            minimum_liquid_reward_risk,
            "minimum_liquid_reward_risk",
        )
        self.estimated_round_trip_cost_bps = _non_negative_decimal(
            estimated_round_trip_cost_bps,
            "estimated_round_trip_cost_bps",
        )

        self.enable_long = bool(enable_long)
        self.enable_short = bool(enable_short)

        if not self.enable_long and not self.enable_short:
            raise ValueError("at least one trade direction must be enabled")

        minimum_primary_history = minimum_required_candles(
            donchian_period=self.donchian_period,
            adx_period=self.adx_period,
            ema_period=self.primary_ema_period,
            atr_period=self.atr_period,
            volume_period=self.volume_period,
        )
        self.primary_history = _positive_int(primary_history, "primary_history")
        if self.primary_history < minimum_primary_history:
            raise ValueError(
                "primary_history is too small for the configured indicators: "
                f"required>={minimum_primary_history}, received={self.primary_history}"
            )

        minimum_higher_history = self.higher_timeframe_ema_period + 1
        self.higher_timeframe_history = _positive_int(
            higher_timeframe_history,
            "higher_timeframe_history",
        )
        if self.higher_timeframe_history < minimum_higher_history:
            raise ValueError(
                "higher_timeframe_history is too small for EMA direction: "
                f"required>={minimum_higher_history}, "
                f"received={self.higher_timeframe_history}"
            )

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> DonchianAdxBreakoutStrategy:
        return cls(
            primary_timeframe=str(params.get("primary_timeframe", "5m")),
            higher_timeframe=str(params.get("higher_timeframe", "1h")),
            primary_history=int(params.get("primary_history", 300)),
            higher_timeframe_history=int(params.get("higher_timeframe_history", 200)),
            donchian_period=int(params.get("donchian_period", 20)),
            adx_period=int(params.get("adx_period", 14)),
            adx_threshold=params.get("adx_threshold", "25"),
            primary_ema_period=int(params.get("primary_ema_period", 50)),
            higher_timeframe_ema_period=int(params.get("higher_timeframe_ema_period", 50)),
            atr_period=int(params.get("atr_period", 14)),
            volume_period=int(params.get("volume_period", 20)),
            volume_multiplier=params.get("volume_multiplier", "1.3"),
            stop_atr_multiplier=params.get("stop_atr_multiplier", "1.5"),
            reward_risk_ratio=params.get("reward_risk_ratio", "2"),
            minimum_liquid_reward_risk=params.get(
                "minimum_liquid_reward_risk",
                "1.5",
            ),
            estimated_round_trip_cost_bps=params.get(
                "estimated_round_trip_cost_bps",
                "15",
            ),
            enable_long=_bool_param(params.get("enable_long", True)),
            enable_short=_bool_param(params.get("enable_short", True)),
        )

    @classmethod
    def describe_parameters(cls) -> dict[str, Any]:
        return {
            "primary_timeframe": {
                "default": "5m",
                "description": "Primary candle interval used for breakout signals.",
            },
            "higher_timeframe": {
                "default": "1h",
                "description": "Higher interval used as the directional trend filter.",
            },
            "primary_history": {
                "default": 300,
                "minimum": 50,
                "description": "Closed primary candles requested during warm-up.",
            },
            "higher_timeframe_history": {
                "default": 200,
                "minimum": 51,
                "description": "Closed higher-timeframe candles requested during warm-up.",
            },
            "donchian_period": {"default": 20, "minimum": 2},
            "adx_period": {"default": 14, "minimum": 2},
            "adx_threshold": {"default": "25", "minimum": "0"},
            "primary_ema_period": {"default": 50, "minimum": 2},
            "higher_timeframe_ema_period": {"default": 50, "minimum": 2},
            "atr_period": {"default": 14, "minimum": 2},
            "volume_period": {"default": 20, "minimum": 2},
            "volume_multiplier": {"default": "1.3", "minimum": "0.01"},
            "stop_atr_multiplier": {"default": "1.5", "minimum": "0.01"},
            "reward_risk_ratio": {"default": "2", "minimum": "0.01"},
            "minimum_liquid_reward_risk": {
                "default": "1.5",
                "minimum": "0.01",
            },
            "estimated_round_trip_cost_bps": {
                "default": "15",
                "minimum": "0",
            },
            "enable_long": {"default": True},
            "enable_short": {"default": True},
        }

    def data_requirements(self) -> StrategyDataRequirements:
        return StrategyDataRequirements(
            primary_timeframe=self.primary_timeframe,
            required_candles={
                self.primary_timeframe: self.primary_history,
                self.higher_timeframe: self.higher_timeframe_history,
            },
            evaluation_policy=EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
            closed_candles_only=True,
        )

    def score_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> SignalScore:
        normalized_symbol = symbol.upper().strip()
        strategy_name = self.__class__.__name__

        if market.stale:
            return self._remember(
                state,
                SignalScore(
                    symbol=normalized_symbol,
                    score=0,
                    reason="Donchian ADX: market snapshot is stale",
                    action="HOLD",
                    confidence=0,
                    strategy=strategy_name,
                    data_age_seconds=market.data_age_seconds,
                ),
            )

        candles_by_timeframe = market.candles
        if not candles_by_timeframe:
            return self._remember(
                state,
                SignalScore(
                    symbol=normalized_symbol,
                    score=0,
                    reason="Donchian ADX: candle data is unavailable",
                    action="HOLD",
                    confidence=0,
                    strategy=strategy_name,
                    data_age_seconds=market.data_age_seconds,
                ),
            )

        primary_candles = candles_by_timeframe.get(self.primary_timeframe, ())
        higher_candles = candles_by_timeframe.get(self.higher_timeframe, ())

        if len(primary_candles) < self.primary_history:
            return self._remember(
                state,
                SignalScore(
                    symbol=normalized_symbol,
                    score=0,
                    reason=(
                        "Donchian ADX: primary warm-up incomplete "
                        f"({len(primary_candles)}/{self.primary_history})"
                    ),
                    action="HOLD",
                    confidence=0,
                    strategy=strategy_name,
                    data_age_seconds=market.data_age_seconds,
                ),
            )

        if len(higher_candles) < self.higher_timeframe_history:
            return self._remember(
                state,
                SignalScore(
                    symbol=normalized_symbol,
                    score=0,
                    reason=(
                        "Donchian ADX: higher-timeframe warm-up incomplete "
                        f"({len(higher_candles)}/{self.higher_timeframe_history})"
                    ),
                    action="HOLD",
                    confidence=0,
                    strategy=strategy_name,
                    data_age_seconds=market.data_age_seconds,
                ),
            )

        if not bool(getattr(primary_candles[-1], "is_closed", False)):
            return self._remember(
                state,
                SignalScore(
                    symbol=normalized_symbol,
                    score=0,
                    reason="Donchian ADX: waiting for the primary candle to close",
                    action="HOLD",
                    confidence=0,
                    strategy=strategy_name,
                    data_age_seconds=market.data_age_seconds,
                ),
            )

        indicators = calculate_donchian_adx_breakout_indicators(
            primary_candles,
            donchian_period=self.donchian_period,
            adx_period=self.adx_period,
            ema_period=self.primary_ema_period,
            atr_period=self.atr_period,
            volume_period=self.volume_period,
            volume_multiplier=self.volume_multiplier,
            adx_threshold=self.adx_threshold,
            require_closed_candle=True,
        )
        trend = _higher_timeframe_trend(
            higher_candles,
            ema_period=self.higher_timeframe_ema_period,
        )

        if indicators is None or trend is None:
            return self._remember(
                state,
                SignalScore(
                    symbol=normalized_symbol,
                    score=0,
                    reason="Donchian ADX: indicators are not ready",
                    action="HOLD",
                    confidence=0,
                    strategy=strategy_name,
                    data_age_seconds=market.data_age_seconds,
                ),
            )

        long_conditions = {
            "breakout": indicators.bullish_breakout,
            "primary_ema": indicators.above_ema,
            "directional_bias": indicators.dmi.bullish_bias,
            "adx_strength": indicators.dmi.adx >= self.adx_threshold,
            "adx_rising": indicators.dmi.adx_rising,
            "relative_volume": indicators.relative_volume.confirmed,
            "higher_timeframe": trend.bullish,
        }
        short_conditions = {
            "breakout": indicators.bearish_breakout,
            "primary_ema": indicators.below_ema,
            "directional_bias": indicators.dmi.bearish_bias,
            "adx_strength": indicators.dmi.adx >= self.adx_threshold,
            "adx_rising": indicators.dmi.adx_rising,
            "relative_volume": indicators.relative_volume.confirmed,
            "higher_timeframe": trend.bearish,
        }

        long_ready = self.enable_long and all(long_conditions.values())
        short_ready = self.enable_short and all(short_conditions.values())

        long_score = _condition_score(long_conditions)
        short_score = _condition_score(short_conditions)
        raw_score = max(long_score, short_score)

        action = _resolve_action(
            position=position,
            long_ready=long_ready,
            short_ready=short_ready,
        )

        setup_side = ""
        if long_ready:
            setup_side = "LONG"
        elif short_ready:
            setup_side = "SHORT"

        planned_exit: PlannedExit | None = None
        if action in {"OPEN_LONG", "OPEN_SHORT"}:
            entry_price = (
                to_decimal(market.price) if to_decimal(market.price) > _ZERO else indicators.close
            )
            planned_exit = _build_planned_exit(
                action=action,
                entry_price=entry_price,
                atr=indicators.atr,
                stop_atr_multiplier=self.stop_atr_multiplier,
                reward_risk_ratio=self.reward_risk_ratio,
                estimated_round_trip_cost_bps=self.estimated_round_trip_cost_bps,
            )
            if (
                planned_exit is None
                or planned_exit.liquid_reward_risk < self.minimum_liquid_reward_risk
            ):
                action = "HOLD"

        score = (
            100
            if action
            in {
                "OPEN_LONG",
                "OPEN_SHORT",
                "CLOSE_POSITION",
            }
            else min(raw_score, 89)
        )

        reason = _build_reason(
            action=action,
            long_score=long_score,
            short_score=short_score,
            indicators=indicators,
            trend=trend,
            setup_side=setup_side,
            planned_exit=planned_exit,
            minimum_liquid_reward_risk=self.minimum_liquid_reward_risk,
        )

        metadata = _indicator_metadata(
            indicators=indicators,
            trend=trend,
            long_conditions=long_conditions,
            short_conditions=short_conditions,
            long_score=long_score,
            short_score=short_score,
        )

        if planned_exit is not None and action in {"OPEN_LONG", "OPEN_SHORT"}:
            side = "LONG" if action == "OPEN_LONG" else "SHORT"
            metadata.update(planned_exit.as_metadata(side=side))

        return self._remember(
            state,
            SignalScore(
                symbol=normalized_symbol,
                score=score,
                reason=reason,
                action=action,
                confidence=score,
                strategy=strategy_name,
                suggested_next_check_seconds=(
                    0.0 if action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"} else 300.0
                ),
                data_age_seconds=market.data_age_seconds,
                metadata=metadata,
            ),
        )

    def intent_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> TradeIntent:
        action = str(state.get("donchian_adx_action") or "HOLD")
        reason = str(state.get("donchian_adx_reason") or "Donchian ADX: no evaluated signal")
        score = int(state.get("donchian_adx_score", 0) or 0)
        metadata = dict(state.get("donchian_adx_metadata") or {})

        return TradeIntent(
            symbol=symbol,
            action=action,
            confidence=score,
            score=score,
            reason=reason,
            strategy_name=self.__class__.__name__,
            market_price=market.price,
            suggested_next_check_seconds=(
                0.0 if action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"} else 300.0
            ),
            metadata=metadata,
        )

    def decide_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> TradeDecision:
        action = str(state.get("donchian_adx_action") or "HOLD")
        reason = str(state.get("donchian_adx_reason") or "Donchian ADX: no evaluated signal")

        if action == "OPEN_LONG":
            return TradeDecision.open_long(reason)
        if action == "OPEN_SHORT":
            return TradeDecision.open_short(reason)
        if action == "CLOSE_POSITION":
            return TradeDecision.close(reason)
        return TradeDecision.hold(reason)

    @staticmethod
    def _remember(
        state: dict[str, Any],
        score: SignalScore,
    ) -> SignalScore:
        state["donchian_adx_action"] = score.action or "HOLD"
        state["donchian_adx_reason"] = score.reason
        state["donchian_adx_score"] = int(score.score)
        state["donchian_adx_metadata"] = dict(score.metadata)
        return score


def _higher_timeframe_trend(
    candles: Any,
    *,
    ema_period: int,
) -> HigherTimeframeTrend | None:
    # A still-forming higher-timeframe candle leaks future information into
    # backtests and makes live signals flicker; only closed candles count.
    closed = [candle for candle in candles if bool(getattr(candle, "is_closed", True))]
    if len(closed) < ema_period + 1:
        return None

    closes = [to_decimal(candle.close) for candle in closed]
    current_ema = exponential_moving_average(closes, ema_period)
    previous_ema = exponential_moving_average(closes[:-1], ema_period)

    if current_ema is None or previous_ema is None:
        return None

    current_close = closes[-1]

    return HigherTimeframeTrend(
        close=current_close,
        ema=current_ema,
        previous_ema=previous_ema,
        bullish=current_close > current_ema and current_ema > previous_ema,
        bearish=current_close < current_ema and current_ema < previous_ema,
    )


def _condition_score(conditions: dict[str, bool]) -> int:
    weights = {
        "breakout": 30,
        "primary_ema": 15,
        "directional_bias": 10,
        "adx_strength": 15,
        "adx_rising": 10,
        "relative_volume": 10,
        "higher_timeframe": 10,
    }
    return sum(weights[name] for name, passed in conditions.items() if passed)


def _resolve_action(
    *,
    position: FuturesPosition,
    long_ready: bool,
    short_ready: bool,
) -> str:
    if not position.is_open:
        if long_ready:
            return "OPEN_LONG"
        if short_ready:
            return "OPEN_SHORT"
        return "HOLD"

    if position.direction is PositionDirection.LONG and short_ready:
        return "CLOSE_POSITION"

    if position.direction is PositionDirection.SHORT and long_ready:
        return "CLOSE_POSITION"

    return "HOLD"


def _build_planned_exit(
    *,
    action: str,
    entry_price: Decimal,
    atr: Decimal,
    stop_atr_multiplier: Decimal,
    reward_risk_ratio: Decimal,
    estimated_round_trip_cost_bps: Decimal,
) -> PlannedExit | None:
    if entry_price <= _ZERO or atr <= _ZERO:
        return None

    risk_abs = atr * stop_atr_multiplier
    reward_abs = risk_abs * reward_risk_ratio

    if risk_abs <= _ZERO or reward_abs <= _ZERO:
        return None

    if action == "OPEN_LONG":
        stop_price = entry_price - risk_abs
        take_profit_price = entry_price + reward_abs
    elif action == "OPEN_SHORT":
        stop_price = entry_price + risk_abs
        take_profit_price = entry_price - reward_abs
    else:
        return None

    if stop_price <= _ZERO or take_profit_price <= _ZERO:
        return None

    estimated_cost_abs = entry_price * estimated_round_trip_cost_bps / _BASIS_POINTS
    net_reward_abs = reward_abs - estimated_cost_abs
    net_risk_abs = risk_abs + estimated_cost_abs

    if net_reward_abs <= _ZERO or net_risk_abs <= _ZERO:
        return None

    liquid_reward_risk = net_reward_abs / net_risk_abs
    raw_reward_risk = reward_abs / risk_abs
    cost_to_reward_ratio = estimated_cost_abs / reward_abs
    risk_pct = risk_abs / entry_price
    reward_pct = reward_abs / entry_price

    return PlannedExit(
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        risk_abs=risk_abs,
        reward_abs=reward_abs,
        estimated_cost_abs=estimated_cost_abs,
        net_reward_abs=net_reward_abs,
        net_risk_abs=net_risk_abs,
        liquid_reward_risk=liquid_reward_risk,
        raw_reward_risk=raw_reward_risk,
        cost_to_reward_ratio=cost_to_reward_ratio,
        risk_pct=risk_pct,
        reward_pct=reward_pct,
    )


def _build_reason(
    *,
    action: str,
    long_score: int,
    short_score: int,
    indicators: DonchianAdxBreakoutIndicators,
    trend: HigherTimeframeTrend,
    setup_side: str,
    planned_exit: PlannedExit | None,
    minimum_liquid_reward_risk: Decimal,
) -> str:
    if action == "OPEN_LONG":
        return (
            "Donchian ADX LONG confirmed: breakout, rising ADX, relative "
            "volume, primary EMA, and higher-timeframe trend agree"
        )

    if action == "OPEN_SHORT":
        return (
            "Donchian ADX SHORT confirmed: breakout, rising ADX, relative "
            "volume, primary EMA, and higher-timeframe trend agree"
        )

    if action == "CLOSE_POSITION":
        return (
            f"Donchian ADX opposite {setup_side} setup confirmed; "
            "closing the current position before any reversal"
        )

    if (
        setup_side
        and planned_exit is not None
        and planned_exit.liquid_reward_risk < minimum_liquid_reward_risk
    ):
        return (
            "Donchian ADX setup rejected: estimated liquid reward/risk "
            f"{planned_exit.liquid_reward_risk:.4f} is below "
            f"{minimum_liquid_reward_risk:.4f}"
        )

    return (
        "Donchian ADX waiting: "
        f"long_score={long_score}, short_score={short_score}, "
        f"adx={indicators.dmi.adx:.4f}, "
        f"relative_volume={_decimal_or_zero(indicators.relative_volume.ratio):.4f}, "
        f"higher_timeframe_bullish={trend.bullish}, "
        f"higher_timeframe_bearish={trend.bearish}"
    )


def _indicator_metadata(
    *,
    indicators: DonchianAdxBreakoutIndicators,
    trend: HigherTimeframeTrend,
    long_conditions: dict[str, bool],
    short_conditions: dict[str, bool],
    long_score: int,
    short_score: int,
) -> dict[str, Any]:
    return {
        "source": "FIXED_DONCHIAN_ADX_BREAKOUT",
        "close": str(indicators.close),
        "primary_ema": str(indicators.ema),
        "atr": str(indicators.atr),
        "donchian_upper": str(indicators.donchian.upper),
        "donchian_lower": str(indicators.donchian.lower),
        "donchian_middle": str(indicators.donchian.middle),
        "adx": str(indicators.dmi.adx),
        "previous_adx": (
            str(indicators.dmi.previous_adx) if indicators.dmi.previous_adx is not None else None
        ),
        "plus_di": str(indicators.dmi.plus_di),
        "minus_di": str(indicators.dmi.minus_di),
        "relative_volume_ratio": (
            str(indicators.relative_volume.ratio)
            if indicators.relative_volume.ratio is not None
            else None
        ),
        "current_volume": str(indicators.relative_volume.current),
        "average_volume": str(indicators.relative_volume.average),
        "higher_timeframe_close": str(trend.close),
        "higher_timeframe_ema": str(trend.ema),
        "higher_timeframe_previous_ema": str(trend.previous_ema),
        "higher_timeframe_bullish": trend.bullish,
        "higher_timeframe_bearish": trend.bearish,
        "long_score": long_score,
        "short_score": short_score,
        "long_conditions": dict(long_conditions),
        "short_conditions": dict(short_conditions),
    }


def _positive_int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error

    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")

    return result


def _positive_decimal(value: Any, name: str) -> Decimal:
    result = _decimal(value, name)

    if result <= _ZERO:
        raise ValueError(f"{name} must be greater than zero")

    return result


def _non_negative_decimal(value: Any, name: str) -> Decimal:
    result = _decimal(value, name)

    if result < _ZERO:
        raise ValueError(f"{name} cannot be negative")

    return result


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a valid decimal") from error

    if not result.is_finite():
        raise ValueError(f"{name} must be finite")

    return result


def _bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False

    return bool(value)


def _non_empty_text(value: Any, name: str) -> str:
    result = str(value or "").strip()

    if not result:
        raise ValueError(f"{name} cannot be empty")

    return result


def _decimal_or_zero(value: Decimal | None) -> Decimal:
    return value if value is not None else _ZERO


__all__ = [
    "DonchianAdxBreakoutStrategy",
    "HigherTimeframeTrend",
    "PlannedExit",
]
