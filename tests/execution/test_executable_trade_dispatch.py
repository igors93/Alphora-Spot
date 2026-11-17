from types import SimpleNamespace

from config.symbol_context import symbol_config_from_app_config
from domain.models import TradeAction
from execution.coordinator import ExecutionCoordinator
from governor.execution_engine import ExecutionEngine


def test_coordinator_dispatches_approved_trade_command_once(config_factory) -> None:
    profile = symbol_config_from_app_config(config_factory())
    snapshot = object()
    command = SimpleNamespace(action=TradeAction.OPEN_LONG)
    trade = SimpleNamespace(execution_command=command)
    calls = []
    coordinator = ExecutionCoordinator(
        open_trade=lambda symbol, heavy, payload: calls.append((symbol, heavy, payload)),
        close_trade=lambda *_args: None,
        ignore_trade=lambda *_args: None,
    )

    coordinator.execute_trade(symbol_config=profile, heavy_snapshot=snapshot, trade=trade)

    assert calls == [(profile, snapshot, command)]


def test_execution_engine_forwards_trade_to_its_coordinator(config_factory) -> None:
    profile = symbol_config_from_app_config(config_factory())
    snapshot = object()
    trade = SimpleNamespace(execution_command=SimpleNamespace(action=TradeAction.OPEN_LONG))
    calls = []
    engine = object.__new__(ExecutionEngine)
    engine.coordinator = SimpleNamespace(
        execute_trade=lambda **kwargs: calls.append(kwargs) or "done"
    )

    assert (
        engine.execute_trade(symbol_config=profile, heavy_snapshot=snapshot, trade=trade) == "done"
    )
    assert calls == [{"symbol_config": profile, "heavy_snapshot": snapshot, "trade": trade}]
