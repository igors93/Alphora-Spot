import sys
import types
from decimal import Decimal

import pytest

from domain.models import (
    FuturesPosition,
    MarketSnapshot,
    PositionDirection,
    TradeAction,
    TradeDecision,
)
from strategies.futures.base import FuturesStrategy
from strategies.loader import StrategyLoadError, load_strategy


def test_load_configured_strategy_from_fake_test_module(monkeypatch):
    module = types.ModuleType("strategies.futures.fixed.test_loadable_strategy")

    class LoadableStrategy(FuturesStrategy):
        def score_symbol(self, symbol, market, position, state):
            raise AssertionError

        def decide_symbol(self, symbol, market, position, state):
            return TradeDecision.hold("test")

    module.LoadableStrategy = LoadableStrategy
    monkeypatch.setitem(sys.modules, module.__name__, module)

    strategy = load_strategy(
        "strategies.futures.fixed.test_loadable_strategy",
        "LoadableStrategy",
    )

    assert isinstance(strategy, LoadableStrategy)
    assert isinstance(strategy, FuturesStrategy)


def test_loader_rejects_non_strategy_class(monkeypatch):
    module = types.ModuleType("strategies.futures.bad_strategy")
    module.NotAStrategy = object
    monkeypatch.setitem(sys.modules, "strategies.futures.bad_strategy", module)

    with pytest.raises(StrategyLoadError, match="precisa herdar"):
        load_strategy("strategies.futures.bad_strategy", "NotAStrategy")


def test_loader_rejects_fixed_strategy_outside_fixed_package(monkeypatch):
    module = types.ModuleType("strategies.futures.experimental.test_strategy")

    class WrongPlaceStrategy(FuturesStrategy):
        def score_symbol(self, symbol, market, position, state):
            raise AssertionError

        def decide_symbol(self, symbol, market, position, state):
            return TradeDecision.hold("test")

    module.WrongPlaceStrategy = WrongPlaceStrategy
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(StrategyLoadError, match="fixed"):
        load_strategy(module.__name__, "WrongPlaceStrategy")


def test_fake_strategy_can_open_and_close_operationally():
    class OpenCloseStrategy(FuturesStrategy):
        def score_symbol(self, symbol, market, position, state):
            raise AssertionError

        def decide_symbol(self, symbol, market, position, state):
            if position.is_open:
                return TradeDecision.close("close test")
            return TradeDecision.open_long("open test")

    strategy = OpenCloseStrategy()
    market = MarketSnapshot("BTCUSDT", Decimal("100"), [Decimal("100")])
    flat = FuturesPosition(
        "BTCUSDT",
        PositionDirection.NONE,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )

    assert strategy.decide_symbol("BTCUSDT", market, flat, {}).action == TradeAction.OPEN_LONG

    open_position = FuturesPosition(
        "BTCUSDT",
        PositionDirection.LONG,
        Decimal("0.5"),
        Decimal("100"),
        Decimal("0"),
        opened_at=0,
    )

    assert (
        strategy.decide_symbol("BTCUSDT", market, open_position, {}).action
        == TradeAction.CLOSE_POSITION
    )


def test_strategy_params_instantiate_same_strategy_with_different_params(monkeypatch):
    module = types.ModuleType("strategies.futures.fixed.param_strategy")

    class ParamStrategy(FuturesStrategy):
        def __init__(self, value=0):
            self.value = value

        @classmethod
        def from_params(cls, params):
            return cls(value=params["value"])

        def score_symbol(self, symbol, market, position, state):
            raise AssertionError

        def decide_symbol(self, symbol, market, position, state):
            return TradeDecision.hold("test")

    module.ParamStrategy = ParamStrategy
    monkeypatch.setitem(sys.modules, "strategies.futures.fixed.param_strategy", module)

    first = load_strategy(
        "strategies.futures.fixed.param_strategy",
        "ParamStrategy",
        {"value": 1},
    )
    second = load_strategy(
        "strategies.futures.fixed.param_strategy",
        "ParamStrategy",
        {"value": 2},
    )

    assert first.value == 1
    assert second.value == 2
