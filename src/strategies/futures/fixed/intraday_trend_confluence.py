"""Day-trade strategy: four independent with-trend entry setups on 1h candles.

Goal: about one good trade per day per symbol. A single setup is either too rare or,
when loosened to fire daily, too weak. This strategy keeps each setup strict and
gets frequency by combining four *different* reasons to enter, all in the direction
of the prevailing 1h trend. They fire in different market phases, so together they
trade often without lowering the bar of any one of them:

1. PULLBACK — trend continuation. Within the last 6 candles price reached the EMA21
   "value zone" (EMA21 +/- 0.25 ATR) while EMA21 > EMA55, then a confirmation candle
   closes above the previous high (Raschke's "Holy Grail" family; buying weakness
   inside strength).
2. VWAP_RECLAIM — price closed below the rolling 24h VWAP (the average price paid by
   volume, the level intraday participants defend) in the last 5 candles and a
   confirmation candle closes back above it. Mean reversion to value inside a trend.
3. SQUEEZE_BREAKOUT — Bollinger band width falls to the lowest 30% of its last 100
   candles (volatility compression; volatility clusters), then a close outside the
   band and above the 20-candle high, on >= 1.2x average volume (expansion).
4. LIQUIDITY_SWEEP — a candle pierces the 12-candle low, where stop orders rest, and
   closes back above it with a long lower wick (a "spring"): forced sellers absorbed,
   trend resumes. Only taken in the trend direction.

Trend bias (all setups): close above EMA200 and EMA50 above EMA200 (mirror for
shorts). EMA200 on 1h is ~8 days, the intermediate trend that intraday flows tend to
follow (time-series momentum). Requiring EMA50 to also be rising is optional and off:
the EMA50 flattens exactly during the pullbacks the strategy wants to buy.

Entry quality filters (every setup): confirmation candle (body >= 30% of the range,
close in the top 45%), no climax candle (range <= 3 ATR), at least 0.6x average
volume (no dead candles), a structural stop between 1 and 3 ATR, and round-trip cost
<= 25% of risk.

Exits: take-profit at 2.5R (resting, maker fee); after +1R a 2.5 ATR chandelier trail
closes on a candle close beyond it and after +1.2R the trail never sits below
break-even + cost; a trade that is not +0.3R after 24 candles is closed; nothing is
held beyond 72 candles; a close beyond EMA200 against the position ends it.

Frequency control: at most 3 entries per UTC day and 2 candles of cooldown after an
exit. Only the 1h timeframe is needed (no secondary CSV); 600 candles (~25 days) of
warm-up converge the EMA200. Parameters are textbook defaults, not fitted to data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeDecision
from domain.strategy_requirements import (
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    StrategyDataRequirements,
)
from domain.timeframe import timeframe_duration_ms
from indicators.atr import average_true_range
from indicators.bollinger import bollinger_bands, bollinger_bandwidth_series, percentile_rank
from indicators.common import to_decimal
from indicators.moving_average import exponential_moving_average_series
from indicators.relative_volume import relative_volume
from indicators.vwap import rolling_vwap_series
from scanner.signal_score import SignalScore
from strategies.futures.base import FuturesStrategyV2, StrategyMetadata
from strategies.futures.fixed.donchian_adx_breakout import PlannedExit
from trading.intent import TradeIntent

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BASIS_POINTS = Decimal("10000")
_DAY_MS = 86_400_000

_LONG = "LONG"
_SHORT = "SHORT"
_OPEN_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}
_ACTIVE_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"}

SETUP_PULLBACK = "PULLBACK"
SETUP_VWAP_RECLAIM = "VWAP_RECLAIM"
SETUP_SQUEEZE_BREAKOUT = "SQUEEZE_BREAKOUT"
SETUP_LIQUIDITY_SWEEP = "LIQUIDITY_SWEEP"
# Order of preference when several setups fire on the same candle.
_SETUP_PRIORITY = (
    SETUP_PULLBACK,
    SETUP_LIQUIDITY_SWEEP,
    SETUP_VWAP_RECLAIM,
    SETUP_SQUEEZE_BREAKOUT,
)

# State keys. Everything stored in ``state`` must stay JSON-serialisable because the
# live Governor persists the per-symbol strategy state.
_KEY_ACTION = "intraday_confluence_action"
_KEY_REASON = "intraday_confluence_reason"
_KEY_SCORE = "intraday_confluence_score"
_KEY_METADATA = "intraday_confluence_metadata"
_KEY_TRADE = "intraday_confluence_trade"
_KEY_PENDING = "intraday_confluence_pending_entry"
_KEY_LAST_EXIT = "intraday_confluence_last_exit_close_time_ms"
_KEY_LAST_ENTRY_SIGNAL = "intraday_confluence_last_entry_signal_close_time_ms"
_KEY_DAY_ENTRIES = "intraday_confluence_day_entries"

_PENDING_ENTRY_MAX_CANDLES = 3


@dataclass(frozen=True)
class MarketReading:
    """Every value the setups need, computed once per closed candle."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    close_time_ms: int
    atr: Decimal
    relative_volume: Decimal | None
    # Trend bias.
    trend_fast_ema: Decimal
    trend_fast_ema_past: Decimal
    trend_slow_ema: Decimal
    require_trend_slope: bool
    # Pullback.
    value_ema: Decimal
    structure_ema: Decimal
    previous_high: Decimal
    previous_low: Decimal
    touched_value_below: bool
    touched_value_above: bool
    pullback_low: Decimal
    pullback_high: Decimal
    # VWAP reclaim.
    vwap: Decimal | None
    closed_below_vwap: bool
    closed_above_vwap: bool
    vwap_swing_low: Decimal
    vwap_swing_high: Decimal
    # Squeeze breakout.
    squeeze_rank: Decimal | None
    band_upper: Decimal
    band_lower: Decimal
    breakout_high: Decimal
    breakout_low: Decimal
    base_low: Decimal
    base_high: Decimal
    # Liquidity sweep.
    sweep_level_low: Decimal
    sweep_level_high: Decimal

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def range_atr(self) -> Decimal:
        return self.range / self.atr

    @property
    def body_fraction(self) -> Decimal:
        return abs(self.close - self.open) / self.range

    @property
    def lower_wick_fraction(self) -> Decimal:
        return (min(self.open, self.close) - self.low) / self.range

    @property
    def upper_wick_fraction(self) -> Decimal:
        return (self.high - max(self.open, self.close)) / self.range

    def close_location(self, side: str) -> Decimal:
        """Where the close sits in the candle, 1 = at the extreme favouring ``side``."""

        if side == _LONG:
            return (self.close - self.low) / self.range
        return (self.high - self.close) / self.range

    @property
    def bullish_bias(self) -> bool:
        slope_ok = not self.require_trend_slope or self.trend_fast_ema > self.trend_fast_ema_past
        return (
            self.close > self.trend_slow_ema
            and self.trend_fast_ema > self.trend_slow_ema
            and slope_ok
        )

    @property
    def bearish_bias(self) -> bool:
        slope_ok = not self.require_trend_slope or self.trend_fast_ema < self.trend_fast_ema_past
        return (
            self.close < self.trend_slow_ema
            and self.trend_fast_ema < self.trend_slow_ema
            and slope_ok
        )


