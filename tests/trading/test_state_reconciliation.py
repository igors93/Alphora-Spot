import json
from decimal import Decimal

from domain.models import FuturesPosition, PositionDirection
from execution.state_reconciliation import StateReconciliationEngine, build_exchange_state_snapshot
from operational_module_modes import ReconciliationMode


class FakeRepairClient:
    def __init__(self):
        self.cancel_order_calls = []
        self.cancel_algo_order_calls = []

    def cancel_order(self, **kwargs):
        self.cancel_order_calls.append(kwargs)
        return {"status": "CANCELED", **kwargs}

    def cancel_algo_order(self, **kwargs):
        self.cancel_algo_order_calls.append(kwargs)
        return {"algoStatus": "CANCELED", **kwargs}


def _position(direction=PositionDirection.LONG, quantity=Decimal("0.1")):
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=direction,
        quantity=quantity,
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )


def _stop_order():
    return {
        "symbol": "BTCUSDT",
        "orderType": "STOP_MARKET",
        "clientAlgoId": "ALPHORA_STOP_1",
    }


def _take_order():
    return {
        "symbol": "BTCUSDT",
        "orderType": "TAKE_PROFIT_MARKET",
        "clientAlgoId": "ALPHORA_TAKE_1",
    }


def test_state_reconciliation_ok_without_divergences():
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        local_position=_position(),
        exchange_position=_position(),
        robot_algo_orders=[_stop_order(), _take_order()],
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.ALERT,
    )

    assert decision.status == "OK"
    assert decision.issues == []


def test_state_reconciliation_exchange_position_without_local_generates_issue():
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        exchange_position=_position(),
        robot_algo_orders=[_stop_order(), _take_order()],
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.ALERT,
    )

    assert decision.issues[0].code == "EXCHANGE_POSITION_WITHOUT_LOCAL_STATE"
    assert decision.should_block_new_entries is False


def test_state_reconciliation_local_position_without_exchange_generates_issue():
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        local_position=_position(),
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.ALERT,
    )

    assert decision.issues[0].code == "LOCAL_POSITION_WITHOUT_EXCHANGE_POSITION"


def test_state_reconciliation_position_without_stop_is_critical():
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        local_position=_position(),
        exchange_position=_position(),
        robot_algo_orders=[_take_order()],
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.BLOCK,
    )

    assert any(issue.code == "POSITION_WITHOUT_STOP" for issue in decision.issues)
    assert decision.should_block_new_entries is True


def test_state_reconciliation_orphan_order_generates_repairable_issue():
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        robot_normal_orders=[{"orderId": 1, "clientOrderId": "ALPHORA_ENTRY_1"}],
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.ALERT,
    )

    assert decision.issues[0].code == "ORPHAN_ROBOT_ORDERS"
    assert decision.issues[0].can_repair is True


def test_state_reconciliation_alert_does_not_block():
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        exchange_position=_position(),
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.ALERT,
    )

    assert decision.should_block_new_entries is False
    assert decision.status == "DIVERGENT"


def test_state_reconciliation_block_blocks_critical_divergence():
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        exchange_position=_position(),
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.BLOCK,
    )

    assert decision.should_block_new_entries is True
    assert decision.status == "BLOCKED"


def test_state_reconciliation_repair_safe_cancels_orphan_robot_orders():
    client = FakeRepairClient()
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        robot_normal_orders=[{"orderId": 1, "clientOrderId": "ALPHORA_ENTRY_1"}],
        robot_algo_orders=[{"algoId": 2, "clientAlgoId": "ALPHORA_STOP_1"}],
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.REPAIR_SAFE,
        repair_client=client,
    )

    assert decision.status == "REPAIRED"
    assert decision.repairs_attempted == 2
    assert len(client.cancel_order_calls) == 1
    assert len(client.cancel_algo_order_calls) == 1


def test_state_reconciliation_repair_safe_never_closes_position():
    client = FakeRepairClient()
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        local_position=_position(),
        exchange_position=_position(),
        robot_algo_orders=[],
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.REPAIR_SAFE,
        repair_client=client,
    )

    assert decision.repairs_attempted == 0
    assert not hasattr(client, "close_position_calls")


def test_state_reconciliation_repair_safe_audits_skipped_protection_repair():
    snapshot = build_exchange_state_snapshot(
        symbol="BTCUSDT",
        local_position=_position(),
        exchange_position=_position(),
        robot_algo_orders=[],
    )

    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.REPAIR_SAFE,
    )

    skipped = decision.diagnostics["repair_skipped"]
    assert skipped[0]["reason"] == "REPAIR_SKIPPED_NO_VALID_PLANNED_EXIT"
    assert decision.repairs_attempted == 0
    assert decision.repairs_failed == 0


def test_state_reconciliation_report_payload_is_json_serializable():
    snapshot = build_exchange_state_snapshot(symbol="BTCUSDT")
    decision = StateReconciliationEngine().evaluate(
        snapshot,
        mode=ReconciliationMode.OBSERVE,
    )

    json.dumps(decision.to_dict())
