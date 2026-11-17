import pytest

from domain.models import TradeAction
from futures.position_execution import PositionExecutionResult, PositionExecutionStatus
from governor.execution_engine import ExecutionResult, ExecutionStatus


def test_execution_results_reject_contradictory_position_change():
    with pytest.raises(ValueError, match="DRY_RUN execution cannot change position"):
        ExecutionResult(
            command_id="cmd",
            status=ExecutionStatus.DRY_RUN,
            symbol="BTCUSDT",
            action=TradeAction.OPEN_LONG,
            order_ids=(),
            position_changed=True,
            reason="contradictory",
        )

    with pytest.raises(ValueError, match="BLOCKED execution cannot change position"):
        PositionExecutionResult(
            status=PositionExecutionStatus.BLOCKED,
            position_changed=True,
            reason="contradictory",
        )
