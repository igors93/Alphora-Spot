from __future__ import annotations

import time
from typing import Any

from domain.models import FuturesPosition, MarketSnapshot, TradeDecision
from scanner.signal_score import SignalScore
from strategies.futures.base import FuturesStrategyV2, StrategyMetadata
from trading_cadence import MIN_TEST_SIGNAL_INTERVAL_SECONDS


class ExecutionProbeStrategy(FuturesStrategyV2):
    """Deterministic probe strategy that cycles through four steps.

    Step sequence (cycles indefinitely):
        flat  → OPEN_LONG
        LONG  → CLOSE_POSITION
        flat  → OPEN_SHORT
        SHORT → CLOSE_POSITION

    The first signal fires immediately (no initial wait).  Subsequent signals
    obey ``signal_interval_seconds``.  No randomness; no coupling to the
    Binance client.
    """

    strategy_category = "FIXED_TEST"

    metadata = StrategyMetadata(
        strategy_name="ExecutionProbeStrategy",
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

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> ExecutionProbeStrategy:
        return cls(
            signal_interval_seconds=float(params.get("signal_interval_seconds", 120.0)),
            ready_score=int(params.get("ready_score", 95)),
            idle_score=int(params.get("idle_score", 20)),
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
        last_signal_at = _optional_float(state.get("probe_last_signal_at"))
        if last_signal_at is None:
            due = True
        else:
            elapsed = now - last_signal_at
            due = elapsed >= self.signal_interval_seconds

        if not due:
            assert last_signal_at is not None
            remaining = max(0.0, self.signal_interval_seconds - (now - last_signal_at))
            sequence = int(state.get("probe_sequence", 0))
            next_action = str(state.get("probe_next_flat_action", "OPEN_LONG"))
            return self._remember(
                state,
                SignalScore(
                    symbol=symbol,
                    score=self.idle_score,
                    reason=(
                        f"ExecutionProbe aguardando: {remaining:.1f}s restantes (seq={sequence})"
                    ),
                    action="NO_ACTION",
                    confidence=self.idle_score,
                    suggested_next_check_seconds=remaining,
                    strategy=self.__class__.__name__,
                    metadata={
                        "source": "FIXED_EXECUTION_PROBE",
                        "signal_interval_seconds": self.signal_interval_seconds,
                        "seconds_until_next_signal": remaining,
                        "probe_sequence": sequence,
                        "next_flat_action": next_action,
                    },
                ),
            )

        action = _probe_action(position, state)
        sequence = int(state.get("probe_sequence", 0)) + 1
        state["probe_sequence"] = sequence
        state["probe_last_signal_at"] = now

        if action in {"OPEN_LONG", "OPEN_SHORT"}:
            state["probe_next_flat_action"] = "OPEN_SHORT" if action == "OPEN_LONG" else "OPEN_LONG"

        return self._remember(
            state,
            SignalScore(
                symbol=symbol,
                score=self.ready_score,
                reason=(f"ExecutionProbe READY: acao={action} seq={sequence}"),
                action=action,
                confidence=self.ready_score,
                suggested_next_check_seconds=self.signal_interval_seconds,
                strategy=self.__class__.__name__,
                metadata={
                    "source": "FIXED_EXECUTION_PROBE",
                    "signal_interval_seconds": self.signal_interval_seconds,
                    "probe_sequence": sequence,
                    "probe_action": action,
                    "position_direction": (
                        position.direction.value if position.direction is not None else "NONE"
                    ),
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
        reason = str(state.get("last_score_reason") or "ExecutionProbe sem sinal pronto")

        if action == "OPEN_LONG" and not position.is_open:
            return TradeDecision.open_long(reason)
        if action == "OPEN_SHORT" and not position.is_open:
            return TradeDecision.open_short(reason)
        if action == "CLOSE_POSITION" and position.is_open:
            return TradeDecision.close(reason)

        return TradeDecision.hold(reason)

    def _remember(self, state: dict[str, Any], score: SignalScore) -> SignalScore:
        state["last_score_action"] = score.action or ""
        state["last_score_reason"] = score.reason
        state["last_score_priority"] = score.priority.value if score.priority else ""
        state["last_score"] = int(score.score)
        return score


def _probe_action(position: FuturesPosition, state: dict[str, Any]) -> str:
    if position.is_open:
        return "CLOSE_POSITION"
    return str(state.get("probe_next_flat_action", "OPEN_LONG"))


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
