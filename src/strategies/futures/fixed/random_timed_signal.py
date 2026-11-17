from __future__ import annotations

import random
import time
from typing import Any

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeDecision
from scanner.signal_score import SignalScore
from strategies.futures.base import FuturesStrategyV2, StrategyMetadata
from trading_cadence import MIN_TEST_SIGNAL_INTERVAL_SECONDS


class RandomTimedSignalStrategy(FuturesStrategyV2):
    """Test strategy that emits a random LONG or SHORT signal on a fixed timer."""

    strategy_category = "FIXED_TEST"

    metadata = StrategyMetadata(
        strategy_name="RandomTimedSignalStrategy",
        supports_scoring=True,
        supports_long=True,
        supports_short=True,
        required_history=1,
        required_timeframes=["1m"],
        required_indicators=[],
    )

    def __init__(
        self,
        *,
        signal_interval_seconds: float = 120.0,
        ready_score: int = 95,
        idle_score: int = 20,
        seed: int | str | None = None,
    ):
        if not signal_interval_seconds >= MIN_TEST_SIGNAL_INTERVAL_SECONDS:
            raise ValueError(
                "signal_interval_seconds precisa ser no minimo "
                f"{MIN_TEST_SIGNAL_INTERVAL_SECONDS:g}s: o Alphora nao opera alta frequencia"
            )
        if not 90 <= int(ready_score) <= 100:
            raise ValueError("ready_score precisa ficar entre 90 e 100")
        if not 0 <= int(idle_score) <= 39:
            raise ValueError("idle_score precisa ficar entre 0 e 39")

        self.signal_interval_seconds = float(signal_interval_seconds)
        self.ready_score = int(ready_score)
        self.idle_score = int(idle_score)
        self._rng = random.Random(seed)

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> RandomTimedSignalStrategy:
        return cls(
            signal_interval_seconds=float(params.get("signal_interval_seconds", 120.0)),
            ready_score=int(params.get("ready_score", 95)),
            idle_score=int(params.get("idle_score", 20)),
            seed=params.get("seed"),
        )

    def score_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> SignalScore:
        symbol = symbol.upper().strip()
        now = _current_time(market=market, state=state)
        last_signal_at = _optional_float(state.get("last_random_timed_signal_at"))
        due = last_signal_at is None or now - last_signal_at >= self.signal_interval_seconds

        if last_signal_at is not None and not due:
            remaining = max(0.0, self.signal_interval_seconds - (now - last_signal_at))
            return self._remember(
                state,
                SignalScore(
                    symbol=symbol,
                    score=self.idle_score,
                    reason=(
                        f"RandomTimedSignal aguardando proximo sinal: {remaining:.1f}s restantes"
                    ),
                    action="NO_ACTION",
                    confidence=self.idle_score,
                    suggested_next_check_seconds=remaining,
                    strategy=self.__class__.__name__,
                    metadata={
                        "source": "FIXED_RANDOM_TIMED_TEST",
                        "signal_interval_seconds": self.signal_interval_seconds,
                        "seconds_until_next_signal": remaining,
                    },
                ),
            )

        action = self._rng.choice(("OPEN_LONG", "OPEN_SHORT"))
        direction = "LONG" if action == "OPEN_LONG" else "SHORT"
        sequence = int(state.get("random_timed_signal_sequence", 0)) + 1
        state["random_timed_signal_sequence"] = sequence
        state["last_random_timed_signal_at"] = now

        return self._remember(
            state,
            SignalScore(
                symbol=symbol,
                score=self.ready_score,
                reason=(
                    "RandomTimedSignal READY: "
                    f"sinal aleatorio {direction} #{sequence} a cada "
                    f"{self.signal_interval_seconds:.0f}s"
                ),
                action=action,
                confidence=self.ready_score,
                suggested_next_check_seconds=self.signal_interval_seconds,
                strategy=self.__class__.__name__,
                metadata={
                    "source": "FIXED_RANDOM_TIMED_TEST",
                    "signal_interval_seconds": self.signal_interval_seconds,
                    "signal_sequence": sequence,
                    "direction": direction,
                },
            ),
        )

    def decide_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> TradeDecision:
        action = str(state.get("last_score_action") or "").upper().strip()
        reason = str(state.get("last_score_reason") or "RandomTimedSignal sem sinal pronto")

        if action not in {"OPEN_LONG", "OPEN_SHORT"}:
            return TradeDecision.hold(reason)

        if not position.is_open:
            if action == "OPEN_LONG":
                return TradeDecision.open_long(reason)
            return TradeDecision.open_short(reason)

        if position.direction is PositionDirection.LONG and action == "OPEN_SHORT":
            return TradeDecision.close(f"{reason} | fechando LONG para testar reversao")
        if position.direction is PositionDirection.SHORT and action == "OPEN_LONG":
            return TradeDecision.close(f"{reason} | fechando SHORT para testar reversao")

        return TradeDecision.hold(f"{reason} | posicao ja esta na direcao sorteada")

    def _remember(self, state: dict[str, Any], score: SignalScore) -> SignalScore:
        state["last_score_action"] = score.action or ""
        state["last_score_reason"] = score.reason
        state["last_score_priority"] = score.priority.value if score.priority else ""
        state["last_score"] = int(score.score)
        return score


def _current_time(*, market: MarketSnapshot, state: dict[str, Any]) -> float:
    indicators = market.indicators if isinstance(market.indicators, dict) else {}
    candidates = (
        state.get("now"),
        state.get("clock"),
        indicators.get("timestamp"),
        indicators.get("event_timestamp"),
        indicators.get("market_timestamp"),
    )
    for candidate in candidates:
        value = _optional_float(candidate)
        if value is not None:
            return value
    return time.time()


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
