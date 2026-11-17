"""Multi-timeframe trend-pullback continuation with a trailing (chandelier) exit.

Why this design
---------------
The evidence that survives costs in crypto futures points to *trend* (time-series
momentum), not to fast scalping or fading moves:

* Time-series momentum: an asset's own past return predicts its next return
  (Moskowitz, Ooi & Pedersen 2012; Hurst, Ooi & Pedersen, "A Century of Evidence
  on Trend-Following"). In crypto the effect is strong at 1-4 week horizons
  (Liu & Tsyvinski 2021, "Risks and Returns of Cryptocurrency").
* Trend-following pays through a convex payoff: many small losses, few large
  winners. A fixed, near take-profit cuts exactly the right tail that pays for
  the strategy, so winners here are managed by a trailing exit and the
  take-profit is only a far backstop.
* Entering on a *pullback* inside the trend, instead of on the breakout itself,
  buys at "value" with the stop under a real structure (the pullback extreme),
  which shrinks the risk per trade and raises the reward/risk (Raschke's
  "Holy Grail" family of setups: strong ADX, pullback to the 20 EMA, resumption).
* Costs decide short-horizon edges. At 1h the average true range is several
  times the round-trip cost, while at 5m/15m the cost eats a large slice of
  every stop. The setup is rejected whenever cost/risk is too high.
* Volatility-normalised risk: stops are measured in ATR, so the position sizer
  risks the same fraction of equity in calm and violent markets
  (Moreira & Muir 2017, "Volatility-Managed Portfolios").

Rules (LONG; SHORT mirrors them)
--------------------------------
Regime, higher timeframe (default 4h), all of:
  close > EMA50 > EMA200, EMA50 rising, ADX >= 20 with +DI > -DI, and a positive
  ~1-week return (42 x 4h candles).
Setup, primary timeframe (default 1h):
  EMA21 > EMA55; within the last 8 closed candles price traded into the EMA21
  "value zone" (low <= EMA21 of that candle).
Trigger, on the candle that just closed:
  bullish candle that closes above the highs of the previous 2 candles and above
  EMA21, with at least average volume and a range no larger than 2.5 ATR
  (no chasing climax candles).
Risk:
  stop = pullback low - 0.2 ATR, clamped to at least 1 ATR and rejected beyond
  2.5 ATR; rejected when round-trip cost exceeds 15% of the risk.
  take-profit = 4R (backstop only).
Management, evaluated on every closed primary candle while in a position:
  * chandelier trail: after +1R, exit on a close below the highest high since
    entry minus 3 ATR; after +1.5R the trail never sits below break-even + cost;
  * trend break: exit when the higher-timeframe close loses its EMA200;
  * time stop: after 36 candles a trade that is not at least +0.5R is closed.
After any exit the symbol waits 4 primary candles before a new entry.

Backtesting
-----------
Run with the primary CSV at ``primary_timeframe`` (1h) and a secondary CSV at
``higher_timeframe`` (4h) for every symbol. The higher-timeframe warm-up is long
(600 x 4h, ~100 days) so the EMA200 is properly converged before the first trade.
No parameter here was fitted to data: they are the textbook values of each
component. Validate them out of sample before trusting any result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeDecision
from domain.strategy_requirements import (
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    StrategyDataRequirements,
)
from domain.timeframe import timeframe_duration_ms
from indicators.adx import directional_movement_index, minimum_adx_candles
from indicators.atr import average_true_range
from indicators.common import to_decimal
from indicators.relative_volume import relative_volume
from scanner.signal_score import SignalScore
from strategies.futures.base import FuturesStrategyV2, StrategyMetadata
from strategies.futures.fixed.donchian_adx_breakout import PlannedExit
from trading.intent import TradeIntent

_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_BASIS_POINTS = Decimal("10000")

_LONG = "LONG"
_SHORT = "SHORT"
_OPEN_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}
_ACTIVE_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"}

# State keys. Everything stored in ``state`` must stay JSON-serialisable because the
# live Governor persists the per-symbol strategy state.
_KEY_ACTION = "trend_pullback_action"
_KEY_REASON = "trend_pullback_reason"
_KEY_SCORE = "trend_pullback_score"
_KEY_METADATA = "trend_pullback_metadata"
_KEY_TRADE = "trend_pullback_trade"
_KEY_PENDING = "trend_pullback_pending_entry"
_KEY_LAST_EXIT = "trend_pullback_last_exit_close_time_ms"
_KEY_LAST_ENTRY_SIGNAL = "trend_pullback_last_entry_signal_close_time_ms"

# A pending entry that has not turned into a position after this many primary
# candles was rejected or never filled.
_PENDING_ENTRY_MAX_CANDLES = 3


@dataclass(frozen=True)
class TrendRegime:
    """Higher-timeframe trend state used to allow entries and to invalidate trades."""

    close: Decimal
    fast_ema: Decimal
    previous_fast_ema: Decimal
    slow_ema: Decimal
    adx: Decimal
    plus_di: Decimal
    minus_di: Decimal
    momentum_return: Decimal
    adx_threshold: Decimal

    @property
    def trending(self) -> bool:
        return self.adx >= self.adx_threshold

    @property
    def bullish(self) -> bool:
        return (
            self.close > self.fast_ema > self.slow_ema
            and self.fast_ema > self.previous_fast_ema
            and self.plus_di > self.minus_di
            and self.momentum_return > _ZERO
            and self.trending
        )

    @property
    def bearish(self) -> bool:
        return (
            self.close < self.fast_ema < self.slow_ema
            and self.fast_ema < self.previous_fast_ema
            and self.minus_di > self.plus_di
            and self.momentum_return < _ZERO
            and self.trending
        )

    def breaks(self, side: str) -> bool:
        """Whether the trend that justified a position of ``side`` is gone."""

        if side == _LONG:
            return self.close < self.slow_ema
        return self.close > self.slow_ema


@dataclass(frozen=True)
class PrimaryReading:
    """Primary-timeframe values of the candle that just closed."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    close_time_ms: int
    atr: Decimal
    value_ema: Decimal
    structure_ema: Decimal
    relative_volume_ratio: Decimal | None
    touched_value_below: bool
    touched_value_above: bool
    pullback_low: Decimal
    pullback_high: Decimal
    trigger_high: Decimal
    trigger_low: Decimal

    @property
    def range_atr(self) -> Decimal:
        return (self.high - self.low) / self.atr


