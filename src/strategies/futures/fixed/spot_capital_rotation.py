from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from domain.models import (
    FuturesPosition,
    MarketSnapshot,
    PositionDirection,
    TradeAction,
    TradeDecision,
)
from domain.strategy_requirements import (
    EVALUATION_EVERY_CYCLE,
    StrategyDataRequirements,
)
from scanner.signal_score import SignalPriority, SignalScore, priority_from_score
from strategies.futures.base import FuturesStrategyV2, StrategyMetadata
from trading.intent import TradeIntent


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (max(1, period) + 1.0)
    result = [float(values[0])]
    for price in values[1:]:
        result.append((float(price) * alpha) + (result[-1] * (1.0 - alpha)))
    return result


def _rsi_series(closes: list[float], period: int = 14) -> list[float]:
    if len(closes) < 2:
        return [50.0] * len(closes)
    period = max(2, min(period, len(closes) - 1))
    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, len(closes)):
        delta = closes[idx] - closes[idx - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis: list[float] = [50.0] * (period + 1)
    if avg_loss == 0.0:
        rsis[-1] = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsis[-1] = 100.0 - (100.0 / (1.0 + rs))

    for idx in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[idx]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[idx]) / period
        if avg_loss == 0.0:
            rsis.append(100.0 if avg_gain > 0 else 50.0)
        else:
            rs = avg_gain / avg_loss
            rsis.append(100.0 - (100.0 / (1.0 + rs)))
    return rsis


def _pearson_correlation(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    x_tail = xs[-n:]
    y_tail = ys[-n:]
    mean_x = sum(x_tail) / n
    mean_y = sum(y_tail) / n
    cov = sum((x_tail[i] - mean_x) * (y_tail[i] - mean_y) for i in range(n))
    var_x = sum((x_tail[i] - mean_x) ** 2 for i in range(n))
    var_y = sum((y_tail[i] - mean_y) ** 2 for i in range(n))
    denom = math.sqrt(var_x * var_y)
    if denom <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, cov / denom))


def _returns_series(closes: list[float], max_points: int = 20) -> list[float]:
    if len(closes) < 2:
        return []
    start = max(1, len(closes) - max_points)
    returns: list[float] = []
    for idx in range(start, len(closes)):
        prev = closes[idx - 1]
        if prev > 0:
            returns.append(((closes[idx] - prev) / prev) * 100.0)
    return returns