@dataclass(frozen=True)
class SetupCandidate:
    name: str
    side: str
    conditions: dict[str, bool]
    stop_anchor: Decimal
    risk_abs: Decimal = _ZERO
    planned_exit: PlannedExit | None = None
    filters: dict[str, bool] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return (
            all(self.conditions.values())
            and all(self.filters.values())
            and self.planned_exit is not None
        )

    @property
    def completion(self) -> Decimal:
        checks = list(self.conditions.values()) + list(self.filters.values())
        if not checks:
            return _ZERO
        return Decimal(sum(1 for passed in checks if passed)) / Decimal(len(checks))

    def as_metadata(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "conditions": dict(self.conditions),
            "filters": dict(self.filters),
            "stop_anchor": str(self.stop_anchor),
            "risk_atr_abs": str(self.risk_abs),
            "ready": self.ready,
        }


@dataclass(frozen=True)
class TradeManagement:
    side: str
    setup: str
    entry_price: Decimal
    initial_risk: Decimal
    best_price: Decimal
    bars_held: int
    max_favorable_r: Decimal
    current_r: Decimal
    trail_price: Decimal | None
    exit_reason: str

    def as_metadata(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "setup": self.setup,
            "entry_price": str(self.entry_price),
            "initial_risk": str(self.initial_risk),
            "best_price": str(self.best_price),
            "bars_held": self.bars_held,
            "max_favorable_r": str(self.max_favorable_r),
            "current_r": str(self.current_r),
            "trail_price": str(self.trail_price) if self.trail_price is not None else None,
            "exit_reason": self.exit_reason or None,
        }


