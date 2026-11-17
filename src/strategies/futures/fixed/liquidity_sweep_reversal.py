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
from indicators.liquidity_sweep import (
    LiquiditySweepIndicators,
    calculate_liquidity_sweep_indicators,
    minimum_liquidity_sweep_candles,
)
from indicators.moving_average import exponential_moving_average
from scanner.signal_score import SignalScore
from strategies.futures.base import FuturesStrategyV2, StrategyMetadata
from strategies.futures.fixed.donchian_adx_breakout import PlannedExit
from trading.intent import TradeIntent

_ZERO = Decimal("0")
_BASIS_POINTS = Decimal("10000")


@dataclass(frozen=True)
class HigherTimeframeBias:
    close: Decimal
    ema: Decimal
    previous_ema: Decimal
    decisively_bullish: bool
    decisively_bearish: bool


class LiquiditySweepReversalStrategy(FuturesStrategyV2):
    """Fifteen-minute liquidity-sweep (failed breakout) reversal strategy.

    Instead of following breakouts, this strategy trades their failure: a
    candle whose wick pierces a prior swing extreme — where resting stop
    orders cluster — but closes back inside the range. The reversal is only
    taken with a volume climax (z-score anomaly), a dominant rejection wick,
    a sweep depth that is meaningful yet not a genuine breakdown, and a
    higher-timeframe filter that refuses to fade a decisive trend. The stop
    sits just beyond the swept extreme, so risk is defined by structure
    rather than by a fixed ATR multiple.
    """

    strategy_category = "FIXED_REVERSAL"

    metadata = StrategyMetadata(
        strategy_name="LiquiditySweepReversalStrategy",
        supports_scoring=True,
        supports_long=True,
        supports_short=True,
        required_history=200,
        required_timeframes=["15m", "1h"],
        required_indicators=[
            "LIQUIDITY_SWEEP",
            "VOLUME_ZSCORE",
            "ATR",
            "EMA",
        ],
    )

    def __init__(
        self,
        *,
        primary_timeframe: str = "15m",
        higher_timeframe: str = "1h",
        primary_history: int = 200,
        higher_timeframe_history: int = 120,
        swing_period: int = 20,
        atr_period: int = 14,
        volume_period: int = 30,
        volume_zscore_threshold: Decimal | int | float | str = Decimal("1.5"),
        max_volume_zscore: Decimal | int | float | str = Decimal("10"),
        min_rejection_wick_fraction: Decimal | int | float | str = Decimal("0.35"),
        min_sweep_depth_atr: Decimal | int | float | str = Decimal("0.15"),
        max_sweep_depth_atr: Decimal | int | float | str = Decimal("2"),
        higher_timeframe_ema_period: int = 50,
        stop_buffer_atr: Decimal | int | float | str = Decimal("0.25"),
        reward_risk_ratio: Decimal | int | float | str = Decimal("1.6"),
        minimum_liquid_reward_risk: Decimal | int | float | str = Decimal("1.2"),
        estimated_round_trip_cost_bps: Decimal | int | float | str = Decimal("15"),
        entry_confirmation_candles: int = 0,
        require_confirmation_body_direction: bool = True,
        enable_long: bool = True,
        enable_short: bool = True,
    ) -> None:
        self.primary_timeframe = _non_empty_text(primary_timeframe, "primary_timeframe")
        self.higher_timeframe = _non_empty_text(higher_timeframe, "higher_timeframe")

        if self.primary_timeframe == self.higher_timeframe:
            raise ValueError("primary_timeframe and higher_timeframe must be different")

        self.swing_period = _positive_int(swing_period, "swing_period")
        if self.swing_period < 2:
            raise ValueError("swing_period must be at least 2")
        self.atr_period = _positive_int(atr_period, "atr_period")
        self.volume_period = _positive_int(volume_period, "volume_period")
        if self.volume_period < 2:
            raise ValueError("volume_period must be at least 2")
        self.higher_timeframe_ema_period = _positive_int(
            higher_timeframe_ema_period,
            "higher_timeframe_ema_period",
        )

        self.volume_zscore_threshold = _non_negative_decimal(
            volume_zscore_threshold,
            "volume_zscore_threshold",
        )
        self.max_volume_zscore = _positive_decimal(
            max_volume_zscore,
            "max_volume_zscore",
        )
        if self.max_volume_zscore <= self.volume_zscore_threshold:
            raise ValueError("max_volume_zscore must be above volume_zscore_threshold")
        self.min_rejection_wick_fraction = _non_negative_decimal(
            min_rejection_wick_fraction,
            "min_rejection_wick_fraction",
        )
        if self.min_rejection_wick_fraction >= Decimal("1"):
            raise ValueError("min_rejection_wick_fraction must be below 1")
        self.min_sweep_depth_atr = _non_negative_decimal(
            min_sweep_depth_atr,
            "min_sweep_depth_atr",
        )
        self.max_sweep_depth_atr = _positive_decimal(
            max_sweep_depth_atr,
            "max_sweep_depth_atr",
        )
        if self.max_sweep_depth_atr <= self.min_sweep_depth_atr:
            raise ValueError("max_sweep_depth_atr must be above min_sweep_depth_atr")
        self.stop_buffer_atr = _non_negative_decimal(stop_buffer_atr, "stop_buffer_atr")
        self.reward_risk_ratio = _positive_decimal(reward_risk_ratio, "reward_risk_ratio")
        self.minimum_liquid_reward_risk = _positive_decimal(
            minimum_liquid_reward_risk,
            "minimum_liquid_reward_risk",
        )
        self.estimated_round_trip_cost_bps = _non_negative_decimal(
            estimated_round_trip_cost_bps,
            "estimated_round_trip_cost_bps",
        )
        self.entry_confirmation_candles = int(entry_confirmation_candles)
        if self.entry_confirmation_candles not in {0, 1}:
            raise ValueError("entry_confirmation_candles must be 0 or 1")
        self.require_confirmation_body_direction = bool(require_confirmation_body_direction)

        self.enable_long = bool(enable_long)
        self.enable_short = bool(enable_short)
        if not self.enable_long and not self.enable_short:
            raise ValueError("at least one trade direction must be enabled")

        minimum_primary_history = minimum_liquidity_sweep_candles(
            swing_period=self.swing_period,
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
    def from_params(cls, params: dict[str, Any]) -> LiquiditySweepReversalStrategy:
        return cls(
            primary_timeframe=str(params.get("primary_timeframe", "15m")),
            higher_timeframe=str(params.get("higher_timeframe", "1h")),
            primary_history=int(params.get("primary_history", 200)),
            higher_timeframe_history=int(params.get("higher_timeframe_history", 120)),
            swing_period=int(params.get("swing_period", 20)),
            atr_period=int(params.get("atr_period", 14)),
            volume_period=int(params.get("volume_period", 30)),
            volume_zscore_threshold=params.get("volume_zscore_threshold", "1.5"),
            max_volume_zscore=params.get("max_volume_zscore", "10"),
            min_rejection_wick_fraction=params.get("min_rejection_wick_fraction", "0.35"),
            min_sweep_depth_atr=params.get("min_sweep_depth_atr", "0.15"),
            max_sweep_depth_atr=params.get("max_sweep_depth_atr", "2"),
            higher_timeframe_ema_period=int(params.get("higher_timeframe_ema_period", 50)),
            stop_buffer_atr=params.get("stop_buffer_atr", "0.25"),
            reward_risk_ratio=params.get("reward_risk_ratio", "1.6"),
            minimum_liquid_reward_risk=params.get("minimum_liquid_reward_risk", "1.2"),
            estimated_round_trip_cost_bps=params.get("estimated_round_trip_cost_bps", "15"),
            entry_confirmation_candles=int(params.get("entry_confirmation_candles", 0)),
            require_confirmation_body_direction=_bool_param(
                params.get("require_confirmation_body_direction", True)
            ),
            enable_long=_bool_param(params.get("enable_long", True)),
            enable_short=_bool_param(params.get("enable_short", True)),
        )

    @classmethod
    def describe_parameters(cls) -> dict[str, Any]:
        return {
            "primary_timeframe": {
                "default": "15m",
                "description": "Primary candle interval used to detect the sweep.",
            },
            "higher_timeframe": {
                "default": "1h",
                "description": "Higher interval used as the counter-trend guard.",
            },
            "primary_history": {
                "default": 200,
                "minimum": 32,
                "description": "Closed primary candles requested during warm-up.",
            },
            "higher_timeframe_history": {
                "default": 120,
                "minimum": 51,
                "description": "Closed higher-timeframe candles requested during warm-up.",
            },
            "swing_period": {
                "default": 20,
                "minimum": 2,
                "description": "Prior candles that define the swept swing high/low.",
            },
            "atr_period": {"default": 14, "minimum": 2},
            "volume_period": {
                "default": 30,
                "minimum": 2,
                "description": "Window for the volume z-score baseline.",
            },
            "volume_zscore_threshold": {
                "default": "1.5",
                "minimum": "0",
                "description": "Volume standard deviations required to confirm the climax.",
            },
            "max_volume_zscore": {
                "default": "10",
                "minimum": "0.01",
                "description": (
                    "Reject extreme volume shocks that are more likely continuation "
                    "or liquidation events than a stable reversal."
                ),
            },
            "min_rejection_wick_fraction": {
                "default": "0.35",
                "minimum": "0",
                "description": "Minimum rejection wick as a fraction of the candle range.",
            },
            "min_sweep_depth_atr": {
                "default": "0.15",
                "minimum": "0",
                "description": "Minimum sweep depth in ATRs (filters noise ticks).",
            },
            "max_sweep_depth_atr": {
                "default": "2",
                "minimum": "0.01",
                "description": "Maximum sweep depth in ATRs (deeper reads as a real breakout).",
            },
            "higher_timeframe_ema_period": {"default": 50, "minimum": 2},
            "stop_buffer_atr": {
                "default": "0.25",
                "minimum": "0",
                "description": "ATR buffer beyond the swept extreme for the stop.",
            },
            "reward_risk_ratio": {"default": "1.6", "minimum": "0.01"},
            "minimum_liquid_reward_risk": {"default": "1.2", "minimum": "0.01"},
            "estimated_round_trip_cost_bps": {"default": "15", "minimum": "0"},
            "entry_confirmation_candles": {
                "default": 0,
                "minimum": 0,
                "maximum": 1,
                "description": "Require the next closed candle to confirm the sweep.",
            },
            "require_confirmation_body_direction": {"default": True},
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
            return self._hold(state, normalized_symbol, market, "market snapshot is stale")

        candles_by_timeframe = market.candles
        if not candles_by_timeframe:
            return self._hold(state, normalized_symbol, market, "candle data is unavailable")

        primary_candles = candles_by_timeframe.get(self.primary_timeframe, ())
        higher_candles = candles_by_timeframe.get(self.higher_timeframe, ())

        if len(primary_candles) < self.primary_history:
            return self._hold(
                state,
                normalized_symbol,
                market,
                f"primary warm-up incomplete ({len(primary_candles)}/{self.primary_history})",
            )

        if len(higher_candles) < self.higher_timeframe_history:
            return self._hold(
                state,
                normalized_symbol,
                market,
                "higher-timeframe warm-up incomplete "
                f"({len(higher_candles)}/{self.higher_timeframe_history})",
            )

        if not bool(getattr(primary_candles[-1], "is_closed", False)):
            return self._hold(
                state,
                normalized_symbol,
                market,
                "waiting for the primary candle to close",
            )

        indicators = calculate_liquidity_sweep_indicators(
            primary_candles,
            swing_period=self.swing_period,
            atr_period=self.atr_period,
            volume_period=self.volume_period,
        )
        bias = _higher_timeframe_bias(
            higher_candles,
            ema_period=self.higher_timeframe_ema_period,
        )

        if indicators is None or bias is None:
            return self._hold(state, normalized_symbol, market, "indicators are not ready")

        long_conditions = self._long_conditions(indicators, bias)
        short_conditions = self._short_conditions(indicators, bias)

        long_ready = self.enable_long and all(long_conditions.values())
        short_ready = self.enable_short and all(short_conditions.values())
        structural_extreme: Decimal | None = None
        confirmation_metadata: dict[str, Any] = {
            "required": self.entry_confirmation_candles > 0,
            "status": "NOT_REQUIRED" if self.entry_confirmation_candles == 0 else "WAITING",
        }
        if self.entry_confirmation_candles > 0 and position.is_open:
            # A pending setup cannot survive a position lifecycle: it would
            # otherwise be "confirmed" by an unrelated candle much later.
            state.pop("liquidity_sweep_pending_confirmation", None)
        if self.entry_confirmation_candles > 0 and not position.is_open:
            candle_close_time_ms = int(getattr(primary_candles[-1], "close_time_ms", 0) or 0)
            previous_close_time_ms = (
                int(getattr(primary_candles[-2], "close_time_ms", 0) or 0)
                if len(primary_candles) >= 2
                else 0
            )
            setup_long_ready = long_ready
            setup_short_ready = short_ready
            # With confirmation enabled the setup candle itself never opens.
            long_ready = False
            short_ready = False
            pending = state.get("liquidity_sweep_pending_confirmation")
            if isinstance(pending, dict):
                created_close_time_ms = int(pending.get("created_close_time_ms", 0) or 0)
                side = str(pending.get("side", ""))
                if created_close_time_ms == candle_close_time_ms:
                    # Same setup candle evaluated again: keep waiting.
                    confirmation_metadata = {
                        "required": True,
                        "status": "PENDING",
                        "side": side,
                        "setup_close": str(pending.get("close", "")),
                        "structural_extreme": str(pending.get("structural_extreme", "")),
                    }
                else:
                    raw_close = _optional_decimal(pending.get("close"))
                    raw_extreme = _optional_decimal(pending.get("structural_extreme"))
                    state.pop("liquidity_sweep_pending_confirmation", None)
                    pending = None
                    # Only the candle immediately after the setup may confirm it.
                    is_next_candle = created_close_time_ms == previous_close_time_ms
                    confirmed_side = ""
                    if is_next_candle and raw_close is not None and raw_extreme is not None:
                        body_confirms_long = (
                            not self.require_confirmation_body_direction
                            or indicators.close > indicators.open
                        )
                        body_confirms_short = (
                            not self.require_confirmation_body_direction
                            or indicators.close < indicators.open
                        )
                        if (
                            side == "LONG"
                            and self.enable_long
                            and indicators.close > raw_close
                            and indicators.low > raw_extreme
                            and body_confirms_long
                        ):
                            confirmed_side = "LONG"
                        elif (
                            side == "SHORT"
                            and self.enable_short
                            and indicators.close < raw_close
                            and indicators.high < raw_extreme
                            and body_confirms_short
                        ):
                            confirmed_side = "SHORT"
                    if confirmed_side:
                        long_ready = confirmed_side == "LONG"
                        short_ready = confirmed_side == "SHORT"
                        structural_extreme = raw_extreme
                        confirmation_metadata = {
                            "required": True,
                            "status": "CONFIRMED",
                            "side": confirmed_side,
                            "setup_close": str(raw_close),
                            "structural_extreme": str(raw_extreme),
                        }
                    else:
                        confirmation_metadata = {
                            "required": True,
                            "status": "EXPIRED_OR_INVALIDATED",
                            "side": side,
                        }
            if (
                not isinstance(pending, dict)
                and not (long_ready or short_ready)
                and (setup_long_ready or setup_short_ready)
            ):
                pending_side = "LONG" if setup_long_ready else "SHORT"
                pending_extreme = indicators.low if setup_long_ready else indicators.high
                state["liquidity_sweep_pending_confirmation"] = {
                    "side": pending_side,
                    "close": str(indicators.close),
                    "structural_extreme": str(pending_extreme),
                    "created_close_time_ms": candle_close_time_ms,
                }
                confirmation_metadata = {
                    "required": True,
                    "status": "PENDING",
                    "side": pending_side,
                    "setup_close": str(indicators.close),
                    "structural_extreme": str(pending_extreme),
                }

        long_score = _condition_score(long_conditions)
        short_score = _condition_score(short_conditions)
        raw_score = max(long_score, short_score)

        action = _resolve_action(
            position=position,
            long_ready=long_ready,
            short_ready=short_ready,
        )

        setup_side = "LONG" if long_ready else ("SHORT" if short_ready else "")

        planned_exit: PlannedExit | None = None
        if action in {"OPEN_LONG", "OPEN_SHORT"}:
            entry_price = (
                to_decimal(market.price) if to_decimal(market.price) > _ZERO else indicators.close
            )
            planned_exit = _build_sweep_exit(
                action=action,
                entry_price=entry_price,
                indicators=indicators,
                stop_buffer_atr=self.stop_buffer_atr,
                reward_risk_ratio=self.reward_risk_ratio,
                estimated_round_trip_cost_bps=self.estimated_round_trip_cost_bps,
                structural_extreme=structural_extreme,
            )
            if (
                planned_exit is None
                or planned_exit.liquid_reward_risk < self.minimum_liquid_reward_risk
            ):
                action = "HOLD"

        score = (
            100 if action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"} else min(raw_score, 89)
        )

        reason = _build_reason(
            action=action,
            long_score=long_score,
            short_score=short_score,
            indicators=indicators,
            setup_side=setup_side,
            planned_exit=planned_exit,
            minimum_liquid_reward_risk=self.minimum_liquid_reward_risk,
        )

        metadata = _indicator_metadata(
            indicators=indicators,
            bias=bias,
            long_conditions=long_conditions,
            short_conditions=short_conditions,
            long_score=long_score,
            short_score=short_score,
        )
        metadata["entry_confirmation"] = confirmation_metadata

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
                    0.0 if action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"} else 900.0
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
        action = str(state.get("liquidity_sweep_action") or "HOLD")
        reason = str(state.get("liquidity_sweep_reason") or "Liquidity sweep: no evaluated signal")
        score = int(state.get("liquidity_sweep_score", 0) or 0)
        metadata = dict(state.get("liquidity_sweep_metadata") or {})

        return TradeIntent(
            symbol=symbol,
            action=action,
            confidence=score,
            score=score,
            reason=reason,
            strategy_name=self.__class__.__name__,
            market_price=market.price,
            suggested_next_check_seconds=(
                0.0 if action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"} else 900.0
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
        action = str(state.get("liquidity_sweep_action") or "HOLD")
        reason = str(state.get("liquidity_sweep_reason") or "Liquidity sweep: no evaluated signal")

        if action == "OPEN_LONG":
            return TradeDecision.open_long(reason)
        if action == "OPEN_SHORT":
            return TradeDecision.open_short(reason)
        if action == "CLOSE_POSITION":
            return TradeDecision.close(reason)
        return TradeDecision.hold(reason)

    def _long_conditions(
        self,
        indicators: LiquiditySweepIndicators,
        bias: HigherTimeframeBias,
    ) -> dict[str, bool]:
        depth = indicators.bullish_sweep_depth_atr
        return {
            "sweep": indicators.bullish_swept,
            "reclaim": indicators.bullish_reclaimed,
            "rejection_wick": (
                indicators.lower_wick_fraction is not None
                and indicators.lower_wick_fraction >= self.min_rejection_wick_fraction
            ),
            "volume_climax": indicators.volume.anomalous(self.volume_zscore_threshold),
            "volume_not_extreme": (
                indicators.volume.zscore is not None
                and indicators.volume.zscore <= self.max_volume_zscore
            ),
            "sweep_depth": (
                depth is not None and self.min_sweep_depth_atr <= depth <= self.max_sweep_depth_atr
            ),
            "higher_timeframe": not bias.decisively_bearish,
        }

    def _short_conditions(
        self,
        indicators: LiquiditySweepIndicators,
        bias: HigherTimeframeBias,
    ) -> dict[str, bool]:
        depth = indicators.bearish_sweep_depth_atr
        return {
            "sweep": indicators.bearish_swept,
            "reclaim": indicators.bearish_reclaimed,
            "rejection_wick": (
                indicators.upper_wick_fraction is not None
                and indicators.upper_wick_fraction >= self.min_rejection_wick_fraction
            ),
            "volume_climax": indicators.volume.anomalous(self.volume_zscore_threshold),
            "volume_not_extreme": (
                indicators.volume.zscore is not None
                and indicators.volume.zscore <= self.max_volume_zscore
            ),
            "sweep_depth": (
                depth is not None and self.min_sweep_depth_atr <= depth <= self.max_sweep_depth_atr
            ),
            "higher_timeframe": not bias.decisively_bullish,
        }

    def _hold(
        self,
        state: dict[str, Any],
        symbol: str,
        market: MarketSnapshot,
        detail: str,
    ) -> SignalScore:
        return self._remember(
            state,
            SignalScore(
                symbol=symbol,
                score=0,
                reason=f"Liquidity sweep: {detail}",
                action="HOLD",
                confidence=0,
                strategy=self.__class__.__name__,
                data_age_seconds=market.data_age_seconds,
            ),
        )

    @staticmethod
    def _remember(
        state: dict[str, Any],
        score: SignalScore,
    ) -> SignalScore:
        state["liquidity_sweep_action"] = score.action or "HOLD"
        state["liquidity_sweep_reason"] = score.reason
        state["liquidity_sweep_score"] = int(score.score)
        state["liquidity_sweep_metadata"] = dict(score.metadata)
        return score


def _higher_timeframe_bias(
    candles: Any,
    *,
    ema_period: int,
) -> HigherTimeframeBias | None:
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

    return HigherTimeframeBias(
        close=current_close,
        ema=current_ema,
        previous_ema=previous_ema,
        decisively_bullish=current_close > current_ema and current_ema > previous_ema,
        decisively_bearish=current_close < current_ema and current_ema < previous_ema,
    )


def _condition_score(conditions: dict[str, bool]) -> int:
    weights = {
        "sweep": 20,
        "reclaim": 15,
        "rejection_wick": 15,
        "volume_climax": 15,
        "volume_not_extreme": 10,
        "sweep_depth": 15,
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


def _build_sweep_exit(
    *,
    action: str,
    entry_price: Decimal,
    indicators: LiquiditySweepIndicators,
    stop_buffer_atr: Decimal,
    reward_risk_ratio: Decimal,
    estimated_round_trip_cost_bps: Decimal,
    structural_extreme: Decimal | None = None,
) -> PlannedExit | None:
    """Structure-based exit: the stop sits beyond the swept extreme.

    The invalidation level of a failed breakout is the sweep wick itself — if
    price trades through it again, the reversal thesis is wrong. This keeps
    the risk tied to market structure instead of a generic ATR multiple.
    """
    if entry_price <= _ZERO or indicators.atr <= _ZERO:
        return None

    buffer_abs = indicators.atr * stop_buffer_atr

    if action == "OPEN_LONG":
        stop_anchor = structural_extreme if structural_extreme is not None else indicators.low
        stop_price = stop_anchor - buffer_abs
        risk_abs = entry_price - stop_price
    elif action == "OPEN_SHORT":
        stop_anchor = structural_extreme if structural_extreme is not None else indicators.high
        stop_price = stop_anchor + buffer_abs
        risk_abs = stop_price - entry_price
    else:
        return None

    if stop_price <= _ZERO or risk_abs <= _ZERO:
        return None

    reward_abs = risk_abs * reward_risk_ratio
    if reward_abs <= _ZERO:
        return None

    if action == "OPEN_LONG":
        take_profit_price = entry_price + reward_abs
    else:
        take_profit_price = entry_price - reward_abs

    if take_profit_price <= _ZERO:
        return None

    estimated_cost_abs = entry_price * estimated_round_trip_cost_bps / _BASIS_POINTS
    net_reward_abs = reward_abs - estimated_cost_abs
    net_risk_abs = risk_abs + estimated_cost_abs

    if net_reward_abs <= _ZERO or net_risk_abs <= _ZERO:
        return None

    return PlannedExit(
        entry_price=entry_price,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        risk_abs=risk_abs,
        reward_abs=reward_abs,
        estimated_cost_abs=estimated_cost_abs,
        net_reward_abs=net_reward_abs,
        net_risk_abs=net_risk_abs,
        liquid_reward_risk=net_reward_abs / net_risk_abs,
        raw_reward_risk=reward_abs / risk_abs,
        cost_to_reward_ratio=estimated_cost_abs / reward_abs,
        risk_pct=risk_abs / entry_price,
        reward_pct=reward_abs / entry_price,
    )


def _build_reason(
    *,
    action: str,
    long_score: int,
    short_score: int,
    indicators: LiquiditySweepIndicators,
    setup_side: str,
    planned_exit: PlannedExit | None,
    minimum_liquid_reward_risk: Decimal,
) -> str:
    if action == "OPEN_LONG":
        return (
            "Liquidity sweep LONG confirmed: prior swing low swept and "
            "reclaimed with rejection wick and volume climax"
        )

    if action == "OPEN_SHORT":
        return (
            "Liquidity sweep SHORT confirmed: prior swing high swept and "
            "reclaimed with rejection wick and volume climax"
        )

    if action == "CLOSE_POSITION":
        return (
            f"Liquidity sweep opposite {setup_side} setup confirmed; "
            "closing the current position before any reversal"
        )

    if (
        setup_side
        and planned_exit is not None
        and planned_exit.liquid_reward_risk < minimum_liquid_reward_risk
    ):
        return (
            "Liquidity sweep setup rejected: estimated liquid reward/risk "
            f"{planned_exit.liquid_reward_risk:.4f} is below "
            f"{minimum_liquid_reward_risk:.4f}"
        )

    zscore = indicators.volume.zscore
    return (
        "Liquidity sweep waiting: "
        f"long_score={long_score}, short_score={short_score}, "
        f"swing_low={indicators.swing_low}, swing_high={indicators.swing_high}, "
        f"volume_zscore={f'{zscore:.4f}' if zscore is not None else 'n/a'}"
    )


def _indicator_metadata(
    *,
    indicators: LiquiditySweepIndicators,
    bias: HigherTimeframeBias,
    long_conditions: dict[str, bool],
    short_conditions: dict[str, bool],
    long_score: int,
    short_score: int,
) -> dict[str, Any]:
    return {
        "source": "FIXED_LIQUIDITY_SWEEP_REVERSAL",
        "open": str(indicators.open),
        "high": str(indicators.high),
        "low": str(indicators.low),
        "close": str(indicators.close),
        "atr": str(indicators.atr),
        "swing_low": str(indicators.swing_low),
        "swing_high": str(indicators.swing_high),
        "lower_wick_fraction": (
            str(indicators.lower_wick_fraction)
            if indicators.lower_wick_fraction is not None
            else None
        ),
        "upper_wick_fraction": (
            str(indicators.upper_wick_fraction)
            if indicators.upper_wick_fraction is not None
            else None
        ),
        "bullish_sweep_depth_atr": (
            str(indicators.bullish_sweep_depth_atr)
            if indicators.bullish_sweep_depth_atr is not None
            else None
        ),
        "bearish_sweep_depth_atr": (
            str(indicators.bearish_sweep_depth_atr)
            if indicators.bearish_sweep_depth_atr is not None
            else None
        ),
        "volume_zscore": (
            str(indicators.volume.zscore) if indicators.volume.zscore is not None else None
        ),
        "volume_current": str(indicators.volume.current),
        "volume_mean": str(indicators.volume.mean),
        "higher_timeframe_close": str(bias.close),
        "higher_timeframe_ema": str(bias.ema),
        "higher_timeframe_decisively_bullish": bias.decisively_bullish,
        "higher_timeframe_decisively_bearish": bias.decisively_bearish,
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


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


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


__all__ = [
    "HigherTimeframeBias",
    "LiquiditySweepReversalStrategy",
]
