from decimal import Decimal
from types import SimpleNamespace

from governor.stages.execution import ExecutionStage
from governor.symbol_evaluation_pipeline import ExecutableTrade
from tests.governor.stages.fakes import FakeJournal, make_evaluation
from trading.intent import TradeIntent


def test_stage_22_passes_approved_trade_to_coordinator() -> None:
    calls = []
    result = object()
    coordinator = SimpleNamespace(
        execute=lambda **_kwargs: calls.append("legacy"),
        execute_trade=lambda **kwargs: calls.append(kwargs) or result,
    )
    ports = SimpleNamespace(
        trade_events=SimpleNamespace(record_execution_payload_sent=lambda **_kwargs: None),
        journal=FakeJournal(),
        trace_payload=lambda: {},
        record_transition=lambda *_args, **_kwargs: None,
        execution_coordinator=coordinator,
    )
    stage = ExecutionStage(outcomes=SimpleNamespace(), ports=ports)
    command = SimpleNamespace(command_id="cmd-1")
    trade = ExecutableTrade(validated_trade=SimpleNamespace(), execution_command=command)
    heavy = object()
    evaluation = make_evaluation(
        intent=TradeIntent(
            symbol="BTCUSDT", action="OPEN_LONG", reason="signal", market_price=Decimal("100")
        ),
        heavy=heavy,
        executable_trade=trade,
        pre_execution_guard_decision=SimpleNamespace(reason="approved"),
        final_economic_validation=SimpleNamespace(),
    )

    assert stage._dispatch(evaluation, "OPEN_LONG", command) is result
    assert calls == [
        {
            "symbol_config": evaluation.evaluation_input.symbol_config,
            "heavy_snapshot": heavy,
            "trade": trade,
        }
    ]
