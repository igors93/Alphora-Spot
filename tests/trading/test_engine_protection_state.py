from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, PositionDirection
from execution.engine import FuturesTradingEngine
from futures.operational import OperationalState
from futures.protection import (
    PROTECTION_SOURCE_TRADE_PLAN,
    ExpectedProtection,
    ProtectionAuditResult,
)
from tests.fakes import StubPositionLedger


def test_mark_position_protected_preserves_trade_plan_metadata():
    engine = object.__new__(FuturesTradingEngine)
    engine.position_ledger = StubPositionLedger()
    engine.active_trade_id = "TRADE-1"
    engine.runtime_state = SimpleNamespace(entry_client_order_id="ENTRY-1")

    saved_payload = {}

    def fake_save_state_for_position(**kwargs):
        saved_payload.update(kwargs)

    engine._save_state_for_position = fake_save_state_for_position
    engine._record_operational_state = lambda *args, **kwargs: None
    engine._new_trade_id = lambda purpose: f"NEW-{purpose}"
    engine._extract_algo_client_id = lambda order: order.get("clientAlgoId", "")

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.001"),
        entry_price=Decimal("75000"),
        unrealized_pnl=Decimal("0"),
    )

    expected = ExpectedProtection(
        close_side="SELL",
        stop_price=Decimal("74000"),
        take_profit_price=Decimal("77000"),
        working_type="MARK_PRICE",
        model="TRADE_PLAN",
        source=PROTECTION_SOURCE_TRADE_PLAN,
        trade_plan_id="PLAN-1",
    )

    audit_result = ProtectionAuditResult(
        ok=True,
        stop_order={"clientAlgoId": "STOP-1"},
        take_order={"clientAlgoId": "TAKE-1"},
        missing=[],
        invalid=[],
        expected=expected,
    )

    FuturesTradingEngine._mark_position_protected(
        engine,
        position,
        audit_result=audit_result,
        reason="test",
    )

    assert saved_payload["operational_state"] is OperationalState.POSITION_OPEN_PROTECTED
    assert saved_payload["protection_source"] == PROTECTION_SOURCE_TRADE_PLAN
    assert saved_payload["trade_plan_id"] == "PLAN-1"
    assert saved_payload["planned_stop_price"] == Decimal("74000")
    assert saved_payload["planned_take_profit_price"] == Decimal("77000")
    assert saved_payload["stop_client_algo_id"] == "STOP-1"
    assert saved_payload["take_client_algo_id"] == "TAKE-1"
