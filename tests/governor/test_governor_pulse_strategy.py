import time
from decimal import Decimal

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeDecision
from scanner.signal_score import SignalPriority, SignalScore


class PulseLikeStrategy:
    def __init__(
        self,
        *,
        min_history=2,
        hold_seconds=0,
        cooldown_seconds=0,
        side="LONG",
    ):
        self.min_history = min_history
        self.hold_seconds = hold_seconds
        self.cooldown_seconds = cooldown_seconds
        self.side = side

    def score_symbol(self, symbol, market, position, state):
        if len(market.prices) < self.min_history:
            return SignalScore(symbol=symbol, score=10, reason="fake warmup")
        if position.is_open:
            holding_seconds = position.holding_seconds or 0
            if self.hold_seconds <= 0 or holding_seconds >= self.hold_seconds:
                return SignalScore(
                    symbol=symbol,
                    score=95,
                    action="CLOSE_POSITION",
                    reason="fake close",
                )
            score = 80 if holding_seconds >= self.hold_seconds * 0.75 else 55
            return SignalScore(symbol=symbol, score=score, reason="fake in position")
        if self._cooldown_remaining(state) > 0:
            score = 10 if state["last_exit_at"] > time.time() - 5 else 55
            return SignalScore(symbol=symbol, score=score, reason="fake cooldown")
        return SignalScore(
            symbol=symbol,
            score=95,
            action=self._entry_action(state),
            reason="fake open",
        )

    def decide_symbol(self, symbol, market, position, state):
        score = self.score_symbol(symbol, market, position, state)
        if score.action == "CLOSE_POSITION":
            return TradeDecision.close(score.reason)
        if score.action == "OPEN_SHORT":
            return TradeDecision.open_short(score.reason)
        if score.action == "OPEN_LONG":
            return TradeDecision.open_long(score.reason)
        return TradeDecision.hold(score.reason)

    def _entry_action(self, state):
        if self.side == "ALTERNATE":
            return "OPEN_SHORT" if state.get("last_open_side") == "LONG" else "OPEN_LONG"
        if self.side == "SHORT":
            return "OPEN_SHORT"
        return "OPEN_LONG"

    def _cooldown_remaining(self, state):
        last_exit_at = state.get("last_exit_at")
        if not last_exit_at or self.cooldown_seconds <= 0:
            return 0
        return max(0, self.cooldown_seconds - (time.time() - last_exit_at))


def test_pulse_strategy_generates_ready_open_after_history_and_cooldown():
    strategy = PulseLikeStrategy(min_history=2, cooldown_seconds=0)
    state = {}

    score = strategy.score_symbol("APTUSDT", _market("APTUSDT"), _flat("APTUSDT"), state)
    decision = strategy.decide_symbol("APTUSDT", _market("APTUSDT"), _flat("APTUSDT"), state)

    assert score.priority is SignalPriority.READY
    assert score.action == "OPEN_LONG"
    assert decision.action.value == "OPEN_LONG"


def test_pulse_strategy_generates_ready_close_after_hold_seconds():
    strategy = PulseLikeStrategy(min_history=2, hold_seconds=1)
    state = {}
    position = _long("APTUSDT", opened_at=time.time() - 2)

    score = strategy.score_symbol("APTUSDT", _market("APTUSDT"), position, state)
    decision = strategy.decide_symbol("APTUSDT", _market("APTUSDT"), position, state)

    assert score.priority is SignalPriority.READY
    assert score.action == "CLOSE_POSITION"
    assert decision.action.value == "CLOSE_POSITION"


def test_pulse_strategy_cooldown_is_per_symbol_state():
    strategy = PulseLikeStrategy(min_history=2, cooldown_seconds=60)
    apt_state = {"last_exit_at": time.time()}
    sei_state = {}

    apt = strategy.score_symbol("APTUSDT", _market("APTUSDT"), _flat("APTUSDT"), apt_state)
    sei = strategy.score_symbol("SEIUSDT", _market("SEIUSDT"), _flat("SEIUSDT"), sei_state)

    assert apt.priority is SignalPriority.COLD
    assert apt.action is None
    assert sei.priority is SignalPriority.READY
    assert sei.action == "OPEN_LONG"


def test_pulse_strategy_alternate_side_is_per_symbol():
    strategy = PulseLikeStrategy(min_history=2, cooldown_seconds=0, side="ALTERNATE")
    apt_state = {"last_open_side": "LONG"}
    sei_state = {"last_open_side": "SHORT"}

    apt = strategy.score_symbol("APTUSDT", _market("APTUSDT"), _flat("APTUSDT"), apt_state)
    sei = strategy.score_symbol("SEIUSDT", _market("SEIUSDT"), _flat("SEIUSDT"), sei_state)

    assert apt.action == "OPEN_SHORT"
    assert sei.action == "OPEN_LONG"


def test_pulse_strategy_attention_states():
    strategy = PulseLikeStrategy(
        min_history=2,
        hold_seconds=100,
        cooldown_seconds=10,
    )
    warm_position = _long("APTUSDT", opened_at=time.time())
    hot_position = _long("SEIUSDT", opened_at=time.time() - 80)
    cold_state = {"last_exit_at": time.time()}
    warm_state = {"last_exit_at": time.time() - 6}

    cold = strategy.score_symbol("BTCUSDT", _market("BTCUSDT"), _flat("BTCUSDT"), cold_state)
    warm = strategy.score_symbol("ETHUSDT", _market("ETHUSDT"), _flat("ETHUSDT"), warm_state)
    in_position_warm = strategy.score_symbol("APTUSDT", _market("APTUSDT"), warm_position, {})
    in_position_hot = strategy.score_symbol("SEIUSDT", _market("SEIUSDT"), hot_position, {})

    assert cold.priority is SignalPriority.COLD
    assert warm.priority is SignalPriority.WARM
    assert in_position_warm.priority is SignalPriority.WARM
    assert in_position_hot.priority is SignalPriority.HOT


def _market(symbol: str) -> MarketSnapshot:
    return MarketSnapshot(symbol, Decimal("100"), [Decimal("99"), Decimal("100")])


def _flat(symbol: str) -> FuturesPosition:
    return FuturesPosition(
        symbol,
        PositionDirection.NONE,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )


def _long(symbol: str, *, opened_at: float) -> FuturesPosition:
    return FuturesPosition(
        symbol,
        PositionDirection.LONG,
        Decimal("1"),
        Decimal("100"),
        Decimal("0"),
        opened_at=opened_at,
    )