class SpotCapitalRotationStrategy(FuturesStrategyV2):
    """Intelligent multi-symbol Spot capital rotation strategy.

    Capabilities:
    1. Monitors multiple coins simultaneously (BTC, ETH, SOL, BNB, XRP, etc.).
    2. Scores trend strength, volume, momentum, and cross-asset correlation/relative strength.
    3. Detects when a profitable position shows early reversal / trend-weakening signals
       even while the price is still rising (momentum deceleration, slope flattening,
       RSI rollover, volume fading on upticks, upper wick rejection).
    4. Sells the coin before a larger portion of profit disappears.
    5. Compares available opportunities on the shared rotation board and rotates capital
       into a coin with a stronger buy signal.
    6. Stays in USDT when no coin presents a sufficiently strong opportunity.
    """

    strategy_category = "FIXED_SPOT_ROTATION"
    is_test_only = False

    metadata = StrategyMetadata(
        strategy_name="spot_capital_rotation",
        supports_scoring=True,
        supports_long=True,
        supports_short=False,
        required_history=1,
        required_timeframes=["15m"],
        required_indicators=["EMA", "RSI", "ROC", "VOLUME", "CORRELATION"],
    )

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        history: int = 50,
        ema_fast_period: int = 9,
        ema_medium_period: int = 21,
        ema_slow_period: int = 50,
        rsi_period: int = 14,
        roc_short_period: int = 3,
        roc_medium_period: int = 8,
        min_entry_score: int = 68,
        rotation_score_delta: int = 12,
        min_profit_pct_for_early_exit: float = 0.35,
        max_profit_giveback_ratio: float = 0.30,
        momentum_decay_ratio: float = 0.55,
        slope_decay_ratio: float = 0.60,
        rsi_rollover_drop: float = 3.5,
        min_weakening_signals: int = 2,
        leader_symbol: str = "BTCUSDT",
        **_ignored: Any,
    ) -> None:
        self.timeframe = str(timeframe or "15m").strip()
        self.history = max(15, int(history or 50))
        self.ema_fast_period = max(2, int(ema_fast_period or 9))
        self.ema_medium_period = max(self.ema_fast_period + 1, int(ema_medium_period or 21))
        self.ema_slow_period = max(self.ema_medium_period + 1, int(ema_slow_period or 50))
        self.rsi_period = max(2, int(rsi_period or 14))
        self.roc_short_period = max(1, int(roc_short_period or 3))
        self.roc_medium_period = max(self.roc_short_period + 1, int(roc_medium_period or 8))
        self.min_entry_score = int(min_entry_score or 68)
        self.rotation_score_delta = int(rotation_score_delta or 12)
        self.min_profit_pct_for_early_exit = float(min_profit_pct_for_early_exit)
        self.max_profit_giveback_ratio = float(max_profit_giveback_ratio)
        self.momentum_decay_ratio = float(momentum_decay_ratio)
        self.slope_decay_ratio = float(slope_decay_ratio)
        self.rsi_rollover_drop = float(rsi_rollover_drop)
        self.min_weakening_signals = max(1, int(min_weakening_signals or 2))
        self.leader_symbol = str(leader_symbol or "BTCUSDT").upper().strip()
        self._shared_board: dict[str, dict[str, Any]] = {}

    def data_requirements(self) -> StrategyDataRequirements:
        return StrategyDataRequirements(
            primary_timeframe=self.timeframe,
            required_candles={self.timeframe: self.history},
            evaluation_policy=EVALUATION_EVERY_CYCLE,
            closed_candles_only=True,
        )

    def _board(self, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        board = state.get("rotation_board")
        if isinstance(board, dict):
            return board
        state["rotation_board"] = self._shared_board
        return self._shared_board

    def _extract_series(
        self, market: MarketSnapshot
    ) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
        candles_map = getattr(market, "candles", None) or {}
        series = candles_map.get(self.timeframe)
        if not series and candles_map:
            first_tf = next(iter(candles_map.keys()))
            series = candles_map.get(first_tf)

        if series and len(series) >= 4:
            opens = [float(c.open) for c in series]
            highs = [float(c.high) for c in series]
            lows = [float(c.low) for c in series]
            closes = [float(c.close) for c in series]
            volumes = [float(c.volume) for c in series]
            live_price = float(market.price)
            if live_price > 0 and abs(live_price - closes[-1]) / max(closes[-1], 1e-9) > 1e-6:
                closes[-1] = live_price
                highs[-1] = max(highs[-1], live_price)
                lows[-1] = min(lows[-1], live_price)
            return opens, highs, lows, closes, volumes

        prices = [float(p) for p in (market.prices or []) if float(p) > 0]
        live_price = float(market.price)
        if live_price > 0 and (not prices or prices[-1] != live_price):
            prices.append(live_price)
        if not prices and live_price > 0:
            prices = [live_price]
        return list(prices), list(prices), list(prices), list(prices), []

    def _compute_metrics(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        board = self._board(state)
        opens, highs, lows, closes, volumes = self._extract_series(market)
        n = len(closes)
        current_price = closes[-1] if closes else float(market.price)
        prev_price = closes[-2] if n >= 2 else current_price

        ema_fast = _ema_series(closes, min(self.ema_fast_period, max(2, n)))
        ema_medium = _ema_series(closes, min(self.ema_medium_period, max(3, n)))
        ema_slow = _ema_series(closes, min(self.ema_slow_period, max(4, n)))
        rsis = _rsi_series(closes, min(self.rsi_period, max(2, n - 1))) if n >= 3 else [50.0]

        ef_now = ema_fast[-1] if ema_fast else current_price
        ef_prev = ema_fast[-2] if len(ema_fast) >= 2 else ef_now
        ef_prev2 = ema_fast[-3] if len(ema_fast) >= 3 else ef_prev
        em_now = ema_medium[-1] if ema_medium else current_price
        es_now = ema_slow[-1] if ema_slow else current_price

        ema_fast_slope_pct = ((ef_now - ef_prev) / ef_prev * 100.0) if ef_prev > 0 else 0.0
        prev_ema_fast_slope_pct = (
            ((ef_prev - ef_prev2) / ef_prev2 * 100.0) if ef_prev2 > 0 else ema_fast_slope_pct
        )

        short_idx = max(0, n - 1 - self.roc_short_period)
        prev_short_idx = max(0, n - 2 - self.roc_short_period)
        med_idx = max(0, n - 1 - self.roc_medium_period)

        roc_short = (
            ((current_price - closes[short_idx]) / closes[short_idx] * 100.0)
            if n >= 2 and closes[short_idx] > 0
            else 0.0
        )
        prev_roc_short = (
            ((prev_price - closes[prev_short_idx]) / closes[prev_short_idx] * 100.0)
            if n >= 3 and closes[prev_short_idx] > 0
            else roc_short
        )
        roc_medium = (
            ((current_price - closes[med_idx]) / closes[med_idx] * 100.0)
            if n >= 2 and closes[med_idx] > 0
            else roc_short
        )
        momentum_accel = roc_short - prev_roc_short

        rsi_now = rsis[-1] if rsis else 50.0
        rsi_prev = rsis[-2] if len(rsis) >= 2 else rsi_now
        rsi_peak_recent = max(rsis[-min(6, len(rsis)) :]) if rsis else rsi_now

        # 1. Trend Score (0 - 100)
        trend_aligned = current_price >= ef_now >= em_now
        full_stack = ef_now >= em_now >= es_now
        trend_score = 25.0
        if current_price > ef_now:
            trend_score += 20.0
        if ef_now > em_now:
            trend_score += 20.0
        if full_stack:
            trend_score += 15.0
        trend_score += _clamp(ema_fast_slope_pct * 35.0, -25.0, 25.0)
        trend_score = _clamp(trend_score, 0.0, 100.0)

        # 2. Momentum Score (0 - 100)
        momentum_score = 30.0
        momentum_score += _clamp(roc_short * 16.0, -25.0, 30.0)
        momentum_score += _clamp(roc_medium * 8.0, -15.0, 25.0)
        momentum_score += _clamp(momentum_accel * 12.0, -15.0, 15.0)
        if 52.0 <= rsi_now <= 74.0:
            momentum_score += 12.0
        elif rsi_now > 78.0:
            momentum_score -= 10.0
        elif rsi_now < 45.0:
            momentum_score -= 15.0
        momentum_score = _clamp(momentum_score, 0.0, 100.0)

        # 3. Volume Score (0 - 100)
        rel_volume = 1.0
        volume_fading = False
        if volumes and len(volumes) >= 4 and max(volumes) > 0:
            vol_window = volumes[-min(20, len(volumes)) : -1] or volumes[:-1]
            avg_vol = sum(vol_window) / max(1, len(vol_window))
            rel_volume = (volumes[-1] / avg_vol) if avg_vol > 0 else 1.0
            volume_fading = (
                len(volumes) >= 3
                and volumes[-1] < volumes[-2] < volumes[-3]
                and current_price >= prev_price
            ) or (rel_volume < 0.82 and current_price >= prev_price)
            volume_score = _clamp(45.0 + (rel_volume - 1.0) * 35.0, 10.0, 100.0)
            if volume_fading:
                volume_score = max(10.0, volume_score - 18.0)
        else:
            # Fallback to price impulse consistency when only tick prices are present
            up_steps = sum(
                1 for i in range(max(1, n - 6), n) if closes[i] > closes[i - 1]
            )
            total_steps = max(1, min(6, n - 1))
            up_ratio = up_steps / total_steps
            volume_score = _clamp(35.0 + up_ratio * 50.0, 15.0, 90.0)

        # Upper wick rejection ratio (for OHLC candles)
        upper_wick_ratio = 0.0
        if highs and lows and opens and len(highs) == len(closes):
            candle_range = highs[-1] - lows[-1]
            if candle_range > 0:
                body_top = max(opens[-1], closes[-1])
                upper_wick_ratio = max(0.0, (highs[-1] - body_top) / candle_range)

        # 4. Correlation & Relative Strength Score (0 - 100)
        returns = _returns_series(closes, max_points=20)
        leader_entry = board.get(self.leader_symbol) if symbol != self.leader_symbol else None
        if leader_entry is None and board:
            for other_sym, other_data in board.items():
                if other_sym != symbol and isinstance(other_data, dict):
                    leader_entry = other_data
                    break

        corr_with_leader = 0.0
        relative_alpha = roc_medium
        if isinstance(leader_entry, dict):
            leader_returns = list(leader_entry.get("returns") or [])
            if returns and leader_returns:
                corr_with_leader = _pearson_correlation(returns, leader_returns)
            leader_roc_medium = float(leader_entry.get("roc_medium", 0.0) or 0.0)
            relative_alpha = roc_medium - leader_roc_medium

        correlation_score = 50.0 + _clamp(relative_alpha * 10.0, -30.0, 35.0)
        # Healthy moderate correlation or positive alpha decoupling boosts rotation score
        if relative_alpha > 0 and 0.15 <= corr_with_leader <= 0.88:
            correlation_score += 10.0
        elif corr_with_leader > 0.95 and relative_alpha < 0:
            correlation_score -= 12.0
        correlation_score = _clamp(correlation_score, 0.0, 100.0)

        # 5. Detect Trend Weakening While Still Rising
        price_still_rising = current_price >= prev_price or roc_short > 0.0
        weakening_signals: list[str] = []

        if price_still_rising:
            if (
                prev_roc_short > 0.12
                and roc_short < (prev_roc_short * self.momentum_decay_ratio)
            ):
                weakening_signals.append("MOMENTUM_DECELERATING_ON_RISE")
            if (
                prev_ema_fast_slope_pct > 0.03
                and ema_fast_slope_pct < (prev_ema_fast_slope_pct * self.slope_decay_ratio)
            ):
                weakening_signals.append("EMA_SLOPE_FLATTENING_ON_RISE")
            if (
                rsi_peak_recent >= 62.0
                and (rsi_peak_recent - rsi_now) >= self.rsi_rollover_drop
            ):
                weakening_signals.append("RSI_DIVERGENCE_ROLLOVER")
            if volume_fading:
                weakening_signals.append("VOLUME_FADING_ON_UPTICK")
            if upper_wick_ratio >= 0.45:
                weakening_signals.append("UPPER_WICK_REJECTION")

        # Track position peak profit & peak momentum in per-symbol state
        pos_tracker = state.setdefault("position_tracker", {})
        pnl_pct = 0.0
        giveback_ratio = 0.0
        if position.is_open and position.entry_price > 0:
            entry_price = float(position.entry_price)
            if position.direction is PositionDirection.SHORT:
                pnl_pct = ((entry_price - current_price) / entry_price) * 100.0
            else:
                pnl_pct = ((current_price - entry_price) / entry_price) * 100.0

            tracked_entry = float(pos_tracker.get("entry_price", 0.0) or 0.0)
            if abs(tracked_entry - entry_price) > 1e-8:
                pos_tracker.clear()
                pos_tracker["entry_price"] = entry_price
                pos_tracker["peak_price"] = max(entry_price, current_price)
                pos_tracker["peak_pnl_pct"] = max(0.0, pnl_pct)
                pos_tracker["peak_roc_short"] = max(0.0, roc_short)
            else:
                pos_tracker["peak_price"] = max(
                    float(pos_tracker.get("peak_price", current_price) or current_price),
                    current_price,
                )
                pos_tracker["peak_pnl_pct"] = max(
                    float(pos_tracker.get("peak_pnl_pct", 0.0) or 0.0),
                    pnl_pct,
                )
                pos_tracker["peak_roc_short"] = max(
                    float(pos_tracker.get("peak_roc_short", 0.0) or 0.0),
                    roc_short,
                )

            peak_roc_short = float(pos_tracker.get("peak_roc_short", 0.0) or 0.0)
            if (
                price_still_rising
                and peak_roc_short > 0.20
                and roc_short < (peak_roc_short * self.momentum_decay_ratio)
                and "MOMENTUM_DECELERATING_ON_RISE" not in weakening_signals
            ):
                weakening_signals.append("PEAK_MOMENTUM_DECELERATION")

            peak_pnl_pct = float(pos_tracker.get("peak_pnl_pct", 0.0) or 0.0)
            if peak_pnl_pct >= self.min_profit_pct_for_early_exit and peak_pnl_pct > 0:
                giveback_ratio = max(0.0, (peak_pnl_pct - pnl_pct) / peak_pnl_pct)
        else:
            pos_tracker.clear()

        trend_weakening_while_rising = (
            price_still_rising and len(weakening_signals) >= self.min_weakening_signals
        )
        profit_reversal_detected = (
            position.is_open
            and pnl_pct > 0.0
            and (
                giveback_ratio >= self.max_profit_giveback_ratio
                or (
                    pnl_pct >= self.min_profit_pct_for_early_exit
                    and current_price < ef_now
                    and roc_short < 0.0
                )
            )
        )

        # 6. Composite Opportunity Score (0 - 100)
        composite = (
            (trend_score * 0.35)
            + (momentum_score * 0.30)
            + (volume_score * 0.20)
            + (correlation_score * 0.15)
        )
        if not trend_aligned or roc_short <= 0.0:
            composite = min(composite, float(self.min_entry_score - 5))
        if trend_weakening_while_rising:
            composite = max(0.0, composite - 18.0)

        composite_score = int(round(_clamp(composite, 0.0, 100.0)))
        qualifies_for_entry = (
            composite_score >= self.min_entry_score
            and trend_aligned
            and roc_short > 0.0
            and not trend_weakening_while_rising
        )

        metrics = {
            "symbol": symbol,
            "price": current_price,
            "score": composite_score,
            "trend_score": round(trend_score, 2),
            "momentum_score": round(momentum_score, 2),
            "volume_score": round(volume_score, 2),
            "correlation_score": round(correlation_score, 2),
            "corr_with_leader": round(corr_with_leader, 4),
            "relative_alpha": round(relative_alpha, 4),
            "roc_short": round(roc_short, 4),
            "prev_roc_short": round(prev_roc_short, 4),
            "roc_medium": round(roc_medium, 4),
            "momentum_accel": round(momentum_accel, 4),
            "ema_fast": round(ef_now, 6),
            "ema_medium": round(em_now, 6),
            "ema_slow": round(es_now, 6),
            "ema_fast_slope_pct": round(ema_fast_slope_pct, 4),
            "prev_ema_fast_slope_pct": round(prev_ema_fast_slope_pct, 4),
            "rsi": round(rsi_now, 2),
            "rsi_prev": round(rsi_prev, 2),
            "rsi_peak_recent": round(rsi_peak_recent, 2),
            "rel_volume": round(rel_volume, 3),
            "upper_wick_ratio": round(upper_wick_ratio, 3),
            "price_still_rising": price_still_rising,
            "weakening_signals": weakening_signals,
            "trend_weakening_while_rising": trend_weakening_while_rising,
            "profit_reversal_detected": profit_reversal_detected,
            "pnl_pct": round(pnl_pct, 4),
            "peak_pnl_pct": round(float(pos_tracker.get("peak_pnl_pct", 0.0) or 0.0), 4),
            "giveback_ratio": round(giveback_ratio, 4),
            "qualifies_for_entry": qualifies_for_entry,
            "returns": returns,
        }
        board[symbol] = metrics
        state["last_rotation_metrics"] = metrics
        return metrics

    def _best_opportunity(
        self, board: dict[str, dict[str, Any]], *, exclude_symbol: str | None = None
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for sym, data in board.items():
            if exclude_symbol and sym == exclude_symbol:
                continue
            if not isinstance(data, dict):
                continue
            if not bool(data.get("qualifies_for_entry", False)):
                continue
            candidates.append(data)
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                int(item.get("score", 0) or 0),
                float(item.get("relative_alpha", 0.0) or 0.0),
                float(item.get("momentum_score", 0.0) or 0.0),
            ),
            reverse=True,
        )
        return candidates[0]

    def score_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> SignalScore:
        metrics = self._compute_metrics(symbol, market, position, state)
        board = self._board(state)
        best_other = self._best_opportunity(board, exclude_symbol=symbol)
        best_overall = self._best_opportunity(board)

        score_val = int(metrics["score"])
        action = TradeAction.HOLD.value
        reason = "STAY_IN_USDT_NO_STRONG_OPPORTUNITY"
        priority = SignalPriority.COLD

        if position.is_open:
            priority = SignalPriority.READY
            if (
                metrics["pnl_pct"] >= self.min_profit_pct_for_early_exit
                and metrics["trend_weakening_while_rising"]
            ):
                action = TradeAction.CLOSE_POSITION.value
                signals_str = ",".join(metrics["weakening_signals"])
                reason = f"EARLY_PROFIT_LOCK_TREND_WEAKENING_WHILE_RISING({signals_str})"
                score_val = max(score_val, 90)
            elif metrics["profit_reversal_detected"]:
                action = TradeAction.CLOSE_POSITION.value
                reason = "PROFIT_PROTECTION_REVERSAL_DETECTED"
                score_val = max(score_val, 88)
            elif (
                best_other is not None
                and int(best_other["score"]) >= ( int(metrics["score"]) + self.rotation_score_delta )
            ):
                action = TradeAction.CLOSE_POSITION.value
                target_sym = str(best_other["symbol"])
                reason = f"CAPITAL_ROTATION_TO_{target_sym}"
                score_val = max(score_val, int(best_other["score"]), 85)
            else:
                action = TradeAction.HOLD.value
                reason = "HOLD_STRONG_OPEN_POSITION"
                priority = SignalPriority.HOT
        else:
            if metrics["qualifies_for_entry"]:
                if best_overall is None or str(best_overall.get("symbol")) == symbol:
                    action = TradeAction.OPEN_LONG.value
                    reason = "STRONGEST_ROTATION_OPPORTUNITY_BUY"
                    priority = SignalPriority.READY if score_val >= 85 else SignalPriority.HOT
                    score_val = max(score_val, 75)
                else:
                    leader_sym = str(best_overall.get("symbol"))
                    action = TradeAction.HOLD.value
                    reason = f"DEFER_TO_STRONGER_OPPORTUNITY_{leader_sym}"
                    priority = SignalPriority.WARM
            else:
                action = TradeAction.HOLD.value
                reason = "STAY_IN_USDT_WEAK_OPPORTUNITY"
                priority = SignalPriority.COLD

        priority = priority_from_score(score_val)
        return SignalScore(
            symbol=symbol,
            score=score_val,
            priority=priority,
            reason=reason,
            suggested_next_check_seconds=(
                1.0 if priority in {SignalPriority.READY, SignalPriority.HOT} else 5.0
            ),
            data_age_seconds=0.0,
            action=action,
            confidence=score_val,
            strategy=self.metadata.strategy_name,
            metadata={
                k: v for k, v in metrics.items() if k != "returns"
            },
        )

    def decide_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> TradeDecision:
        metrics = self._compute_metrics(symbol, market, position, state)
        board = self._board(state)
        best_other = self._best_opportunity(board, exclude_symbol=symbol)
        best_overall = self._best_opportunity(board)

        if position.is_open:
            if (
                metrics["pnl_pct"] >= self.min_profit_pct_for_early_exit
                and metrics["trend_weakening_while_rising"]
            ):
                signals_str = ",".join(metrics["weakening_signals"])
                return TradeDecision(
                    action=TradeAction.CLOSE_POSITION,
                    reason=f"EARLY_PROFIT_LOCK_TREND_WEAKENING_WHILE_RISING({signals_str})",
                )
            if metrics["profit_reversal_detected"]:
                return TradeDecision(
                    action=TradeAction.CLOSE_POSITION,
                    reason="PROFIT_PROTECTION_REVERSAL_DETECTED",
                )
            if (
                best_other is not None
                and int(best_other["score"]) >= (int(metrics["score"]) + self.rotation_score_delta)
            ):
                target_sym = str(best_other["symbol"])
                return TradeDecision(
                    action=TradeAction.CLOSE_POSITION,
                    reason=f"CAPITAL_ROTATION_TO_{target_sym}",
                )
            return TradeDecision(
                action=TradeAction.HOLD,
                reason="HOLD_STRONG_OPEN_POSITION",
            )

        if metrics["qualifies_for_entry"]:
            if best_overall is None or str(best_overall.get("symbol")) == symbol:
                return TradeDecision(
                    action=TradeAction.OPEN_LONG,
                    reason="STRONGEST_ROTATION_OPPORTUNITY_BUY",
                )
            leader_sym = str(best_overall.get("symbol"))
            return TradeDecision(
                action=TradeAction.HOLD,
                reason=f"DEFER_TO_STRONGER_OPPORTUNITY_{leader_sym}",
            )

        return TradeDecision(
            action=TradeAction.HOLD,
            reason="STAY_IN_USDT_NO_STRONG_OPPORTUNITY",
        )

    def intent_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
        score: SignalScore | None = None,
    ) -> TradeIntent:
        decision = self.decide_symbol(symbol, market, position, state)
        metrics = state.get("last_rotation_metrics") or self._compute_metrics(
            symbol, market, position, state
        )
        confidence = int(score.score) if score is not None else int(metrics.get("score", 0))
        if decision.action is TradeAction.CLOSE_POSITION:
            confidence = max(confidence, 90)
        return TradeIntent(
            symbol=symbol,
            action=decision.action.value,
            confidence=confidence,
            score=confidence,
            reason=decision.reason,
            strategy_name=self.metadata.strategy_name,
            market_price=Decimal(str(market.price)),
            metadata={k: v for k, v in metrics.items() if k != "returns"},
        )