@dataclass(frozen=True)
class TradeManagement:
    """Exit evaluation of an open position on the latest closed primary candle."""

    side: str
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
            "entry_price": str(self.entry_price),
            "initial_risk": str(self.initial_risk),
            "best_price": str(self.best_price),
            "bars_held": self.bars_held,
            "max_favorable_r": str(self.max_favorable_r),
            "current_r": str(self.current_r),
            "trail_price": str(self.trail_price) if self.trail_price is not None else None,
            "exit_reason": self.exit_reason or None,
        }


class TrendPullbackContinuationStrategy(FuturesStrategyV2):
    """Trend-following pullback entries (1h) inside a confirmed 4h trend, trailing exit.

    Enters in the direction of a higher-timeframe trend confirmed by EMA structure,
    ADX and time-series momentum, only after a pullback into the 1h EMA21 value zone
    resumes. The stop sits under the pullback extreme, setups are skipped when costs
    are a large share of the risk, and winners are held with a chandelier trail
    instead of a near take-profit. Trend breaks and stalled trades are closed early.
    """

    strategy_category = "FIXED_TREND"

    metadata = StrategyMetadata(
        strategy_name="TrendPullbackContinuationStrategy",
        supports_scoring=True,
        supports_long=True,
        supports_short=True,
        required_history=300,
        required_timeframes=["1h", "4h"],
        required_indicators=[
            "EMA",
            "ADX",
            "ATR",
            "RELATIVE_VOLUME",
            "TIME_SERIES_MOMENTUM",
        ],
    )

    def __init__(
        self,
        *,
        primary_timeframe: str = "1h",
        higher_timeframe: str = "4h",
        primary_history: int = 300,
        higher_timeframe_history: int = 600,
        htf_fast_ema_period: int = 50,
        htf_slow_ema_period: int = 200,
        htf_adx_period: int = 14,
        htf_adx_threshold: Decimal | int | float | str = Decimal("20"),
        htf_momentum_lookback: int = 42,
        value_ema_period: int = 21,
        structure_ema_period: int = 55,
        atr_period: int = 14,
        pullback_lookback: int = 8,
        trigger_lookback: int = 2,
        volume_period: int = 20,
        min_relative_volume: Decimal | int | float | str = Decimal("1.0"),
        max_trigger_range_atr: Decimal | int | float | str = Decimal("2.5"),
        stop_buffer_atr: Decimal | int | float | str = Decimal("0.2"),
        min_stop_atr: Decimal | int | float | str = Decimal("1.0"),
        max_stop_atr: Decimal | int | float | str = Decimal("2.5"),
        take_profit_r: Decimal | int | float | str = Decimal("4"),
        minimum_liquid_reward_risk: Decimal | int | float | str = Decimal("2"),
        estimated_round_trip_cost_bps: Decimal | int | float | str = Decimal("15"),
        max_cost_to_risk: Decimal | int | float | str = Decimal("0.15"),
        trail_activation_r: Decimal | int | float | str = Decimal("1.0"),
        trail_atr_multiplier: Decimal | int | float | str = Decimal("3"),
        breakeven_after_r: Decimal | int | float | str = Decimal("1.5"),
        max_bars_without_progress: int = 36,
        progress_r: Decimal | int | float | str = Decimal("0.5"),
        cooldown_bars: int = 4,
        exit_on_trend_break: bool = True,
        enable_long: bool = True,
        enable_short: bool = True,
    ) -> None:
        self.primary_timeframe = _non_empty_text(primary_timeframe, "primary_timeframe")
        self.higher_timeframe = _non_empty_text(higher_timeframe, "higher_timeframe")
        try:
            primary_ms = timeframe_duration_ms(self.primary_timeframe)
            higher_ms = timeframe_duration_ms(self.higher_timeframe)
        except (KeyError, ValueError) as error:
            raise ValueError(f"unsupported timeframe: {error}") from error
        if higher_ms <= primary_ms:
            raise ValueError("higher_timeframe must be longer than primary_timeframe")
        self._primary_duration_ms = primary_ms

        self.htf_fast_ema_period = _positive_int(htf_fast_ema_period, "htf_fast_ema_period")
        self.htf_slow_ema_period = _positive_int(htf_slow_ema_period, "htf_slow_ema_period")
        if self.htf_fast_ema_period >= self.htf_slow_ema_period:
            raise ValueError("htf_fast_ema_period must be below htf_slow_ema_period")
        self.htf_adx_period = _positive_int(htf_adx_period, "htf_adx_period")
        self.htf_adx_threshold = _non_negative_decimal(htf_adx_threshold, "htf_adx_threshold")
        self.htf_momentum_lookback = _positive_int(htf_momentum_lookback, "htf_momentum_lookback")

        self.value_ema_period = _positive_int(value_ema_period, "value_ema_period")
        self.structure_ema_period = _positive_int(structure_ema_period, "structure_ema_period")
        if self.value_ema_period >= self.structure_ema_period:
            raise ValueError("value_ema_period must be below structure_ema_period")
        self.atr_period = _positive_int(atr_period, "atr_period")
        self.pullback_lookback = _positive_int(pullback_lookback, "pullback_lookback")
        self.trigger_lookback = _positive_int(trigger_lookback, "trigger_lookback")
        self.volume_period = _positive_int(volume_period, "volume_period")
        self.min_relative_volume = _non_negative_decimal(min_relative_volume, "min_relative_volume")
        self.max_trigger_range_atr = _positive_decimal(
            max_trigger_range_atr, "max_trigger_range_atr"
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
        self.cooldown_bars = _non_negative_int(cooldown_bars, "cooldown_bars")
        self.exit_on_trend_break = bool(exit_on_trend_break)

        self.enable_long = bool(enable_long)
        self.enable_short = bool(enable_short)
        if not self.enable_long and not self.enable_short:
            raise ValueError("at least one trade direction must be enabled")

        minimum_primary_history = max(
            self.structure_ema_period + self.pullback_lookback + self.trigger_lookback + 1,
            self.atr_period + 1,
            self.volume_period + 1,
        )
        self.primary_history = _positive_int(primary_history, "primary_history")
        if self.primary_history < minimum_primary_history:
            raise ValueError(
                "primary_history is too small for the configured indicators: "
                f"required>={minimum_primary_history}, received={self.primary_history}"
            )

        minimum_higher_history = max(
            self.htf_slow_ema_period + 1,
            self.htf_fast_ema_period + 2,
            self.htf_momentum_lookback + 1,
            minimum_adx_candles(self.htf_adx_period, require_previous_adx=False),
        )
        self.higher_timeframe_history = _positive_int(
            higher_timeframe_history, "higher_timeframe_history"
        )
        if self.higher_timeframe_history < minimum_higher_history:
            raise ValueError(
                "higher_timeframe_history is too small for the trend regime: "
                f"required>={minimum_higher_history}, "
                f"received={self.higher_timeframe_history}"
            )

    # ------------------------------------------------------------------
    # Construction / contract
    # ------------------------------------------------------------------

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> TrendPullbackContinuationStrategy:
        defaults = cls.describe_parameters()
        values: dict[str, Any] = {}
        for name, spec in defaults.items():
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
            "primary_timeframe": {
                "default": "1h",
                "description": "Candle interval of the pullback setup and the trigger.",
            },
            "higher_timeframe": {
                "default": "4h",
                "description": "Longer interval that must show a confirmed trend.",
            },
            "primary_history": {
                "default": 300,
                "minimum": 66,
                "description": "Closed primary candles used for indicators (EMA warm-up).",
            },
            "higher_timeframe_history": {
                "default": 600,
                "minimum": 201,
                "description": "Closed higher-timeframe candles; ~3x the slow EMA converges it.",
            },
            "htf_fast_ema_period": {"default": 50, "minimum": 2},
            "htf_slow_ema_period": {"default": 200, "minimum": 3},
            "htf_adx_period": {"default": 14, "minimum": 2},
            "htf_adx_threshold": {
                "default": "20",
                "minimum": "0",
                "description": "Minimum higher-timeframe ADX: below it the market is ranging.",
            },
            "htf_momentum_lookback": {
                "default": 42,
                "minimum": 1,
                "description": "Higher-timeframe candles of the time-series momentum return.",
            },
            "value_ema_period": {
                "default": 21,
                "minimum": 2,
                "description": "Primary EMA that defines the pullback value zone.",
            },
            "structure_ema_period": {
                "default": 55,
                "minimum": 3,
                "description": "Primary EMA that must stay on the trend side of the value EMA.",
            },
            "atr_period": {"default": 14, "minimum": 2},
            "pullback_lookback": {
                "default": 8,
                "minimum": 1,
                "description": "Recent primary candles where the pullback must have happened.",
            },
            "trigger_lookback": {
                "default": 2,
                "minimum": 1,
                "description": "Previous candles whose extreme the trigger close must clear.",
            },
            "volume_period": {"default": 20, "minimum": 2},
            "min_relative_volume": {
                "default": "1.0",
                "minimum": "0",
                "description": "Trigger volume / average volume. 0 disables the filter.",
            },
            "max_trigger_range_atr": {
                "default": "2.5",
                "minimum": "0.01",
                "description": "Reject climax trigger candles wider than this many ATRs.",
            },
            "stop_buffer_atr": {"default": "0.2", "minimum": "0"},
            "min_stop_atr": {
                "default": "1.0",
                "minimum": "0.01",
                "description": "Stops closer than this many ATRs are widened (noise guard).",
            },
            "max_stop_atr": {
                "default": "2.5",
                "minimum": "0.01",
                "description": "Setups whose structural stop is farther than this are skipped.",
            },
            "take_profit_r": {
                "default": "4",
                "minimum": "0.01",
                "description": "Backstop take-profit in R; the trailing exit manages winners.",
            },
            "minimum_liquid_reward_risk": {"default": "2", "minimum": "0.01"},
            "estimated_round_trip_cost_bps": {"default": "15", "minimum": "0"},
            "max_cost_to_risk": {
                "default": "0.15",
                "minimum": "0.01",
                "description": "Skip setups where round-trip cost exceeds this share of risk.",
            },
            "trail_activation_r": {
                "default": "1.0",
                "minimum": "0",
                "description": "Favourable excursion (in R) that arms the chandelier trail.",
            },
            "trail_atr_multiplier": {
                "default": "3",
                "minimum": "0.01",
                "description": "Chandelier distance from the best price, in ATRs.",
            },
            "breakeven_after_r": {
                "default": "1.5",
                "minimum": "0",
                "description": "After this excursion the trail never sits below break-even.",
            },
            "max_bars_without_progress": {
                "default": 36,
                "minimum": 0,
                "description": "Time stop in primary candles. 0 disables it.",
            },
            "progress_r": {
                "default": "0.5",
                "description": "Open profit (in R) a trade needs to survive the time stop.",
            },
            "cooldown_bars": {
                "default": 4,
                "minimum": 0,
                "description": "Primary candles to wait after an exit before a new entry.",
            },
            "exit_on_trend_break": {
                "default": True,
                "description": "Close when the higher-timeframe close crosses its slow EMA.",
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
                state, normalized_symbol, market, "waiting for the primary candle to close"
            )

        reading = self._primary_reading(primary_candles)
        regime = self._trend_regime(higher_candles)
        if reading is None or regime is None:
            return self._hold(state, normalized_symbol, market, "indicators are not ready")

        self._expire_pending_entry(state, position, reading.close_time_ms)

        if position.is_open:
            return self._manage_position(
                state, normalized_symbol, market, position, reading, regime
            )

        self._register_exit_if_any(state, reading.close_time_ms)
        return self._evaluate_entry(state, normalized_symbol, market, reading, regime)

    def intent_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> TradeIntent:
        action = str(state.get(_KEY_ACTION) or "HOLD")
        reason = str(state.get(_KEY_REASON) or "Trend pullback: no evaluated signal")
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
        reason = str(state.get(_KEY_REASON) or "Trend pullback: no evaluated signal")

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
        reading: PrimaryReading,
        regime: TrendRegime,
    ) -> SignalScore:
        entry_price = _positive_or(to_decimal(market.price), reading.close)
        cost_abs = entry_price * self.estimated_round_trip_cost_bps / _BASIS_POINTS

        long_conditions, long_exit = self._side_conditions(
            _LONG, reading, regime, entry_price, cost_abs
        )
        short_conditions, short_exit = self._side_conditions(
            _SHORT, reading, regime, entry_price, cost_abs
        )
        long_ready = self.enable_long and all(long_conditions.values())
        short_ready = self.enable_short and all(short_conditions.values())

        long_score = _condition_score(long_conditions)
        short_score = _condition_score(short_conditions)

        cooling_down = self._cooling_down(state, reading.close_time_ms)
        # A primary candle can be seen more than once (e.g. a faster backtest feed):
        # it may open at most one position.
        already_signalled = (
            reading.close_time_ms > 0
            and int(state.get(_KEY_LAST_ENTRY_SIGNAL, 0) or 0) == reading.close_time_ms
        )

        action = "HOLD"
        planned_exit: PlannedExit | None = None
        side = ""
        if (long_ready or short_ready) and not cooling_down and not already_signalled:
            side = _LONG if long_ready else _SHORT
            planned_exit = long_exit if long_ready else short_exit
            if (
                planned_exit is not None
                and planned_exit.liquid_reward_risk >= self.minimum_liquid_reward_risk
            ):
                action = "OPEN_LONG" if side == _LONG else "OPEN_SHORT"

        metadata = self._base_metadata(reading, regime)
        metadata.update(
            {
                "phase": "ENTRY",
                "long_conditions": dict(long_conditions),
                "short_conditions": dict(short_conditions),
                "long_score": long_score,
                "short_score": short_score,
                "cooling_down": cooling_down,
            }
        )

        if action in _OPEN_ACTIONS and planned_exit is not None:
            metadata.update(planned_exit.as_metadata(side=side))
            state[_KEY_PENDING] = {
                "side": side,
                "stop_price": str(planned_exit.stop_price),
                "atr": str(reading.atr),
                "created_close_time_ms": reading.close_time_ms,
            }
            state[_KEY_LAST_ENTRY_SIGNAL] = reading.close_time_ms
            reason = (
                f"Trend pullback {side} confirmed: higher-timeframe trend, pullback into the "
                f"value EMA and resumption candle agree (risk {planned_exit.risk_abs / reading.atr:.2f} "
                f"ATR, liquid R/R {planned_exit.liquid_reward_risk:.2f})"
            )
            score = 100
        else:
            reason = self._waiting_reason(
                long_score=long_score,
                short_score=short_score,
                long_ready=long_ready,
                short_ready=short_ready,
                cooling_down=cooling_down,
                already_signalled=already_signalled,
                planned_exit=planned_exit,
                regime=regime,
            )
            score = min(max(long_score, short_score), 89)

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

    def _side_conditions(
        self,
        side: str,
        reading: PrimaryReading,
        regime: TrendRegime,
        entry_price: Decimal,
        cost_abs: Decimal,
    ) -> tuple[dict[str, bool], PlannedExit | None]:
        long = side == _LONG
        if long:
            regime_ok = regime.bullish
            alignment = reading.value_ema > reading.structure_ema
            pullback = reading.touched_value_below
            trigger = (
                reading.close > reading.open
                and reading.close > reading.trigger_high
                and reading.close > reading.value_ema
            )
            structural_stop = reading.pullback_low - reading.atr * self.stop_buffer_atr
            risk = entry_price - structural_stop
        else:
            regime_ok = regime.bearish
            alignment = reading.value_ema < reading.structure_ema
            pullback = reading.touched_value_above
            trigger = (
                reading.close < reading.open
                and reading.close < reading.trigger_low
                and reading.close < reading.value_ema
            )
            structural_stop = reading.pullback_high + reading.atr * self.stop_buffer_atr
            risk = structural_stop - entry_price

        minimum_risk = reading.atr * self.min_stop_atr
        if risk < minimum_risk:
            risk = minimum_risk
        stop_within_limit = risk <= reading.atr * self.max_stop_atr
        cost_efficient = risk > _ZERO and cost_abs <= risk * self.max_cost_to_risk

        volume_ok = self.min_relative_volume <= _ZERO or (
            reading.relative_volume_ratio is not None
            and reading.relative_volume_ratio >= self.min_relative_volume
        )

        conditions = {
            "htf_trend": regime_ok,
            "primary_alignment": alignment,
            "pullback_to_value": pullback,
            "resumption_trigger": trigger,
            "volume": volume_ok,
            "not_climax": reading.range_atr <= self.max_trigger_range_atr,
            "stop_within_limit": stop_within_limit,
            "cost_efficient": cost_efficient,
        }

        planned_exit = _build_planned_exit(
            side=side,
            entry_price=entry_price,
            risk_abs=risk,
            take_profit_r=self.take_profit_r,
            estimated_round_trip_cost_bps=self.estimated_round_trip_cost_bps,
        )
        return conditions, planned_exit

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _manage_position(
        self,
        state: dict[str, Any],
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        reading: PrimaryReading,
        regime: TrendRegime,
    ) -> SignalScore:
        side = _LONG if position.direction is PositionDirection.LONG else _SHORT
        trade = self._trade_state(state, position, side, reading)

        management = self._evaluate_exit(trade, reading, regime)

        action = "CLOSE_POSITION" if management.exit_reason else "HOLD"
        if management.exit_reason:
            reason = (
                f"Trend pullback exit {side}: {management.exit_reason} "
                f"(bars={management.bars_held}, R={management.current_r:.2f}, "
                f"max R={management.max_favorable_r:.2f})"
            )
        else:
            trail = (
                f"{management.trail_price}" if management.trail_price is not None else "inactive"
            )
            reason = (
                f"Trend pullback holding {side}: R={management.current_r:.2f}, "
                f"max R={management.max_favorable_r:.2f}, trail={trail}, "
                f"bars={management.bars_held}"
            )

        metadata = self._base_metadata(reading, regime)
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
        reading: PrimaryReading,
    ) -> dict[str, Any]:
        """Create or advance the persisted bookkeeping of the open position."""

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
        reading: PrimaryReading,
    ) -> dict[str, Any]:
        pending = state.pop(_KEY_PENDING, None)
        initial_risk: Decimal | None = None
        if isinstance(pending, dict) and pending.get("side") == side:
            planned_stop = _optional_decimal(pending.get("stop_price"))
            if planned_stop is not None:
                distance = (
                    entry_price - planned_stop if side == _LONG else planned_stop - entry_price
                )
                if distance > _ZERO:
                    initial_risk = distance
        if initial_risk is None:
            # Position opened without a matching plan (manual, restored, or filled late):
            # assume the minimum structural risk so R-multiples stay meaningful.
            initial_risk = reading.atr * self.min_stop_atr

        # The first candle seen with the position open was traded through while holding
        # it (the entry filled on the previous close or at this candle's open), so its
        # extreme counts and it is the first bar held.
        best = max(entry_price, reading.high) if side == _LONG else min(entry_price, reading.low)
        return {
            "side": side,
            "entry_price": str(entry_price),
            "initial_risk": str(initial_risk),
            "best_price": str(best),
            "bars_held": 1,
            "last_close_time_ms": reading.close_time_ms,
        }

    def _evaluate_exit(
        self,
        trade: dict[str, Any],
        reading: PrimaryReading,
        regime: TrendRegime,
    ) -> TradeManagement:
        side = str(trade["side"])
        entry_price = to_decimal(trade["entry_price"])
        initial_risk = to_decimal(trade["initial_risk"])
        best_price = to_decimal(trade["best_price"])
        bars_held = int(trade.get("bars_held", 0) or 0)
        long = side == _LONG

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

        exit_reason = ""
        if trail_price is not None and (
            reading.close <= trail_price if long else reading.close >= trail_price
        ):
            exit_reason = "chandelier trail crossed on close"
        elif self.exit_on_trend_break and regime.breaks(side):
            exit_reason = "higher-timeframe trend broke its slow EMA"
        elif (
            self.max_bars_without_progress > 0
            and bars_held >= self.max_bars_without_progress
            and current_r < self.progress_r
        ):
            exit_reason = "time stop: trade did not progress"

        return TradeManagement(
            side=side,
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
        if close_time_ms - created > self._primary_duration_ms * _PENDING_ENTRY_MAX_CANDLES:
            state.pop(_KEY_PENDING, None)

    def _cooling_down(self, state: dict[str, Any], close_time_ms: int) -> bool:
        if self.cooldown_bars <= 0:
            return False
        last_exit = int(state.get(_KEY_LAST_EXIT, 0) or 0)
        if last_exit <= 0:
            return False
        return close_time_ms - last_exit < self._primary_duration_ms * self.cooldown_bars

    # ------------------------------------------------------------------
    # Indicators
    # ------------------------------------------------------------------

    def _primary_reading(self, candles: Sequence[Any]) -> PrimaryReading | None:
        window = list(candles[-self.primary_history :])
        if len(window) < self.primary_history:
            return None

        closes = [to_decimal(candle.close) for candle in window]
        value_series = _ema_series(closes, self.value_ema_period)
        structure_series = _ema_series(closes, self.structure_ema_period)
        value_ema = value_series[-1]
        structure_ema = structure_series[-1]
        if value_ema is None or structure_ema is None:
            return None

        atr = average_true_range(window, self.atr_period)
        if atr is None or atr <= _ZERO:
            return None

        current = window[-1]
        high = to_decimal(current.high)
        low = to_decimal(current.low)
        if high <= low:
            return None

        # Pullback window: the ``pullback_lookback`` candles before the trigger plus the
        # trigger itself (a candle that dips into value and closes above the prior highs
        # is the pullback and the resumption at once).
        touched_below = False
        touched_above = False
        start = len(window) - 1 - self.pullback_lookback
        for index in range(start, len(window)):
            ema_at = value_series[index]
            if ema_at is None:
                continue
            candle = window[index]
            if to_decimal(candle.low) <= ema_at:
                touched_below = True
            if to_decimal(candle.high) >= ema_at:
                touched_above = True

        structure_window = window[start:]
        pullback_low = min(to_decimal(candle.low) for candle in structure_window)
        pullback_high = max(to_decimal(candle.high) for candle in structure_window)

        trigger_window = window[-(self.trigger_lookback + 1) : -1]
        trigger_high = max(to_decimal(candle.high) for candle in trigger_window)
        trigger_low = min(to_decimal(candle.low) for candle in trigger_window)

        volume = relative_volume(window, self.volume_period, required_multiplier=_ONE)
        ratio = volume.ratio if volume is not None else None

        return PrimaryReading(
            open=to_decimal(current.open),
            high=high,
            low=low,
            close=closes[-1],
            close_time_ms=int(getattr(current, "close_time_ms", 0) or 0),
            atr=atr,
            value_ema=value_ema,
            structure_ema=structure_ema,
            relative_volume_ratio=ratio,
            touched_value_below=touched_below,
            touched_value_above=touched_above,
            pullback_low=pullback_low,
            pullback_high=pullback_high,
            trigger_high=trigger_high,
            trigger_low=trigger_low,
        )

    def _trend_regime(self, candles: Sequence[Any]) -> TrendRegime | None:
        # A still-forming higher-timeframe candle leaks future information into
        # backtests and makes live signals flicker; only closed candles count.
        tail = candles[-(self.higher_timeframe_history + 1) :]
        closed = [candle for candle in tail if bool(getattr(candle, "is_closed", True))]
        window = closed[-self.higher_timeframe_history :]
        if len(window) < self.higher_timeframe_history:
            return None

        closes = [to_decimal(candle.close) for candle in window]
        fast_series = _ema_series(closes, self.htf_fast_ema_period)
        slow_series = _ema_series(closes, self.htf_slow_ema_period)
        fast_ema = fast_series[-1]
        previous_fast_ema = fast_series[-2]
        slow_ema = slow_series[-1]
        if fast_ema is None or previous_fast_ema is None or slow_ema is None:
            return None

        dmi = directional_movement_index(window, self.htf_adx_period)
        if dmi is None:
            return None

        base = closes[-1 - self.htf_momentum_lookback]
        momentum_return = closes[-1] / base - _ONE if base > _ZERO else _ZERO

        return TrendRegime(
            close=closes[-1],
            fast_ema=fast_ema,
            previous_fast_ema=previous_fast_ema,
            slow_ema=slow_ema,
            adx=dmi.adx,
            plus_di=dmi.plus_di,
            minus_di=dmi.minus_di,
            momentum_return=momentum_return,
            adx_threshold=self.htf_adx_threshold,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_check_seconds(self, action: str) -> float:
        if action in _ACTIVE_ACTIONS:
            return 0.0
        return self._primary_duration_ms / 1000.0

    def _waiting_reason(
        self,
        *,
        long_score: int,
        short_score: int,
        long_ready: bool,
        short_ready: bool,
        cooling_down: bool,
        already_signalled: bool,
        planned_exit: PlannedExit | None,
        regime: TrendRegime,
    ) -> str:
        if long_ready or short_ready:
            if cooling_down:
                return "Trend pullback setup ignored: cooling down after the last exit"
            if already_signalled:
                return "Trend pullback setup ignored: this candle already produced an entry"
            if planned_exit is None:
                return "Trend pullback setup rejected: invalid planned exit"
            return (
                "Trend pullback setup rejected: liquid reward/risk "
                f"{planned_exit.liquid_reward_risk:.4f} is below "
                f"{self.minimum_liquid_reward_risk:.4f}"
            )
        return (
            "Trend pullback waiting: "
            f"long_score={long_score}, short_score={short_score}, "
            f"htf_bullish={regime.bullish}, htf_bearish={regime.bearish}, "
            f"htf_adx={regime.adx:.2f}, htf_momentum={regime.momentum_return:.4f}"
        )

    def _base_metadata(self, reading: PrimaryReading, regime: TrendRegime) -> dict[str, Any]:
        return {
            "source": "FIXED_TREND_PULLBACK_CONTINUATION",
            "close": str(reading.close),
            "atr": str(reading.atr),
            "value_ema": str(reading.value_ema),
            "structure_ema": str(reading.structure_ema),
            "pullback_low": str(reading.pullback_low),
            "pullback_high": str(reading.pullback_high),
            "trigger_high": str(reading.trigger_high),
            "trigger_low": str(reading.trigger_low),
            "trigger_range_atr": str(reading.range_atr),
            "relative_volume_ratio": (
                str(reading.relative_volume_ratio)
                if reading.relative_volume_ratio is not None
                else None
            ),
            "htf_close": str(regime.close),
            "htf_fast_ema": str(regime.fast_ema),
            "htf_slow_ema": str(regime.slow_ema),
            "htf_adx": str(regime.adx),
            "htf_plus_di": str(regime.plus_di),
            "htf_minus_di": str(regime.minus_di),
            "htf_momentum_return": str(regime.momentum_return),
            "htf_bullish": regime.bullish,
            "htf_bearish": regime.bearish,
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
                reason=f"Trend pullback: {detail}",
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


def _ema_series(values: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """EMA at every index, SMA-seeded like ``indicators.moving_average``.

    The last element equals ``exponential_moving_average(values, period)``; earlier
    elements let the pullback check compare each candle with the EMA of its own time.
    """

    result: list[Decimal | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    multiplier = _TWO / Decimal(period + 1)
    ema = sum(values[:period], _ZERO) / Decimal(period)
    result[period - 1] = ema
    for index in range(period, len(values)):
        ema = (values[index] - ema) * multiplier + ema
        result[index] = ema
    return result


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


def _condition_score(conditions: dict[str, bool]) -> int:
    weights = {
        "htf_trend": 25,
        "primary_alignment": 10,
        "pullback_to_value": 15,
        "resumption_trigger": 20,
        "volume": 10,
        "not_climax": 5,
        "stop_within_limit": 10,
        "cost_efficient": 5,
    }
    return sum(weights[name] for name, passed in conditions.items() if passed)


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
    "PrimaryReading",
    "TradeManagement",
    "TrendPullbackContinuationStrategy",
    "TrendRegime",
]