class IntradayTrendConfluenceStrategy(FuturesStrategyV2):
    """Day trade on 1h: four with-trend setups (pullback, VWAP reclaim, squeeze
    breakout, liquidity sweep) under one EMA200 trend bias, ~1 entry per day.

    Each setup is strict on its own; frequency comes from combining independent
    reasons to enter. Structural stops between 1 and 2.5 ATR, costs capped at 25% of
    the risk, 2.5R take-profit, chandelier trail after +1R, break-even after +1.2R,
    a 24-candle time stop and at most 3 entries per UTC day.
    """

    strategy_category = "FIXED_DAY_TRADE"

    metadata = StrategyMetadata(
        strategy_name="IntradayTrendConfluenceStrategy",
        supports_scoring=True,
        supports_long=True,
        supports_short=True,
        required_history=600,
        required_timeframes=["1h"],
        required_indicators=[
            "EMA",
            "ATR",
            "VWAP",
            "BOLLINGER",
            "RELATIVE_VOLUME",
            "LIQUIDITY_SWEEP",
        ],
    )

    def __init__(
        self,
        *,
        timeframe: str = "1h",
        history: int = 600,
        trend_fast_ema_period: int = 50,
        trend_slow_ema_period: int = 200,
        require_trend_slope: bool = False,
        trend_slope_bars: int = 6,
        value_ema_period: int = 21,
        structure_ema_period: int = 55,
        atr_period: int = 14,
        volume_period: int = 20,
        min_relative_volume: Decimal | int | float | str = Decimal("0.6"),
        breakout_min_relative_volume: Decimal | int | float | str = Decimal("1.2"),
        min_body_fraction: Decimal | int | float | str = Decimal("0.3"),
        min_close_location: Decimal | int | float | str = Decimal("0.55"),
        max_signal_range_atr: Decimal | int | float | str = Decimal("3"),
        pullback_lookback: int = 6,
        value_zone_atr: Decimal | int | float | str = Decimal("0.25"),
        vwap_window: int = 24,
        vwap_lookback: int = 5,
        bollinger_period: int = 20,
        bollinger_std: Decimal | int | float | str = Decimal("2"),
        squeeze_history: int = 100,
        squeeze_lookback: int = 6,
        squeeze_percentile: Decimal | int | float | str = Decimal("0.3"),
        breakout_lookback: int = 20,
        breakout_base_bars: int = 3,
        sweep_lookback: int = 12,
        sweep_min_wick_fraction: Decimal | int | float | str = Decimal("0.4"),
        sweep_min_depth_atr: Decimal | int | float | str = Decimal("0.05"),
        sweep_max_depth_atr: Decimal | int | float | str = Decimal("1.5"),
        sweep_min_close_location: Decimal | int | float | str = Decimal("0.5"),
        stop_buffer_atr: Decimal | int | float | str = Decimal("0.2"),
        min_stop_atr: Decimal | int | float | str = Decimal("1.0"),
        max_stop_atr: Decimal | int | float | str = Decimal("3"),
        take_profit_r: Decimal | int | float | str = Decimal("2.5"),
        minimum_liquid_reward_risk: Decimal | int | float | str = Decimal("1.5"),
        estimated_round_trip_cost_bps: Decimal | int | float | str = Decimal("15"),
        max_cost_to_risk: Decimal | int | float | str = Decimal("0.25"),
        trail_activation_r: Decimal | int | float | str = Decimal("1.0"),
        trail_atr_multiplier: Decimal | int | float | str = Decimal("2.5"),
        breakeven_after_r: Decimal | int | float | str = Decimal("1.2"),
        max_bars_without_progress: int = 24,
        progress_r: Decimal | int | float | str = Decimal("0.3"),
        max_holding_bars: int = 72,
        exit_on_trend_break: bool = True,
        cooldown_bars: int = 2,
        max_entries_per_day: int = 3,
        enable_pullback: bool = True,
        enable_vwap_reclaim: bool = True,
        enable_squeeze_breakout: bool = True,
        enable_liquidity_sweep: bool = True,
        enable_long: bool = True,
        enable_short: bool = True,
    ) -> None:
        self.timeframe = _non_empty_text(timeframe, "timeframe")
        try:
            self._duration_ms = timeframe_duration_ms(self.timeframe)
        except (KeyError, ValueError) as error:
            raise ValueError(f"unsupported timeframe: {error}") from error

        self.trend_fast_ema_period = _positive_int(trend_fast_ema_period, "trend_fast_ema_period")
        self.trend_slow_ema_period = _positive_int(trend_slow_ema_period, "trend_slow_ema_period")
        if self.trend_fast_ema_period >= self.trend_slow_ema_period:
            raise ValueError("trend_fast_ema_period must be below trend_slow_ema_period")
        self.require_trend_slope = bool(require_trend_slope)
        self.trend_slope_bars = _positive_int(trend_slope_bars, "trend_slope_bars")
        self.value_ema_period = _positive_int(value_ema_period, "value_ema_period")
        self.structure_ema_period = _positive_int(structure_ema_period, "structure_ema_period")
        if self.value_ema_period >= self.structure_ema_period:
            raise ValueError("value_ema_period must be below structure_ema_period")
        self.atr_period = _positive_int(atr_period, "atr_period")
        self.volume_period = _positive_int(volume_period, "volume_period")
        self.min_relative_volume = _non_negative_decimal(min_relative_volume, "min_relative_volume")
        self.breakout_min_relative_volume = _non_negative_decimal(
            breakout_min_relative_volume, "breakout_min_relative_volume"
        )
        self.min_body_fraction = _fraction(min_body_fraction, "min_body_fraction")
        self.min_close_location = _fraction(min_close_location, "min_close_location")
        self.max_signal_range_atr = _positive_decimal(max_signal_range_atr, "max_signal_range_atr")

        self.pullback_lookback = _positive_int(pullback_lookback, "pullback_lookback")
        self.value_zone_atr = _non_negative_decimal(value_zone_atr, "value_zone_atr")
        self.vwap_window = _positive_int(vwap_window, "vwap_window")
        self.vwap_lookback = _positive_int(vwap_lookback, "vwap_lookback")
        self.bollinger_period = _positive_int(bollinger_period, "bollinger_period")
        if self.bollinger_period < 2:
            raise ValueError("bollinger_period must be at least 2")
        self.bollinger_std = _positive_decimal(bollinger_std, "bollinger_std")
        self.squeeze_history = _positive_int(squeeze_history, "squeeze_history")
        self.squeeze_lookback = _positive_int(squeeze_lookback, "squeeze_lookback")
        self.squeeze_percentile = _fraction(squeeze_percentile, "squeeze_percentile")
        self.breakout_lookback = _positive_int(breakout_lookback, "breakout_lookback")
        self.breakout_base_bars = _positive_int(breakout_base_bars, "breakout_base_bars")
        self.sweep_lookback = _positive_int(sweep_lookback, "sweep_lookback")
        if self.sweep_lookback < 2:
            raise ValueError("sweep_lookback must be at least 2")
        self.sweep_min_wick_fraction = _fraction(sweep_min_wick_fraction, "sweep_min_wick_fraction")
        self.sweep_min_depth_atr = _non_negative_decimal(sweep_min_depth_atr, "sweep_min_depth_atr")
        self.sweep_max_depth_atr = _positive_decimal(sweep_max_depth_atr, "sweep_max_depth_atr")
        if self.sweep_max_depth_atr <= self.sweep_min_depth_atr:
            raise ValueError("sweep_max_depth_atr must be above sweep_min_depth_atr")
        self.sweep_min_close_location = _fraction(
            sweep_min_close_location, "sweep_min_close_location"
        )

        self.stop_buffer_atr = _non_negative_decimal(stop_buffer_atr, "stop_buffer_atr")
        self.min_stop_atr = _positive_decimal(min_stop_atr, "min_stop_atr")
        self.max_stop_atr = _positive_decimal(max_stop_atr, "max_stop_atr")
        if self.max_stop_atr < self.min_stop_atr:
            raise ValueError("max_stop_atr must be at least min_stop_atr")
        self.take_profit_r = _positive_decimal(take_profit_r, "take_profit_r")
        self.minimum_liquid_reward_risk = _positive_decimal(
            minimum_liquid_reward_risk, "minimum_liquid_reward_risk"
        )
        self.estimated_round_trip_cost_bps = _non_negative_decimal(
            estimated_round_trip_cost_bps, "estimated_round_trip_cost_bps"
        )
        self.max_cost_to_risk = _positive_decimal(max_cost_to_risk, "max_cost_to_risk")

        self.trail_activation_r = _non_negative_decimal(trail_activation_r, "trail_activation_r")
        self.trail_atr_multiplier = _positive_decimal(trail_atr_multiplier, "trail_atr_multiplier")
        self.breakeven_after_r = _non_negative_decimal(breakeven_after_r, "breakeven_after_r")
        self.max_bars_without_progress = _non_negative_int(
            max_bars_without_progress, "max_bars_without_progress"
        )
        self.progress_r = _decimal(progress_r, "progress_r")
        self.max_holding_bars = _non_negative_int(max_holding_bars, "max_holding_bars")
        self.exit_on_trend_break = bool(exit_on_trend_break)
        self.cooldown_bars = _non_negative_int(cooldown_bars, "cooldown_bars")
        self.max_entries_per_day = _non_negative_int(max_entries_per_day, "max_entries_per_day")

        self.enabled_setups = {
            SETUP_PULLBACK: bool(enable_pullback),
            SETUP_VWAP_RECLAIM: bool(enable_vwap_reclaim),
            SETUP_SQUEEZE_BREAKOUT: bool(enable_squeeze_breakout),
            SETUP_LIQUIDITY_SWEEP: bool(enable_liquidity_sweep),
        }
        if not any(self.enabled_setups.values()):
            raise ValueError("at least one setup must be enabled")
        self.enable_long = bool(enable_long)
        self.enable_short = bool(enable_short)
        if not self.enable_long and not self.enable_short:
            raise ValueError("at least one trade direction must be enabled")

        minimum_history = max(
            self.trend_slow_ema_period + self.trend_slope_bars + 1,
            self.structure_ema_period + self.pullback_lookback + 1,
            self.squeeze_history + self.bollinger_period + 1,
            self.vwap_window + self.vwap_lookback + 1,
            self.breakout_lookback + 1,
            self.sweep_lookback + 1,
            self.atr_period + 1,
            self.volume_period + 1,
        )
        self.history = _positive_int(history, "history")
        if self.history < minimum_history:
            raise ValueError(
                "history is too small for the configured indicators: "
                f"required>={minimum_history}, received={self.history}"
            )

    # ------------------------------------------------------------------
    # Construction / contract
    # ------------------------------------------------------------------

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> IntradayTrendConfluenceStrategy:
        values: dict[str, Any] = {}
        for name, spec in cls.describe_parameters().items():
            raw = params.get(name, spec["default"])
            default = spec["default"]
            if isinstance(default, bool):
                values[name] = _bool_param(raw)
            elif isinstance(default, int):
                values[name] = int(raw)
            else:
                values[name] = raw
        return cls(**values)

    @classmethod
    def describe_parameters(cls) -> dict[str, Any]:
        return {
            "timeframe": {"default": "1h", "description": "Candle interval of every setup."},
            "history": {
                "default": 600,
                "minimum": 207,
                "description": "Closed candles used per evaluation (~3x the EMA200 warm-up).",
            },
            "trend_fast_ema_period": {"default": 50, "minimum": 2},
            "trend_slow_ema_period": {
                "default": 200,
                "minimum": 3,
                "description": "Trend bias EMA: trades only on its side, in its direction.",
            },
            "require_trend_slope": {
                "default": False,
                "description": (
                    "Also require the fast trend EMA to be rising/falling. Off by default: "
                    "it flattens exactly during the pullbacks this strategy buys."
                ),
            },
            "trend_slope_bars": {
                "default": 6,
                "minimum": 1,
                "description": "Candles of the optional fast trend EMA slope check.",
            },
            "value_ema_period": {"default": 21, "minimum": 2},
            "structure_ema_period": {"default": 55, "minimum": 3},
            "atr_period": {"default": 14, "minimum": 2},
            "volume_period": {"default": 20, "minimum": 2},
            "min_relative_volume": {
                "default": "0.6",
                "minimum": "0",
                "description": "Signal volume / average volume for every setup (0 disables).",
            },
            "breakout_min_relative_volume": {
                "default": "1.2",
                "minimum": "0",
                "description": "Higher volume bar required by the squeeze breakout.",
            },
            "min_body_fraction": {
                "default": "0.3",
                "minimum": "0",
                "description": "Confirmation candle body as a fraction of its range.",
            },
            "min_close_location": {
                "default": "0.55",
                "minimum": "0",
                "description": "Confirmation close position in the range (1 = at the extreme).",
            },
            "max_signal_range_atr": {
                "default": "3",
                "minimum": "0.01",
                "description": "Reject climax signal candles wider than this many ATRs.",
            },
            "pullback_lookback": {"default": 6, "minimum": 1},
            "value_zone_atr": {
                "default": "0.25",
                "minimum": "0",
                "description": "Width of the EMA21 value zone a pullback must reach, in ATRs.",
            },
            "vwap_window": {
                "default": 24,
                "minimum": 1,
                "description": "Candles of the rolling VWAP (24 x 1h = one day).",
            },
            "vwap_lookback": {
                "default": 5,
                "minimum": 1,
                "description": "Recent candles in which price must have closed beyond VWAP.",
            },
            "bollinger_period": {"default": 20, "minimum": 2},
            "bollinger_std": {"default": "2", "minimum": "0.01"},
            "squeeze_history": {
                "default": 100,
                "minimum": 1,
                "description": "Candles of band width history used to rank the squeeze.",
            },
            "squeeze_lookback": {
                "default": 6,
                "minimum": 1,
                "description": "Recent candles in which the squeeze must have occurred.",
            },
            "squeeze_percentile": {
                "default": "0.3",
                "minimum": "0",
                "description": "Band width percentile that counts as compression.",
            },
            "breakout_lookback": {"default": 20, "minimum": 1},
            "breakout_base_bars": {
                "default": 3,
                "minimum": 1,
                "description": "Candles whose extreme anchors the breakout stop.",
            },
            "sweep_lookback": {
                "default": 12,
                "minimum": 2,
                "description": "Candles whose low/high the sweep must pierce.",
            },
            "sweep_min_wick_fraction": {"default": "0.4", "minimum": "0"},
            "sweep_min_depth_atr": {"default": "0.05", "minimum": "0"},
            "sweep_max_depth_atr": {"default": "1.5", "minimum": "0.01"},
            "sweep_min_close_location": {"default": "0.5", "minimum": "0"},
            "stop_buffer_atr": {"default": "0.2", "minimum": "0"},
            "min_stop_atr": {
                "default": "1.0",
                "minimum": "0.01",
                "description": "Structural stops closer than this many ATRs are widened.",
            },
            "max_stop_atr": {
                "default": "3",
                "minimum": "0.01",
                "description": "Setups whose structural stop is farther than this are skipped.",
            },
            "take_profit_r": {"default": "2.5", "minimum": "0.01"},
            "minimum_liquid_reward_risk": {"default": "1.5", "minimum": "0.01"},
            "estimated_round_trip_cost_bps": {"default": "15", "minimum": "0"},
            "max_cost_to_risk": {
                "default": "0.25",
                "minimum": "0.01",
                "description": "Skip setups where round-trip cost exceeds this share of risk.",
            },
            "trail_activation_r": {"default": "1.0", "minimum": "0"},
            "trail_atr_multiplier": {"default": "2.5", "minimum": "0.01"},
            "breakeven_after_r": {"default": "1.2", "minimum": "0"},
            "max_bars_without_progress": {
                "default": 24,
                "minimum": 0,
                "description": "Time stop in candles for trades below progress_r. 0 disables.",
            },
            "progress_r": {"default": "0.3"},
            "max_holding_bars": {
                "default": 72,
                "minimum": 0,
                "description": "Hard limit of candles in a position. 0 disables.",
            },
            "exit_on_trend_break": {
                "default": True,
                "description": "Close when price closes beyond the trend EMA against the trade.",
            },
            "cooldown_bars": {"default": 2, "minimum": 0},
            "max_entries_per_day": {
                "default": 3,
                "minimum": 0,
                "description": "Entry signals allowed per UTC day. 0 means unlimited.",
            },
            "enable_pullback": {"default": True},
            "enable_vwap_reclaim": {"default": True},
            "enable_squeeze_breakout": {"default": True},
            "enable_liquidity_sweep": {"default": True},
            "enable_long": {"default": True},
            "enable_short": {"default": True},
        }

    def data_requirements(self) -> StrategyDataRequirements:
        return StrategyDataRequirements(
            primary_timeframe=self.timeframe,
            required_candles={self.timeframe: self.history},
            evaluation_policy=EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
            closed_candles_only=True,
        )

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> SignalScore:
        normalized_symbol = symbol.upper().strip()

        if market.stale:
            return self._hold(state, normalized_symbol, market, "market snapshot is stale")
        if not market.candles:
            return self._hold(state, normalized_symbol, market, "candle data is unavailable")

        candles = market.candles.get(self.timeframe, ())
        if len(candles) < self.history:
            return self._hold(
                state,
                normalized_symbol,
                market,
                f"warm-up incomplete ({len(candles)}/{self.history})",
            )
        if not bool(getattr(candles[-1], "is_closed", False)):
            return self._hold(state, normalized_symbol, market, "waiting for the candle to close")

        reading = self._reading(candles)
        if reading is None:
            return self._hold(state, normalized_symbol, market, "indicators are not ready")

        self._expire_pending_entry(state, position, reading.close_time_ms)

        if position.is_open:
            return self._manage_position(state, normalized_symbol, market, position, reading)

        self._register_exit_if_any(state, reading.close_time_ms)
        return self._evaluate_entry(state, normalized_symbol, market, reading)

    def intent_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> TradeIntent:
        action = str(state.get(_KEY_ACTION) or "HOLD")
        reason = str(state.get(_KEY_REASON) or "Intraday confluence: no evaluated signal")
        score = int(state.get(_KEY_SCORE, 0) or 0)
        metadata = dict(state.get(_KEY_METADATA) or {})
        return TradeIntent(
            symbol=symbol,
            action=action,
            confidence=score,
            score=score,
            reason=reason,
            strategy_name=self.__class__.__name__,
            market_price=market.price,
            suggested_next_check_seconds=self._next_check_seconds(action),
            metadata=metadata,
        )

    def decide_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> TradeDecision:
        action = str(state.get(_KEY_ACTION) or "HOLD")
        reason = str(state.get(_KEY_REASON) or "Intraday confluence: no evaluated signal")
        if action == "OPEN_LONG":
            return TradeDecision.open_long(reason)
        if action == "OPEN_SHORT":
            return TradeDecision.open_short(reason)
        if action == "CLOSE_POSITION":
            return TradeDecision.close(reason)
        return TradeDecision.hold(reason)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def _evaluate_entry(
        self,
        state: dict[str, Any],
        symbol: str,
        market: MarketSnapshot,
        reading: MarketReading,
    ) -> SignalScore:
        entry_price = _positive_or(to_decimal(market.price), reading.close)

        side = ""
        if reading.bullish_bias and self.enable_long:
            side = _LONG
        elif reading.bearish_bias and self.enable_short:
            side = _SHORT

        candidates = self._candidates(side, reading, entry_price) if side else []
        ready = [candidate for candidate in candidates if candidate.ready]
        chosen = _first_by_priority(ready)

        cooling_down = self._cooling_down(state, reading.close_time_ms)
        day_limit_reached = self._day_limit_reached(state, reading.close_time_ms)
        already_signalled = (
            reading.close_time_ms > 0
            and int(state.get(_KEY_LAST_ENTRY_SIGNAL, 0) or 0) == reading.close_time_ms
        )
        blocked = cooling_down or day_limit_reached or already_signalled

        metadata = self._base_metadata(reading)
        metadata.update(
            {
                "phase": "ENTRY",
                "bias": side or "NONE",
                "setups": {candidate.name: candidate.as_metadata() for candidate in candidates},
                "ready_setups": [candidate.name for candidate in ready],
                "cooling_down": cooling_down,
                "day_limit_reached": day_limit_reached,
            }
        )

        action = "HOLD"
        if chosen is not None and not blocked:
            action = "OPEN_LONG" if side == _LONG else "OPEN_SHORT"

        if action in _OPEN_ACTIONS and chosen is not None and chosen.planned_exit is not None:
            planned_exit = chosen.planned_exit
            metadata.update(planned_exit.as_metadata(side=side))
            metadata["setup"] = chosen.name
            metadata["confluence"] = len(ready)
            state[_KEY_PENDING] = {
                "side": side,
                "setup": chosen.name,
                "stop_price": str(planned_exit.stop_price),
                "created_close_time_ms": reading.close_time_ms,
            }
            state[_KEY_LAST_ENTRY_SIGNAL] = reading.close_time_ms
            self._count_day_entry(state, reading.close_time_ms)
            others = [candidate.name for candidate in ready if candidate is not chosen]
            confluence = f" + {', '.join(others)}" if others else ""
            reason = (
                f"Intraday confluence {side} via {chosen.name}{confluence}: "
                f"risk {planned_exit.risk_abs / reading.atr:.2f} ATR, "
                f"liquid R/R {planned_exit.liquid_reward_risk:.2f}"
            )
            score = 100
        else:
            reason = self._waiting_reason(
                side=side,
                ready=ready,
                candidates=candidates,
                cooling_down=cooling_down,
                day_limit_reached=day_limit_reached,
                already_signalled=already_signalled,
            )
            best = max((candidate.completion for candidate in candidates), default=_ZERO)
            score = min(int(best * Decimal("89")), 89)

        return self._remember(
            state,
            SignalScore(
                symbol=symbol,
                score=score,
                reason=reason,
                action=action,
                confidence=score,
                strategy=self.__class__.__name__,
                suggested_next_check_seconds=self._next_check_seconds(action),
                data_age_seconds=market.data_age_seconds,
                metadata=metadata,
            ),
        )

    def _candidates(
        self,
        side: str,
        reading: MarketReading,
        entry_price: Decimal,
    ) -> list[SetupCandidate]:
        builders = {
            SETUP_PULLBACK: self._pullback,
            SETUP_VWAP_RECLAIM: self._vwap_reclaim,
            SETUP_SQUEEZE_BREAKOUT: self._squeeze_breakout,
            SETUP_LIQUIDITY_SWEEP: self._liquidity_sweep,
        }
        candidates = []
        for name in _SETUP_PRIORITY:
            if not self.enabled_setups[name]:
                continue
            setup = builders[name](side, reading)
            if setup is None:
                continue
            candidates.append(self._with_risk(setup, reading, entry_price))
        return candidates

    def _confirmation(self, side: str, reading: MarketReading) -> bool:
        direction_ok = (
            reading.close > reading.open if side == _LONG else reading.close < reading.open
        )
        return (
            direction_ok
            and reading.body_fraction >= self.min_body_fraction
            and reading.close_location(side) >= self.min_close_location
        )

    def _pullback(self, side: str, reading: MarketReading) -> SetupCandidate:
        long = side == _LONG
        buffer = reading.atr * self.stop_buffer_atr
        if long:
            conditions = {
                "structure": reading.value_ema > reading.structure_ema,
                "touched_value": reading.touched_value_below,
                "resumption": reading.close > reading.previous_high
                and reading.close > reading.value_ema,
                "confirmation_candle": self._confirmation(side, reading),
            }
            anchor = reading.pullback_low - buffer
        else:
            conditions = {
                "structure": reading.value_ema < reading.structure_ema,
                "touched_value": reading.touched_value_above,
                "resumption": reading.close < reading.previous_low
                and reading.close < reading.value_ema,
                "confirmation_candle": self._confirmation(side, reading),
            }
            anchor = reading.pullback_high + buffer
        conditions["volume"] = self._volume_ok(reading, self.min_relative_volume)
        return SetupCandidate(SETUP_PULLBACK, side, conditions, anchor)

    def _vwap_reclaim(self, side: str, reading: MarketReading) -> SetupCandidate | None:
        if reading.vwap is None:
            return None
        long = side == _LONG
        buffer = reading.atr * self.stop_buffer_atr
        if long:
            conditions = {
                "dipped_below_vwap": reading.closed_below_vwap,
                "reclaimed_vwap": reading.close > reading.vwap,
                "confirmation_candle": self._confirmation(side, reading),
            }
            anchor = reading.vwap_swing_low - buffer
        else:
            conditions = {
                "popped_above_vwap": reading.closed_above_vwap,
                "lost_vwap": reading.close < reading.vwap,
                "confirmation_candle": self._confirmation(side, reading),
            }
            anchor = reading.vwap_swing_high + buffer
        conditions["volume"] = self._volume_ok(reading, self.min_relative_volume)
        return SetupCandidate(SETUP_VWAP_RECLAIM, side, conditions, anchor)

    def _squeeze_breakout(self, side: str, reading: MarketReading) -> SetupCandidate:
        long = side == _LONG
        buffer = reading.atr * self.stop_buffer_atr
        squeezed = (
            reading.squeeze_rank is not None and reading.squeeze_rank <= self.squeeze_percentile
        )
        if long:
            conditions = {
                "squeeze": squeezed,
                "outside_band": reading.close > reading.band_upper,
                "range_breakout": reading.close > reading.breakout_high,
                "confirmation_candle": self._confirmation(side, reading),
            }
            anchor = reading.base_low - buffer
        else:
            conditions = {
                "squeeze": squeezed,
                "outside_band": reading.close < reading.band_lower,
                "range_breakout": reading.close < reading.breakout_low,
                "confirmation_candle": self._confirmation(side, reading),
            }
            anchor = reading.base_high + buffer
        conditions["volume"] = self._volume_ok(reading, self.breakout_min_relative_volume)
        return SetupCandidate(SETUP_SQUEEZE_BREAKOUT, side, conditions, anchor)

    def _liquidity_sweep(self, side: str, reading: MarketReading) -> SetupCandidate:
        long = side == _LONG
        buffer = reading.atr * self.stop_buffer_atr
        if long:
            level = reading.sweep_level_low
            depth = (level - reading.low) / reading.atr
            conditions = {
                "swept_low": reading.low < level,
                "reclaimed_level": reading.close > level,
                "rejection_wick": reading.lower_wick_fraction >= self.sweep_min_wick_fraction,
            }
            anchor = reading.low - buffer
        else:
            level = reading.sweep_level_high
            depth = (reading.high - level) / reading.atr
            conditions = {
                "swept_high": reading.high > level,
                "reclaimed_level": reading.close < level,
                "rejection_wick": reading.upper_wick_fraction >= self.sweep_min_wick_fraction,
            }
            anchor = reading.high + buffer
        conditions["sweep_depth"] = self.sweep_min_depth_atr <= depth <= self.sweep_max_depth_atr
        conditions["close_location"] = reading.close_location(side) >= self.sweep_min_close_location
        conditions["volume"] = self._volume_ok(reading, self.min_relative_volume)
        return SetupCandidate(SETUP_LIQUIDITY_SWEEP, side, conditions, anchor)

    def _volume_ok(self, reading: MarketReading, minimum: Decimal) -> bool:
        if minimum <= _ZERO:
            return True
        return reading.relative_volume is not None and reading.relative_volume >= minimum

    def _with_risk(
        self,
        setup: SetupCandidate,
        reading: MarketReading,
        entry_price: Decimal,
    ) -> SetupCandidate:
        long = setup.side == _LONG
        risk = entry_price - setup.stop_anchor if long else setup.stop_anchor - entry_price
        minimum_risk = reading.atr * self.min_stop_atr
        if risk < minimum_risk:
            risk = minimum_risk
        cost_abs = entry_price * self.estimated_round_trip_cost_bps / _BASIS_POINTS
        filters = {
            "not_climax": reading.range_atr <= self.max_signal_range_atr,
            "stop_within_limit": risk <= reading.atr * self.max_stop_atr,
            "cost_efficient": risk > _ZERO and cost_abs <= risk * self.max_cost_to_risk,
        }
        planned_exit = _build_planned_exit(
            side=setup.side,
            entry_price=entry_price,
            risk_abs=risk,
            take_profit_r=self.take_profit_r,
            estimated_round_trip_cost_bps=self.estimated_round_trip_cost_bps,
        )
        filters["liquid_reward_risk"] = (
            planned_exit is not None
            and planned_exit.liquid_reward_risk >= self.minimum_liquid_reward_risk
        )
        return SetupCandidate(
            name=setup.name,
            side=setup.side,
            conditions=setup.conditions,
            stop_anchor=setup.stop_anchor,
            risk_abs=risk,
            planned_exit=planned_exit,
            filters=filters,
        )

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _manage_position(
        self,
        state: dict[str, Any],
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        reading: MarketReading,
    ) -> SignalScore:
        side = _LONG if position.direction is PositionDirection.LONG else _SHORT
        trade = self._trade_state(state, position, side, reading)
        management = self._evaluate_exit(trade, reading)

        action = "CLOSE_POSITION" if management.exit_reason else "HOLD"
        if management.exit_reason:
            reason = (
                f"Intraday confluence exit {side} ({management.setup}): "
                f"{management.exit_reason} (bars={management.bars_held}, "
                f"R={management.current_r:.2f}, max R={management.max_favorable_r:.2f})"
            )
        else:
            trail = str(management.trail_price) if management.trail_price is not None else "off"
            reason = (
                f"Intraday confluence holding {side} ({management.setup}): "
                f"R={management.current_r:.2f}, max R={management.max_favorable_r:.2f}, "
                f"trail={trail}, bars={management.bars_held}"
            )

        metadata = self._base_metadata(reading)
        metadata["phase"] = "MANAGEMENT"
        metadata["trade_management"] = management.as_metadata()

        score = 100 if action == "CLOSE_POSITION" else 50
        return self._remember(
            state,
            SignalScore(
                symbol=symbol,
                score=score,
                reason=reason,
                action=action,
                confidence=score,
                strategy=self.__class__.__name__,
                suggested_next_check_seconds=self._next_check_seconds(action),
                data_age_seconds=market.data_age_seconds,
                metadata=metadata,
            ),
        )

    def _trade_state(
        self,
        state: dict[str, Any],
        position: FuturesPosition,
        side: str,
        reading: MarketReading,
    ) -> dict[str, Any]:
        entry_price = to_decimal(position.entry_price)
        trade = state.get(_KEY_TRADE)
        if not isinstance(trade, dict) or trade.get("side") != side:
            trade = self._new_trade_state(state, side, entry_price, reading)
            state[_KEY_TRADE] = trade
            return trade

        # Partial fills move the average entry; keep the trade's history and risk.
        if entry_price > _ZERO and _optional_decimal(trade.get("entry_price")) != entry_price:
            trade["entry_price"] = str(entry_price)

        last_close_time_ms = int(trade.get("last_close_time_ms", 0) or 0)
        if reading.close_time_ms > last_close_time_ms:
            best = _optional_decimal(trade.get("best_price")) or entry_price
            best = max(best, reading.high) if side == _LONG else min(best, reading.low)
            trade["best_price"] = str(best)
            trade["bars_held"] = int(trade.get("bars_held", 0) or 0) + 1
            trade["last_close_time_ms"] = reading.close_time_ms
        return trade

    def _new_trade_state(
        self,
        state: dict[str, Any],
        side: str,
        entry_price: Decimal,
        reading: MarketReading,
    ) -> dict[str, Any]:
        pending = state.pop(_KEY_PENDING, None)
        initial_risk: Decimal | None = None
        setup = "UNKNOWN"
        if isinstance(pending, dict) and pending.get("side") == side:
            setup = str(pending.get("setup") or "UNKNOWN")
            planned_stop = _optional_decimal(pending.get("stop_price"))
            if planned_stop is not None:
                distance = (
                    entry_price - planned_stop if side == _LONG else planned_stop - entry_price
                )
                if distance > _ZERO:
                    initial_risk = distance
        if initial_risk is None:
            # Position opened without a matching plan (manual, restored or filled late):
            # assume the minimum structural risk so R-multiples stay meaningful.
            initial_risk = reading.atr * self.min_stop_atr

        # The first candle seen with the position open was traded through while holding
        # it, so its extreme counts and it is the first bar held.
        best = max(entry_price, reading.high) if side == _LONG else min(entry_price, reading.low)
        return {
            "side": side,
            "setup": setup,
            "entry_price": str(entry_price),
            "initial_risk": str(initial_risk),
            "best_price": str(best),
            "bars_held": 1,
            "last_close_time_ms": reading.close_time_ms,
        }

    def _evaluate_exit(self, trade: dict[str, Any], reading: MarketReading) -> TradeManagement:
        side = str(trade["side"])
        long = side == _LONG
        entry_price = to_decimal(trade["entry_price"])
        initial_risk = to_decimal(trade["initial_risk"])
        best_price = to_decimal(trade["best_price"])
        bars_held = int(trade.get("bars_held", 0) or 0)
        if initial_risk <= _ZERO:
            initial_risk = reading.atr * self.min_stop_atr

        favorable = best_price - entry_price if long else entry_price - best_price
        current = reading.close - entry_price if long else entry_price - reading.close
        max_favorable_r = favorable / initial_risk
        current_r = current / initial_risk

        trail_price: Decimal | None = None
        if max_favorable_r >= self.trail_activation_r:
            distance = reading.atr * self.trail_atr_multiplier
            trail_price = best_price - distance if long else best_price + distance
            initial_stop = entry_price - initial_risk if long else entry_price + initial_risk
            trail_price = max(trail_price, initial_stop) if long else min(trail_price, initial_stop)
            if max_favorable_r >= self.breakeven_after_r:
                cost_abs = entry_price * self.estimated_round_trip_cost_bps / _BASIS_POINTS
                breakeven = entry_price + cost_abs if long else entry_price - cost_abs
                trail_price = max(trail_price, breakeven) if long else min(trail_price, breakeven)

        trend_broken = (
            reading.close < reading.trend_slow_ema
            if long
            else reading.close > reading.trend_slow_ema
        )

        exit_reason = ""
        if trail_price is not None and (
            reading.close <= trail_price if long else reading.close >= trail_price
        ):
            exit_reason = "chandelier trail crossed on close"
        elif self.exit_on_trend_break and trend_broken:
            exit_reason = "price closed beyond the trend EMA"
        elif (
            self.max_bars_without_progress > 0
            and bars_held >= self.max_bars_without_progress
            and current_r < self.progress_r
        ):
            exit_reason = "time stop: trade did not progress"
        elif self.max_holding_bars > 0 and bars_held >= self.max_holding_bars:
            exit_reason = "maximum holding time reached"

        return TradeManagement(
            side=side,
            setup=str(trade.get("setup") or "UNKNOWN"),
            entry_price=entry_price,
            initial_risk=initial_risk,
            best_price=best_price,
            bars_held=bars_held,
            max_favorable_r=max_favorable_r,
            current_r=current_r,
            trail_price=trail_price,
            exit_reason=exit_reason,
        )

    def _register_exit_if_any(self, state: dict[str, Any], close_time_ms: int) -> None:
        """A flat position with live trade bookkeeping means the trade just ended."""

        if isinstance(state.get(_KEY_TRADE), dict):
            state.pop(_KEY_TRADE, None)
            state[_KEY_LAST_EXIT] = close_time_ms

    def _expire_pending_entry(
        self,
        state: dict[str, Any],
        position: FuturesPosition,
        close_time_ms: int,
    ) -> None:
        pending = state.get(_KEY_PENDING)
        if not isinstance(pending, dict) or position.is_open:
            return
        created = int(pending.get("created_close_time_ms", 0) or 0)
        if close_time_ms - created > self._duration_ms * _PENDING_ENTRY_MAX_CANDLES:
            state.pop(_KEY_PENDING, None)

    def _cooling_down(self, state: dict[str, Any], close_time_ms: int) -> bool:
        if self.cooldown_bars <= 0:
            return False
        last_exit = int(state.get(_KEY_LAST_EXIT, 0) or 0)
        if last_exit <= 0:
            return False
        return close_time_ms - last_exit < self._duration_ms * self.cooldown_bars

    def _day_limit_reached(self, state: dict[str, Any], close_time_ms: int) -> bool:
        if self.max_entries_per_day <= 0 or close_time_ms <= 0:
            return False
        record = state.get(_KEY_DAY_ENTRIES)
        if not isinstance(record, dict):
            return False
        if int(record.get("day", -1)) != close_time_ms // _DAY_MS:
            return False
        return int(record.get("count", 0) or 0) >= self.max_entries_per_day

    def _count_day_entry(self, state: dict[str, Any], close_time_ms: int) -> None:
        day = close_time_ms // _DAY_MS
        record = state.get(_KEY_DAY_ENTRIES)
        if isinstance(record, dict) and int(record.get("day", -1)) == day:
            record["count"] = int(record.get("count", 0) or 0) + 1
        else:
            state[_KEY_DAY_ENTRIES] = {"day": day, "count": 1}

    # ------------------------------------------------------------------
    # Indicators
    # ------------------------------------------------------------------

    def _reading(self, candles: Sequence[Any]) -> MarketReading | None:
        window = list(candles[-self.history :])
        if len(window) < self.history:
            return None

        opens = [to_decimal(candle.open) for candle in window]
        highs = [to_decimal(candle.high) for candle in window]
        lows = [to_decimal(candle.low) for candle in window]
        closes = [to_decimal(candle.close) for candle in window]
        last = len(window) - 1
        if highs[last] <= lows[last]:
            return None

        atr = average_true_range(window, self.atr_period)
        if atr is None or atr <= _ZERO:
            return None

        trend_fast = exponential_moving_average_series(closes, self.trend_fast_ema_period)
        trend_slow = exponential_moving_average_series(closes, self.trend_slow_ema_period)
        value = exponential_moving_average_series(closes, self.value_ema_period)
        structure = exponential_moving_average_series(closes, self.structure_ema_period)
        fast_now = trend_fast[last]
        fast_past = trend_fast[last - self.trend_slope_bars]
        slow_now = trend_slow[last]
        value_now = value[last]
        structure_now = structure[last]
        if None in (fast_now, fast_past, slow_now, value_now, structure_now):
            return None

        # Pullback: the last ``pullback_lookback`` candles including the signal candle
        # reached the value zone, the EMA21 widened by ``value_zone_atr`` ATRs.
        pullback_start = len(window) - self.pullback_lookback
        zone = atr * self.value_zone_atr
        touched_below = False
        touched_above = False
        for index in range(pullback_start, len(window)):
            ema_at = value[index]
            if ema_at is None:
                continue
            if lows[index] <= ema_at + zone:
                touched_below = True
            if highs[index] >= ema_at - zone:
                touched_above = True

        # VWAP: the series of the last few candles, so a recent close can be compared
        # with the VWAP of its own time.
        vwap_slice = window[-(self.vwap_window + self.vwap_lookback) :]
        vwap_series = rolling_vwap_series(vwap_slice, self.vwap_window)
        vwap_now = vwap_series[-1]
        closed_below = False
        closed_above = False
        for offset in range(2, self.vwap_lookback + 2):
            vwap_at = vwap_series[-offset]
            if vwap_at is None:
                continue
            close_at = closes[-offset]
            if close_at < vwap_at:
                closed_below = True
            if close_at > vwap_at:
                closed_above = True
        vwap_swing_low = min(lows[-(self.vwap_lookback + 1) :])
        vwap_swing_high = max(highs[-(self.vwap_lookback + 1) :])

        # Squeeze: rank the narrowest recent band width against its own history.
        width_closes = closes[-(self.squeeze_history + self.bollinger_period) :]
        widths = bollinger_bandwidth_series(width_closes, self.bollinger_period, self.bollinger_std)
        history_widths = [
            width for width in widths[-(self.squeeze_history + 1) : -1] if width is not None
        ]
        recent_widths = [
            width for width in widths[-(self.squeeze_lookback + 1) : -1] if width is not None
        ]
        squeeze_rank = (
            percentile_rank(history_widths, min(recent_widths))
            if history_widths and recent_widths
            else None
        )
        bands = bollinger_bands(closes, self.bollinger_period, self.bollinger_std)
        if bands is None:
            return None

        volume = relative_volume(window, self.volume_period, required_multiplier=_ONE)

        return MarketReading(
            open=opens[last],
            high=highs[last],
            low=lows[last],
            close=closes[last],
            close_time_ms=int(getattr(window[last], "close_time_ms", 0) or 0),
            atr=atr,
            relative_volume=volume.ratio if volume is not None else None,
            trend_fast_ema=fast_now,
            trend_fast_ema_past=fast_past,
            trend_slow_ema=slow_now,
            require_trend_slope=self.require_trend_slope,
            value_ema=value_now,
            structure_ema=structure_now,
            previous_high=highs[last - 1],
            previous_low=lows[last - 1],
            touched_value_below=touched_below,
            touched_value_above=touched_above,
            pullback_low=min(lows[pullback_start:]),
            pullback_high=max(highs[pullback_start:]),
            vwap=vwap_now,
            closed_below_vwap=closed_below,
            closed_above_vwap=closed_above,
            vwap_swing_low=vwap_swing_low,
            vwap_swing_high=vwap_swing_high,
            squeeze_rank=squeeze_rank,
            band_upper=bands.upper,
            band_lower=bands.lower,
            breakout_high=max(highs[-(self.breakout_lookback + 1) : -1]),
            breakout_low=min(lows[-(self.breakout_lookback + 1) : -1]),
            base_low=min(lows[-self.breakout_base_bars :]),
            base_high=max(highs[-self.breakout_base_bars :]),
            sweep_level_low=min(lows[-(self.sweep_lookback + 1) : -1]),
            sweep_level_high=max(highs[-(self.sweep_lookback + 1) : -1]),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_check_seconds(self, action: str) -> float:
        if action in _ACTIVE_ACTIONS:
            return 0.0
        return self._duration_ms / 1000.0

    def _waiting_reason(
        self,
        *,
        side: str,
        ready: list[SetupCandidate],
        candidates: list[SetupCandidate],
        cooling_down: bool,
        day_limit_reached: bool,
        already_signalled: bool,
    ) -> str:
        if not side:
            return "Intraday confluence waiting: no trend bias (EMA50/EMA200 not aligned)"
        if ready:
            names = ", ".join(candidate.name for candidate in ready)
            if day_limit_reached:
                return f"Intraday confluence {side} {names} ignored: daily entry limit reached"
            if cooling_down:
                return f"Intraday confluence {side} {names} ignored: cooling down after exit"
            if already_signalled:
                return f"Intraday confluence {side} {names} ignored: candle already signalled"
        progress = ", ".join(
            f"{candidate.name}={int(candidate.completion * 100)}%" for candidate in candidates
        )
        return f"Intraday confluence waiting ({side} bias): {progress}"

    def _base_metadata(self, reading: MarketReading) -> dict[str, Any]:
        return {
            "source": "FIXED_INTRADAY_TREND_CONFLUENCE",
            "close": str(reading.close),
            "atr": str(reading.atr),
            "relative_volume": (
                str(reading.relative_volume) if reading.relative_volume is not None else None
            ),
            "trend_fast_ema": str(reading.trend_fast_ema),
            "trend_slow_ema": str(reading.trend_slow_ema),
            "bullish_bias": reading.bullish_bias,
            "bearish_bias": reading.bearish_bias,
            "value_ema": str(reading.value_ema),
            "structure_ema": str(reading.structure_ema),
            "vwap": str(reading.vwap) if reading.vwap is not None else None,
            "squeeze_rank": (
                str(reading.squeeze_rank) if reading.squeeze_rank is not None else None
            ),
            "band_upper": str(reading.band_upper),
            "band_lower": str(reading.band_lower),
            "signal_range_atr": str(reading.range_atr),
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
                reason=f"Intraday confluence: {detail}",
                action="HOLD",
                confidence=0,
                strategy=self.__class__.__name__,
                data_age_seconds=market.data_age_seconds,
            ),
        )

    @staticmethod
    def _remember(state: dict[str, Any], score: SignalScore) -> SignalScore:
        state[_KEY_ACTION] = score.action or "HOLD"
        state[_KEY_REASON] = score.reason
        state[_KEY_SCORE] = int(score.score)
        state[_KEY_METADATA] = dict(score.metadata)
        return score


