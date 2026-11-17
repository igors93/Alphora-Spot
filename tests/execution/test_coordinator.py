from types import SimpleNamespace

from config.symbol_context import symbol_config_from_app_config
from domain.models import TradeAction
from execution.coordinator import ExecutionCoordinator


def test_coordinator_dispatches_each_command_once(config_factory) -> None:
    profile = symbol_config_from_app_config(config_factory())
    snapshot = object()
    calls = []

    def port(name):
        def execute(symbol_config, heavy_snapshot, command):
            calls.append((name, symbol_config, heavy_snapshot, command))
            return name

        return execute

    coordinator = ExecutionCoordinator(
        open_trade=port("open"),
        close_trade=port("close"),
        ignore_trade=port("ignored"),
    )
    for action, expected in (
        (TradeAction.OPEN_LONG, "open"),
        (TradeAction.OPEN_SHORT, "open"),
        (TradeAction.CLOSE_POSITION, "close"),
        (TradeAction.HOLD, "ignored"),
    ):
        command = SimpleNamespace(action=action)
        assert (
            coordinator.execute(symbol_config=profile, heavy_snapshot=snapshot, command=command)
            == expected
        )
        assert calls[-1] == (expected, profile, snapshot, command)
    assert len(calls) == 4
