from dataclasses import replace
from decimal import Decimal

import pytest

from config.symbol_context import symbol_config_from_app_config
from domain.candle import Candle
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeDecision
from domain.strategy_requirements import (
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    StrategyDataRequirements,
)
from governor.strategy_coordinator import StrategyCoordinator
from scanner.signal_score import SignalScore
from trading.intent import TradeIntent


class GlobalSymbolStrategy:
    def score_symbol(self, symbol, market, position, state):
        state["score_calls"] = int(state.get("score_calls", 0)) + 1
        return SignalScore(
            symbol=symbol,
            score=95,
            action="OPEN_LONG",
            reason=f"score {symbol}",
        )

    def decide_symbol(self, symbol, market, position, state):
        state["decide_calls"] = int(state.get("decide_calls", 0)) + 1
        return TradeDecision.open_long(f"decide {symbol}")

    def intent_symbol(self, symbol, market, position, state):
        state["intent_calls"] = int(state.get("intent_calls", 0)) + 1
        return TradeIntent(
            symbol=symbol,
            action="OPEN_LONG",
            confidence=95,
            reason=f"intent {symbol}",
            strategy_name="tests.GlobalSymbolStrategy",
        )


class DecisionSymbolStrategy:
    def score_symbol(self, symbol, market, position, state):
        state["score_calls"] = int(state.get("score_calls", 0)) + 1
        return SignalScore(
            symbol=symbol,
            score=95,
            action="OPEN_LONG",
            reason=f"score {symbol}",
        )

    def decide_symbol(self, symbol, market, position, state):
        state["decide_calls"] = int(state.get("decide_calls", 0)) + 1
        return TradeDecision.open_long(f"decide {symbol}")


class CandleCloseStrategy(GlobalSymbolStrategy):
    def data_requirements(self):
        return StrategyDataRequirements(
            primary_timeframe="1m",
            required_candles={"1m": 1},
            evaluation_policy=EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
        )


class ClassicStatefulStrategy:
    def __init__(self):
        self.calls = 0

    def score(self, market, position):
        self.calls += 1
        return SignalScore(
            symbol=market.symbol, score=95, action="OPEN_LONG", reason=str(self.calls)
        )

    def decide(self, market, position):
        self.calls += 1
        return TradeDecision.open_long(str(self.calls))


def test_strategy_coordinator_loads_global_strategy_and_applies_to_two_symbols(
    config_factory,
    monkeypatch,
):
    import governor.strategy_coordinator as module

    monkeypatch.setattr(module, "load_strategy", lambda *_args, **_kwargs: GlobalSymbolStrategy())
    config = config_factory(symbols=("APTUSDT", "SEIUSDT"))
    profiles = [
        symbol_config_from_app_config(config, symbol="APTUSDT"),
        symbol_config_from_app_config(config, symbol="SEIUSDT"),
    ]
    coordinator = StrategyCoordinator(config=config, symbol_configs=profiles)

    first = coordinator.score(profiles[0], _market("APTUSDT"), _flat("APTUSDT"))
    second = coordinator.score(profiles[1], _market("SEIUSDT"), _flat("SEIUSDT"))

    assert coordinator.runtime_mode == "GLOBAL_SYMBOL_API"
    assert first.symbol == "APTUSDT"
    assert second.symbol == "SEIUSDT"
    assert coordinator.state_for_symbol("APTUSDT").data["score_calls"] == 1
    assert coordinator.state_for_symbol("SEIUSDT").data["score_calls"] == 1


def test_strategy_coordinator_intent_symbol_uses_isolated_state(config_factory, monkeypatch):
    import governor.strategy_coordinator as module

    monkeypatch.setattr(module, "load_strategy", lambda *_args, **_kwargs: GlobalSymbolStrategy())
    config = config_factory(symbols=("APTUSDT", "SEIUSDT"))
    profiles = [
        symbol_config_from_app_config(config, symbol="APTUSDT"),
        symbol_config_from_app_config(config, symbol="SEIUSDT"),
    ]
    coordinator = StrategyCoordinator(config=config, symbol_configs=profiles)

    first_score = coordinator.score(profiles[0], _market("APTUSDT"), _flat("APTUSDT"))
    coordinator.intent(
        profiles[0],
        _market("APTUSDT"),
        _flat("APTUSDT"),
        score=first_score,
    )
    second_score = coordinator.score(profiles[1], _market("SEIUSDT"), _flat("SEIUSDT"))
    coordinator.intent(
        profiles[1],
        _market("SEIUSDT"),
        _flat("SEIUSDT"),
        score=second_score,
    )

    assert coordinator.state_for_symbol("APTUSDT").data["score_calls"] == 1
    assert coordinator.state_for_symbol("APTUSDT").data["intent_calls"] == 1
    assert coordinator.state_for_symbol("SEIUSDT").data["score_calls"] == 1
    assert coordinator.state_for_symbol("SEIUSDT").data["intent_calls"] == 1


