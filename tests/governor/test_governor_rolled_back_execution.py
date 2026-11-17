"""Tests for protection-failure rollback classification and propagation."""

import pytest

from domain.models import TradeAction
from futures.position_execution import (
    PositionChangeState,
    PositionExecutionResult,
    PositionExecutionStatus,
    PositionRecoveryStatus,
)
from governor.events import EXECUTION_ROLLED_BACK, EXECUTION_SKIPPED
from governor.execution_engine import (
    ExecutionCommand,
    ExecutionResult,
    ExecutionStatus,
    _execution_result_from_position_result,
)
from governor.stages.execution import execution_event_type

# ---------------------------------------------------------------------------
# PositionExecutionResult factory constraints
# ---------------------------------------------------------------------------


def test_rolled_back_factory_produces_correct_invariants():
    result = PositionExecutionResult.rolled_back(
        "PROTECTION_FAILED_POSITION_ROLLED_BACK",
        order_ids=("entry-001", "close-001"),
    )
    assert result.status is PositionExecutionStatus.SKIPPED
    assert result.position_changed is False
    assert result.position_state is PositionChangeState.UNCHANGED
    assert result.recovery_status is PositionRecoveryStatus.ROLLED_BACK
    assert result.reason == "PROTECTION_FAILED_POSITION_ROLLED_BACK"
    assert result.order_ids == ("entry-001", "close-001")


def test_partial_factory_produces_correct_invariants():
    result = PositionExecutionResult.partial(
        "PROTECTION_FAILED_CLOSE_UNCERTAIN",
        order_ids=("entry-002",),
    )
    assert result.status is PositionExecutionStatus.UNCERTAIN
    assert result.position_changed is False
    assert result.position_state is PositionChangeState.UNKNOWN
    assert result.recovery_status is PositionRecoveryStatus.PARTIAL
    assert result.reason == "PROTECTION_FAILED_CLOSE_UNCERTAIN"


def test_rolled_back_requires_skipped_status():
    with pytest.raises(ValueError, match="ROLLED_BACK recovery requires SKIPPED execution status"):
        PositionExecutionResult(
            status=PositionExecutionStatus.FAILED,
            position_changed=False,
            position_state=PositionChangeState.UNCHANGED,
            recovery_status=PositionRecoveryStatus.ROLLED_BACK,
            reason="bad",
        )


def test_rolled_back_requires_unchanged_position_state():
    with pytest.raises(ValueError, match="ROLLED_BACK recovery requires UNCHANGED position state"):
        PositionExecutionResult(
            status=PositionExecutionStatus.SKIPPED,
            position_changed=False,
            position_state=PositionChangeState.UNKNOWN,
            recovery_status=PositionRecoveryStatus.ROLLED_BACK,
            reason="bad",
        )


def test_partial_requires_uncertain_status():
    with pytest.raises(ValueError, match="PARTIAL recovery requires UNCERTAIN execution status"):
        PositionExecutionResult(
            status=PositionExecutionStatus.SKIPPED,
            position_changed=False,
            position_state=PositionChangeState.UNKNOWN,
            recovery_status=PositionRecoveryStatus.PARTIAL,
            reason="bad",
        )


def test_partial_requires_unknown_position_state():
    with pytest.raises(ValueError, match="PARTIAL recovery requires UNKNOWN position state"):
        PositionExecutionResult(
            status=PositionExecutionStatus.UNCERTAIN,
            position_changed=False,
            position_state=PositionChangeState.UNCHANGED,
            recovery_status=PositionRecoveryStatus.PARTIAL,
            reason="bad",
        )


# ---------------------------------------------------------------------------
# ExecutionResult new fields default and propagation
# ---------------------------------------------------------------------------


def test_execution_result_defaults_to_no_recovery():
    result = ExecutionResult(
        command_id="cmd-1",
        status=ExecutionStatus.SKIPPED,
        symbol="BTCUSDT",
        action=TradeAction.OPEN_LONG,
        order_ids=(),
        position_changed=False,
        reason="PLAIN_SKIP",
    )
    assert result.position_state is PositionChangeState.UNCHANGED
    assert result.recovery_status is PositionRecoveryStatus.NONE


def _make_command() -> ExecutionCommand:
    from decimal import Decimal

    return ExecutionCommand(
        command_id="cmd-test",
        intent_id="intent-1",
        symbol="BTCUSDT",
        action=TradeAction.OPEN_LONG,
        quantity=Decimal("0.01"),
        market_price=Decimal("50000"),
        stop_price=Decimal("49000"),
        take_profit_price=Decimal("52000"),
        planned_exit_source=None,
        trade_plan_id=None,
        reason="test_open",
    )


def test_bridge_propagates_rolled_back():
    command = _make_command()
    position_result = PositionExecutionResult.rolled_back(
        "PROTECTION_FAILED_POSITION_ROLLED_BACK",
        order_ids=("entry-001", "close-001"),
        diagnostics={"trade_id": "t-001"},
    )
    execution_result = _execution_result_from_position_result(
        command=command,
        symbol="BTCUSDT",
        action=TradeAction.OPEN_LONG,
        result=position_result,
    )
    assert execution_result.status is ExecutionStatus.SKIPPED
    assert execution_result.position_changed is False
    assert execution_result.recovery_status is PositionRecoveryStatus.ROLLED_BACK
    assert execution_result.position_state is PositionChangeState.UNCHANGED
    assert execution_result.reason == "PROTECTION_FAILED_POSITION_ROLLED_BACK"
    assert "entry-001" in execution_result.order_ids
    assert "close-001" in execution_result.order_ids


def test_bridge_propagates_partial():
    command = _make_command()
    position_result = PositionExecutionResult.partial(
        "PROTECTION_FAILED_ENGINE_PAUSED",
        order_ids=("entry-002",),
    )
    execution_result = _execution_result_from_position_result(
        command=command,
        symbol="BTCUSDT",
        action=TradeAction.OPEN_LONG,
        result=position_result,
    )
    assert execution_result.status is ExecutionStatus.UNCERTAIN
    assert execution_result.recovery_status is PositionRecoveryStatus.PARTIAL
    assert execution_result.position_state is PositionChangeState.UNKNOWN


def test_bridge_propagates_confirmed():
    command = _make_command()
    position_result = PositionExecutionResult.confirmed(
        "POSITION_OPEN_CONFIRMED",
        order_ids=("entry-003",),
    )
    execution_result = _execution_result_from_position_result(
        command=command,
        symbol="BTCUSDT",
        action=TradeAction.OPEN_LONG,
        result=position_result,
    )
    assert execution_result.status is ExecutionStatus.CONFIRMED
    assert execution_result.position_changed is True
    assert execution_result.recovery_status is PositionRecoveryStatus.NONE
    assert execution_result.position_state is PositionChangeState.CHANGED


# ---------------------------------------------------------------------------
# execution_event_type distinguishes ROLLED_BACK from plain SKIPPED
# ---------------------------------------------------------------------------


def _make_execution_result(
    *,
    status: ExecutionStatus,
    recovery_status: PositionRecoveryStatus = PositionRecoveryStatus.NONE,
    position_state: PositionChangeState = PositionChangeState.UNCHANGED,
    position_changed: bool = False,
) -> ExecutionResult:
    return ExecutionResult(
        command_id="cmd-evt",
        status=status,
        symbol="BTCUSDT",
        action=TradeAction.OPEN_LONG,
        order_ids=(),
        position_changed=position_changed,
        reason="reason",
        position_state=position_state,
        recovery_status=recovery_status,
    )


def test_execution_event_type_rolled_back_returns_rolled_back_event():
    result = _make_execution_result(
        status=ExecutionStatus.SKIPPED,
        recovery_status=PositionRecoveryStatus.ROLLED_BACK,
    )
    assert execution_event_type(result) == EXECUTION_ROLLED_BACK


def test_execution_event_type_plain_skipped_returns_skipped_event():
    result = _make_execution_result(status=ExecutionStatus.SKIPPED)
    assert execution_event_type(result) == EXECUTION_SKIPPED


def test_execution_event_type_confirmed_returns_correct_event():
    from governor.events import EXECUTION_CONFIRMED

    result = _make_execution_result(
        status=ExecutionStatus.CONFIRMED,
        position_state=PositionChangeState.CHANGED,
        position_changed=True,
    )
    assert execution_event_type(result) == EXECUTION_CONFIRMED


def test_execution_event_type_uncertain_partial_returns_uncertain_not_rolled_back():
    from governor.events import EXECUTION_UNCERTAIN

    result = ExecutionResult(
        command_id="cmd-unc",
        status=ExecutionStatus.UNCERTAIN,
        symbol="BTCUSDT",
        action=TradeAction.OPEN_LONG,
        order_ids=(),
        position_changed=False,
        reason="partial",
        position_state=PositionChangeState.UNKNOWN,
        recovery_status=PositionRecoveryStatus.PARTIAL,
    )
    assert execution_event_type(result) == EXECUTION_UNCERTAIN
