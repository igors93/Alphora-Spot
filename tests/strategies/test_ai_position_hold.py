"""IA0.5: an AI position is held for the horizon the model predicts, as its label assumes."""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeAction
from strategies.futures.ai.probabilistic_strategy import ProbabilisticAIStrategy
from tests.fakes import candles_from_closes

INTERVAL = "15m"
HORIZON = 4


def _market(candle_count: int) -> MarketSnapshot:
    candles = candles_from_closes(
        [100 + index * 0.1 for index in range(candle_count)], interval=INTERVAL
    )
    return MarketSnapshot(
        symbol="BTCUSDT",
        price=candles[-1].close,
        prices=[candle.close for candle in candles],
        candles={INTERVAL: tuple(candles)},
    )


def _position(direction: PositionDirection) -> FuturesPosition:
    open_ = direction is not PositionDirection.NONE
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=direction,
        quantity=Decimal("1") if open_ else Decimal("0"),
        entry_price=Decimal("100") if open_ else Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def _strategy(monkeypatch, predictions: list[dict], **params) -> ProbabilisticAIStrategy:
    strategy = ProbabilisticAIStrategy.from_params({"global_model_path": "unused.json", **params})
    queue = list(predictions)

    def predict(_market):
        values = queue.pop(0) if len(queue) > 1 else queue[0]
        return {
            "reason": "AI_PROBABILITY_NO_ACTION",
            "model_available": True,
            "interval": INTERVAL,
            "horizon_candles": HORIZON,
            "p_long_event": 0.30,
            "p_short_event": 0.20,
            "action_candidate": "NO_ACTION",
            **values,
        }

    monkeypatch.setattr(strategy, "_predict", predict)
    return strategy


OPEN_LONG = {"p_long_event": 0.70, "action_candidate": "OPEN_LONG"}
FADED = {"p_long_event": 0.10, "p_short_event": 0.15}


def _decide(strategy, candle_count, direction, state):
    return strategy.decide_symbol("BTCUSDT", _market(candle_count), _position(direction), state)


def test_position_is_held_until_the_model_horizon_even_when_the_probability_fades(monkeypatch):
    strategy = _strategy(monkeypatch, [OPEN_LONG, FADED])
    state: dict = {}

    opened = _decide(strategy, 30, PositionDirection.NONE, state)
    held = [_decide(strategy, 30 + bars, PositionDirection.LONG, state) for bars in (1, 2, 3)]
    closed = _decide(strategy, 30 + HORIZON, PositionDirection.LONG, state)

    assert opened.action is TradeAction.OPEN_LONG
    assert all(decision.action is TradeAction.HOLD for decision in held)
    assert closed.action is TradeAction.CLOSE_POSITION
    assert "exit_reason=AI_HORIZON_REACHED" in closed.reason


def test_a_renewed_signal_on_the_same_side_restarts_the_window(monkeypatch):
    strategy = _strategy(monkeypatch, [OPEN_LONG])
    state: dict = {}

    _decide(strategy, 30, PositionDirection.NONE, state)
    renewed = _decide(strategy, 30 + HORIZON, PositionDirection.LONG, state)
    still_held = _decide(strategy, 30 + HORIZON + 1, PositionDirection.LONG, state)

    assert renewed.action is TradeAction.HOLD
    assert still_held.action is TradeAction.HOLD


def test_reversal_closes_after_the_minimum_hold(monkeypatch):
    reversal = {"p_long_event": 0.10, "p_short_event": 0.70}
    strategy = _strategy(monkeypatch, [OPEN_LONG, reversal], min_hold_candles=2)
    state: dict = {}

    _decide(strategy, 30, PositionDirection.NONE, state)
    early = _decide(strategy, 31, PositionDirection.LONG, state)
    after_minimum = _decide(strategy, 32, PositionDirection.LONG, state)

    assert early.action is TradeAction.HOLD
    assert after_minimum.action is TradeAction.CLOSE_POSITION
    assert "exit_reason=AI_REVERSAL" in after_minimum.reason


@pytest.mark.parametrize("fade_enabled", [False, True])
def test_probability_fade_closes_only_when_enabled(monkeypatch, fade_enabled):
    strategy = _strategy(
        monkeypatch,
        [OPEN_LONG, FADED],
        min_hold_candles=1,
        close_on_probability_fade=fade_enabled,
    )
    state: dict = {}

    _decide(strategy, 30, PositionDirection.NONE, state)
    decision = _decide(strategy, 31, PositionDirection.LONG, state)

    expected = TradeAction.CLOSE_POSITION if fade_enabled else TradeAction.HOLD
    assert decision.action is expected


def test_a_recovered_position_counts_from_the_first_candle_it_is_seen(monkeypatch):
    strategy = _strategy(monkeypatch, [FADED])
    state: dict = {}

    first_seen = _decide(strategy, 50, PositionDirection.LONG, state)
    before_horizon = _decide(strategy, 50 + HORIZON - 1, PositionDirection.LONG, state)
    at_horizon = _decide(strategy, 50 + HORIZON, PositionDirection.LONG, state)

    assert first_seen.action is TradeAction.HOLD
    assert before_horizon.action is TradeAction.HOLD
    assert at_horizon.action is TradeAction.CLOSE_POSITION


def test_short_positions_follow_the_same_rule(monkeypatch):
    open_short = {"p_short_event": 0.70, "p_long_event": 0.10, "action_candidate": "OPEN_SHORT"}
    strategy = _strategy(monkeypatch, [open_short, {"p_short_event": 0.05}])
    state: dict = {}

    _decide(strategy, 30, PositionDirection.NONE, state)
    held = _decide(strategy, 31, PositionDirection.SHORT, state)
    closed = _decide(strategy, 30 + HORIZON, PositionDirection.SHORT, state)

    assert held.action is TradeAction.HOLD
    assert closed.action is TradeAction.CLOSE_POSITION
