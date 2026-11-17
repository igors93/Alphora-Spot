"""Tests for journaling positions closed outside the robot's request path.

Covers the fix for dangling open trades in the journal when a position is
closed manually on the exchange (or a stop/take fills while the robot is
offline) — see _record_external_position_close.
"""

import logging
from decimal import Decimal
from types import SimpleNamespace

from binance.futures_client import BinanceFuturesAPIError
from domain.models import PositionDirection
from execution.engine import FuturesTradingEngine
from tests.fakes import StubPositionLedger


class _CapturingJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


class _CapturingRecorder:
    def __init__(self):
        self.completed = []

    def record_completed(self, **fields):
        self.completed.append(fields)


def _runtime_state(**overrides):
    state = SimpleNamespace(
        active_trade_id="ALP_BTC_T_OPEN_LONG_1783099166323_A5",
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.0009"),
        entry_price=Decimal("62175.70"),
        opened_at=1783099167.29,
        trade_plan_id=None,
        planned_stop_price=Decimal("61989.10"),
        planned_take_profit_price=Decimal("62424.50"),
        stop_client_algo_id="ALP_BTC_STOP_1",
        take_client_algo_id="ALP_BTC_TAKE_1",
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _engine(runtime_state, *, client=None):
    engine = object.__new__(FuturesTradingEngine)
    engine.position_ledger = StubPositionLedger()
    engine.runtime_state = runtime_state
    engine.logger = logging.getLogger("test.external_close")
    engine.journal = _CapturingJournal()
    engine.trade_audit_recorder = _CapturingRecorder()
    engine.client = client or SimpleNamespace()
    return engine


def _failing_query(*args, **kwargs):
    raise BinanceFuturesAPIError(400, -2013, "Order does not exist")


def test_noop_when_no_runtime_state():
    engine = _engine(None)

    engine._record_external_position_close(reason="flat_position_detected")

    assert engine.journal.events == []
    assert engine.trade_audit_recorder.completed == []


def test_records_external_close_when_protection_orders_unknown():
    client = SimpleNamespace(query_algo_order=_failing_query)
    engine = _engine(_runtime_state(), client=client)

    engine._record_external_position_close(reason="recovery_boot_exchange_flat")

    assert len(engine.journal.events) == 1
    event_type, data = engine.journal.events[0]
    assert event_type == "position_closed_externally"
    assert data["trade_id"] == "ALP_BTC_T_OPEN_LONG_1783099166323_A5"
    assert data["exit_source"] == "EXTERNAL"
    assert data["exit_reason"] == "recovery_boot_exchange_flat"
    assert data["pnl_status"] == "UNKNOWN"
    assert data["close_price"] is None

    assert len(engine.trade_audit_recorder.completed) == 1
    completed = engine.trade_audit_recorder.completed[0]
    assert completed["exit_source"] == "EXTERNAL"
    assert completed["pnl_source"] == "EXTERNAL_CLOSE"


def test_classifies_late_stop_fill():
    def query_algo_order(client_algo_id):
        if client_algo_id == "ALP_BTC_STOP_1":
            return {"algoStatus": "FILLED", "avgPrice": "61989.05"}
        return {"algoStatus": "CANCELLED"}

    client = SimpleNamespace(query_algo_order=query_algo_order)
    engine = _engine(_runtime_state(), client=client)

    engine._record_external_position_close(reason="flat_position_detected")

    event_type, data = engine.journal.events[0]
    assert event_type == "position_closed_externally"
    assert data["exit_source"] == "STOP_LOSS"
    assert data["close_price"] == "61989.05"


def test_classifies_late_take_profit_fill():
    def query_algo_order(client_algo_id):
        if client_algo_id == "ALP_BTC_TAKE_1":
            return {"algoStatus": "EXECUTED", "avgPrice": "62424.60"}
        return {"algoStatus": "CANCELLED"}

    client = SimpleNamespace(query_algo_order=query_algo_order)
    engine = _engine(_runtime_state(), client=client)

    engine._record_external_position_close(reason="flat_position_detected")

    event_type, data = engine.journal.events[0]
    assert data["exit_source"] == "TAKE_PROFIT"
    assert data["close_price"] == "62424.60"


def test_journal_still_written_when_trade_audit_recorder_fails():
    class _BrokenRecorder:
        def record_completed(self, **fields):
            raise OSError("disk full")

    client = SimpleNamespace(query_algo_order=_failing_query)
    engine = _engine(_runtime_state(), client=client)
    engine.trade_audit_recorder = _BrokenRecorder()

    engine._record_external_position_close(reason="flat_position_detected")

    assert len(engine.journal.events) == 1
    assert engine.journal.events[0][0] == "position_closed_externally"