def _first_by_priority(candidates: list[SetupCandidate]) -> SetupCandidate | None:
    for name in _SETUP_PRIORITY:
        for candidate in candidates:
            if candidate.name == name:
                return candidate
    return None


def _build_planned_exit(
    *,
    side: str,
    entry_price: Decimal,
    risk_abs: Decimal,
    take_profit_r: Decimal,
    estimated_round_trip_cost_bps: Decimal,
) -> PlannedExit | None:
    if entry_price <= _ZERO or risk_abs <= _ZERO:
        return None

    reward_abs = risk_abs * take_profit_r
    if side == _LONG:
        stop_price = entry_price - risk_abs
        take_profit_price = entry_price + reward_abs
    else:
        stop_price = entry_price + risk_abs
        take_profit_price = entry_price - reward_abs
    if stop_price <= _ZERO or take_profit_price <= _ZERO:
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


def _positive_or(value: Decimal, fallback: Decimal) -> Decimal:
    return value if value > _ZERO else fallback


def _positive_int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _non_negative_int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if result < 0:
        raise ValueError(f"{name} cannot be negative")
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


def _fraction(value: Any, name: str) -> Decimal:
    result = _non_negative_decimal(value, name)
    if result > _ONE:
        raise ValueError(f"{name} must be between 0 and 1")
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
    "IntradayTrendConfluenceStrategy",
    "MarketReading",
    "SetupCandidate",
    "TradeManagement",
]
