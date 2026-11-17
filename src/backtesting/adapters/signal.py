"""Historical market data port for the Governor's strategy and regime rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from backtesting.adapters.decision_stages import HistoricalClock
from backtesting.domain.config import CandleEvent
from backtesting.engine.execution import ExecutionSimulator
from backtesting.engine.strategy_runtime import BacktestStrategyRuntime
from config.regime_policy_modes import normalize_regime_policy_mode
from domain.models import MarketSnapshot, TradeAction
from futures.trend import MarketRegime, TrendContext, TrendContextEngine, TrendDirection
from futures.trend.regime_policy import evaluate_regime_policy
from scanner.signal_score import SignalScore


@dataclass(frozen=True)
class HistoricalSignal:
    score: SignalScore
    action: TradeAction
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


class HistoricalSignalPort:
    """Feeds closed candles into the same strategy and regime contracts as live."""

    def __init__(
        self,
        *,
        runtime: BacktestStrategyRuntime,
        execution: ExecutionSimulator,
        clock: HistoricalClock,
        trend_engine: TrendContextEngine,
        trend_enabled: bool,
        trend_mode: str,
        price_window: int,
        trend_timeframe: str = "",
    ) -> None:
        self.runtime = runtime
        self.trend_timeframe = str(trend_timeframe or "").strip()
        self.execution = execution
        self.clock = clock
        self.trend_engine = trend_engine
        self.trend_enabled = trend_enabled
        self.trend_mode = normalize_regime_policy_mode(trend_mode)
        self.price_window = price_window

    def evaluate(
        self,
        event: CandleEvent,
        *,
        prices_by_symbol: dict[str, list[Decimal]],
        closed_intrabar: bool,
    ) -> HistoricalSignal:
        prices = prices_by_symbol.setdefault(event.symbol, [])
        if self.clock.signal_timing_policy == "ON_CLOSE":
            prices.append(Decimal(str(event.candle.close)))
            if len(prices) > self.price_window:
                del prices[: -self.price_window]
        if closed_intrabar:
            reason = "intrabar exit already processed"
            return HistoricalSignal(
                score=SignalScore(
                    symbol=event.symbol,
                    score=0,
                    action=TradeAction.HOLD.value,
                    reason=reason,
                ),
                action=TradeAction.HOLD,
                reason=reason,
                metadata={"intrabar_exit_same_candle_reentry_blocked": True},
            )

        trend_context = self._trend_context(event)
        indicators: dict[str, Any] = {"timestamp": event.timestamp}
        if trend_context is not None:
            indicators["trend_context"] = trend_context
        market = MarketSnapshot(
            symbol=event.symbol,
            price=self.clock.price(event),
            prices=list(prices[-self.price_window :]),
            indicators=indicators,
            candles=event.candle_snapshot or None,
        )
        position = self.execution.current_position(event.symbol)
        evaluation = self.runtime.evaluate(event.symbol, market, position)
        score = evaluation.score
        decision = evaluation.decision
        policy = evaluate_regime_policy(
            action=decision.action,
            trend_context=trend_context,
            mode=self.trend_mode,
        )
        metadata = {
            **evaluation.metadata,
            **_trend_metadata_for_action(decision.action.value, trend_context),
            "regime_policy": policy.to_dict(),
        }
        if not policy.allowed:
            blocked_score = SignalScore(
                symbol=score.symbol,
                score=min(int(score.score), 39),
                reason=policy.reason,
                action=TradeAction.HOLD.value,
                confidence=score.confidence,
                strategy=score.strategy,
                data_age_seconds=score.data_age_seconds,
                metadata={**score.metadata, "regime_policy": policy.to_dict()},
            )
            return HistoricalSignal(
                score=blocked_score,
                action=TradeAction.HOLD,
                reason=policy.reason,
                metadata=metadata,
            )
        if decision.action not in {
            TradeAction.HOLD,
            TradeAction.OPEN_LONG,
            TradeAction.OPEN_SHORT,
            TradeAction.CLOSE_POSITION,
        }:
            raise ValueError(f"TradeAction nao tratado no backtesting: {decision.action}")
        return HistoricalSignal(score, decision.action, decision.reason, metadata)

    def _trend_context(self, event: CandleEvent) -> TrendContext | None:
        """The trend of the closed candles visible at this event, as the Governor computes it."""

        if not self.trend_enabled:
            return None
        timeframe = self.trend_timeframe or str(getattr(event.candle, "interval", "") or "")
        return self.trend_engine.evaluate_candles(
            symbol=event.symbol,
            candles=event.candle_snapshot.get(timeframe, ()),
            timestamp=event.timestamp,
        )


def _trend_metadata_for_action(action: str, trend_context: TrendContext | None) -> dict[str, Any]:
    if trend_context is None:
        return {
            "trend_alignment": "NEUTRAL_OR_UNKNOWN",
            "is_with_trend": False,
            "is_against_trend": False,
        }
    context_payload = trend_context.to_dict()
    diagnostics = context_payload.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    direction = trend_context.direction
    regime = trend_context.regime
    with_trend = False
    against_trend = False
    normalized_action = str(action or "").upper().strip()
    if normalized_action == TradeAction.OPEN_LONG.value:
        with_trend = direction is TrendDirection.UP or regime is MarketRegime.TREND_UP
        against_trend = direction is TrendDirection.DOWN or regime is MarketRegime.TREND_DOWN
    elif normalized_action == TradeAction.OPEN_SHORT.value:
        with_trend = direction is TrendDirection.DOWN or regime is MarketRegime.TREND_DOWN
        against_trend = direction is TrendDirection.UP or regime is MarketRegime.TREND_UP
    alignment = "NEUTRAL_OR_UNKNOWN"
    if with_trend:
        alignment = "WITH_TREND"
    elif against_trend:
        alignment = "AGAINST_TREND"
    return {
        "trend_context": context_payload,
        "trend_direction": context_payload["direction"],
        "trend_strength": context_payload["strength"],
        "market_regime": context_payload["regime"],
        "primary_regime": diagnostics.get("primary_regime"),
        "directional_regime": diagnostics.get("directional_regime"),
        "volatility_state": diagnostics.get("volatility_state"),
        "trend_confidence": context_payload["confidence"],
        "is_with_trend": with_trend,
        "is_against_trend": against_trend,
        "trend_alignment": alignment,
    }