def test_strategy_coordinator_converts_decide_symbol_once(config_factory, monkeypatch):
    import governor.strategy_coordinator as module

    monkeypatch.setattr(module, "load_strategy", lambda *_args, **_kwargs: DecisionSymbolStrategy())
    config = config_factory(symbols=("APTUSDT",))
    profile = symbol_config_from_app_config(config, symbol="APTUSDT")
    coordinator = StrategyCoordinator(config=config, symbol_configs=[profile])

    score = coordinator.score(profile, _market("APTUSDT"), _flat("APTUSDT"))
    intent = coordinator.intent(profile, _market("APTUSDT"), _flat("APTUSDT"), score=score)

    assert intent.action == "OPEN_LONG"
    assert coordinator.state_for_symbol("APTUSDT").data["score_calls"] == 1
    assert coordinator.state_for_symbol("APTUSDT").data["decide_calls"] == 1


def test_strategy_coordinator_rejects_legacy_strategy_without_symbol_api(
    config_factory,
    monkeypatch,
):
    import governor.strategy_coordinator as module

    monkeypatch.setattr(
        module, "load_strategy", lambda *_args, **_kwargs: ClassicStatefulStrategy()
    )
    config = config_factory(symbols=("APTUSDT", "SEIUSDT"))
    profiles = [
        symbol_config_from_app_config(config, symbol="APTUSDT"),
        symbol_config_from_app_config(config, symbol="SEIUSDT"),
    ]
    with pytest.raises(TypeError, match="score_symbol"):
        StrategyCoordinator(config=config, symbol_configs=profiles)


def test_profile_strategy_params_are_context_only(config_factory, monkeypatch):
    import governor.strategy_coordinator as module

    monkeypatch.setattr(module, "load_strategy", lambda *_args, **_kwargs: GlobalSymbolStrategy())
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    profile = replace(profile, strategy_params={"min_history": 1})

    coordinator = StrategyCoordinator(config=config, symbol_configs=[profile])

    assert coordinator.profile_strategy_warnings


def test_strategy_coordinator_blocks_repeated_signal_on_same_primary_candle(
    config_factory,
    monkeypatch,
):
    import governor.strategy_coordinator as module

    monkeypatch.setattr(module, "load_strategy", lambda *_args, **_kwargs: CandleCloseStrategy())
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    coordinator = StrategyCoordinator(config=config, symbol_configs=[profile])

    open_market = _market_with_candle("BTCUSDT", _candle(is_closed=False))
    closed_market = _market_with_candle("BTCUSDT", _candle(is_closed=True))

    assert coordinator.should_evaluate(profile.symbol, open_market) == (
        False,
        "WAITING_FOR_PRIMARY_CANDLE_CLOSE",
    )
    assert coordinator.should_evaluate(profile.symbol, closed_market) == (True, "")

    coordinator.mark_evaluated(profile.symbol, closed_market)

    assert coordinator.should_evaluate(profile.symbol, closed_market) == (
        False,
        "ALREADY_EVALUATED_THIS_CANDLE",
    )


def _market(symbol: str) -> MarketSnapshot:
    return MarketSnapshot(symbol, Decimal("100"), [Decimal("99"), Decimal("100")])


def _market_with_candle(symbol: str, candle: Candle) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        price=Decimal("100"),
        prices=[Decimal("99"), Decimal("100")],
        candles={"1m": (candle,)},
        primary_timeframe="1m",
    )


def _candle(*, is_closed: bool) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=0,
        close_time_ms=59_999,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("10"),
        is_closed=is_closed,
        source="test",
    )


def _flat(symbol: str) -> FuturesPosition:
    return FuturesPosition(
        symbol,
        PositionDirection.NONE,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )
