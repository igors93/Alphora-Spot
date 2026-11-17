from decimal import Decimal
from types import SimpleNamespace

from binance.futures_client import BinanceFuturesNetworkError
from domain.models import FuturesPosition, PositionDirection, TradeAction
from execution.engine import FuturesTradingEngine
from futures.operational import OperationalState
from futures.position_execution import PositionExecutionResult, PositionExecutionStatus
from tests.fakes import StubPositionLedger


def _engine_with_confirmed_low_level_result(*, position, runtime_state, paused=False):
    engine = object.__new__(FuturesTradingEngine)
    engine.position_ledger = StubPositionLedger()
    engine.runtime_state = runtime_state
    engine.paused = paused
    engine._open_position = lambda *args, **kwargs: PositionExecutionResult.confirmed(
        "POSITION_OPEN_CONFIRMED",
        order_ids=("entry-1",),
        diagnostics={"trade_id": "trade-1"},
    )
    engine._get_position = lambda: position
    return engine


def _open_position():
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )


def _flat_position():
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def test_open_reports_rolled_back_when_final_position_is_flat_after_protection_failure():
    engine = _engine_with_confirmed_low_level_result(
        position=_flat_position(),
        runtime_state=None,
    )

    result = engine.open_position(TradeAction.OPEN_LONG, Decimal("100"), "signal")

    assert result.status is PositionExecutionStatus.SKIPPED
    assert result.position_changed is False
    assert getattr(result, "recovery_status", None).value == "ROLLED_BACK"
    assert getattr(result, "position_state", None).value == "UNCHANGED"
    assert result.reason == "POSITION_OPEN_ROLLED_BACK_AFTER_PROTECTION_FAILURE"


def test_open_reports_partial_with_unknown_position_when_protection_remains_pending():
    runtime_state = SimpleNamespace(operational_state=OperationalState.PROTECTIONS_PENDING)
    engine = _engine_with_confirmed_low_level_result(
        position=_open_position(),
        runtime_state=runtime_state,
        paused=True,
    )

    result = engine.open_position(TradeAction.OPEN_LONG, Decimal("100"), "signal")

    assert result.status is PositionExecutionStatus.UNCERTAIN
    assert (
        result.position_changed is False
    )  # compatibility projection; position_state is authoritative
    assert getattr(result, "recovery_status", None).value == "PARTIAL"
    assert getattr(result, "position_state", None).value == "UNKNOWN"
    assert result.reason == "POSITION_OPEN_PARTIAL_AFTER_PROTECTION_FAILURE"


def test_open_keeps_confirmed_result_when_runtime_state_is_protected():
    runtime_state = SimpleNamespace(operational_state=OperationalState.POSITION_OPEN_PROTECTED)
    engine = _engine_with_confirmed_low_level_result(
        position=_open_position(),
        runtime_state=runtime_state,
    )

    result = engine.open_position(TradeAction.OPEN_LONG, Decimal("100"), "signal")

    assert result.status is PositionExecutionStatus.CONFIRMED
    assert result.position_changed is True
    assert result.reason == "POSITION_OPEN_CONFIRMED"


def test_open_reports_partial_unknown_when_final_position_cannot_be_reconciled():
    runtime_state = SimpleNamespace(operational_state=OperationalState.PROTECTIONS_PENDING)
    engine = _engine_with_confirmed_low_level_result(
        position=_open_position(),
        runtime_state=runtime_state,
        paused=True,
    )
    engine._get_position = lambda: (_ for _ in ()).throw(
        BinanceFuturesNetworkError("position query unavailable")
    )

    result = engine.open_position(TradeAction.OPEN_LONG, Decimal("100"), "signal")

    assert result.status is PositionExecutionStatus.UNCERTAIN
    assert getattr(result, "recovery_status", None).value == "PARTIAL"
    assert getattr(result, "position_state", None).value == "UNKNOWN"
    assert result.reason == "POSITION_STATE_UNKNOWN_AFTER_PROTECTION_FAILURE"
