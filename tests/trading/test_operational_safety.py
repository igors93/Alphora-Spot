import json
from dataclasses import replace
from decimal import Decimal

from config import ExecutionMode, ExitModelType, ProtectionPolicy, RecoveryAction
from domain.models import PositionDirection, TradeAction
from futures.operational import OperationalState, manual_reconcile_path
from futures.state import state_from_position
from ops.cancel_robot_orders import cancel_robot_orders
from ops.emergency_flatten import emergency_flatten
from ops.operational_stop import operational_stop
from ops.preflight import run_preflight
from tests.fakes import FakeFuturesClient
from tests.trading.test_engine_safety import make_engine


def _protected_algo_orders():
    return [
        {
            "clientAlgoId": "ALPHORA_STOP_1",
            "algoId": 1,
            "orderType": "STOP_MARKET",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "triggerPrice": "99.0",
            "algoStatus": "NEW",
        },
        {
            "clientAlgoId": "ALPHORA_TAKE_1",
            "algoId": 2,
            "orderType": "TAKE_PROFIT_MARKET",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "triggerPrice": "102.0",
            "algoStatus": "NEW",
        },
    ]


def test_stop_keep_protections_does_not_close_position_or_cancel_protection(
    monkeypatch,
    config_factory,
    tmp_path,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.open_algo_order_items = _protected_algo_orders()

    monkeypatch.setattr("ops.operational_stop.load_config", lambda: config)
    monkeypatch.setattr("ops.operational_stop.make_client", lambda *_args, **_kwargs: client)

    result = operational_stop(mode="STOP_KEEP_PROTECTIONS")

    assert result["ok"] is True
    assert (tmp_path / "runtime" / "stop.request").exists()
    assert client.new_order_calls == []
    assert client.cancel_algo_order_calls == []


def test_safe_stop_blocks_if_position_is_unprotected(monkeypatch, config_factory, tmp_path):
    config = config_factory(
        execution_mode=ExecutionMode.TESTNET_LIVE,
        protection_policy=ProtectionPolicy.PAUSE,
    )
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")

    monkeypatch.setattr("ops.operational_stop.load_config", lambda: config)
    monkeypatch.setattr("ops.operational_stop.make_client", lambda *_args, **_kwargs: client)

    result = operational_stop(mode="SAFE_STOP", confirm="SAFE_STOP")

    assert result["ok"] is False
    assert result["results"][0]["state"] == OperationalState.STOP_BLOCKED_UNPROTECTED.value
    assert not (tmp_path / "runtime" / "stop.request").exists()


def test_emergency_flatten_closes_position_before_cancel_orders(monkeypatch, config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    events = []

    class ClosingClient(FakeFuturesClient):
        def new_order(self, **params):
            events.append("close")
            response = super().new_order(**params)
            self.direction = PositionDirection.NONE
            self.quantity = Decimal("0")
            return response

    client = ClosingClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")

    def fake_cancel_robot_orders(**_kwargs):
        events.append("cancel")
        return {"dry_run": False}

    monkeypatch.setattr("ops.emergency_flatten.load_config", lambda: config)
    monkeypatch.setattr("ops.emergency_flatten.make_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr("ops.emergency_flatten.cancel_robot_orders", fake_cancel_robot_orders)

    result = emergency_flatten(confirm="EMERGENCY_FLATTEN")

    assert result["dry_run"] is False
    assert events == ["close", "cancel"]


def test_cancel_robot_orders_blocks_canceling_protections_with_open_position(
    monkeypatch,
    config_factory,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.open_algo_order_items = _protected_algo_orders()

    monkeypatch.setattr("ops.cancel_robot_orders.load_config", lambda: config)
    monkeypatch.setattr("ops.cancel_robot_orders.make_client", lambda *_args, **_kwargs: client)

    result = cancel_robot_orders(confirm="CANCEL_ROBOT_ORDERS")

    assert result["blocked"] is True
    assert result["required_confirmation"] == "CANCEL_PROTECTIONS_WHILE_IN_POSITION"
    assert client.cancel_algo_order_calls == []


def test_protections_pending_is_saved_before_creating_stop_take(
    config_factory,
    symbol_rules,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules)

    def fake_create_protection_orders(**_kwargs):
        state = engine.state_store.load()
        assert state is not None
        assert state.operational_state is OperationalState.PROTECTIONS_PENDING
        return {}

    engine._create_protection_orders = fake_create_protection_orders

    engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")


def test_recovery_prioritizes_protections_pending(
    config_factory,
    symbol_rules,
):
    config = replace(
        config_factory(
            execution_mode=ExecutionMode.TESTNET_LIVE,
            protection_policy=ProtectionPolicy.PAUSE,
        ),
        recovery_action=RecoveryAction.HOLD,
    )
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules)
    state = state_from_position(
        active_trade_id="trade",
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.5"),
        entry_client_order_id="entry",
        operational_state=OperationalState.PROTECTIONS_PENDING,
    )
    engine.state_store.save(state)
    engine.runtime_state = engine.state_store.load()

    engine._recover_state_on_boot()

    assert engine.paused is True
    with open(config.trade_journal_path, encoding="utf-8") as file:
        events = [json.loads(line) for line in file]
    assert any(
        event["event_type"] == "operational_state_changed"
        and event["data"]["reason"] == "recovery_protections_pending"
        for event in events
    )


def test_manual_reconcile_required_blocks_preflight(monkeypatch, config_factory):
    config = config_factory()
    path = manual_reconcile_path(config.runtime_dir, config.symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"reason": "entry_order_status_unknown"}', encoding="utf-8")
    monkeypatch.setattr("ops.preflight.load_config", lambda: config)

    result = run_preflight()

    assert result["ok"] is False
    manual = [check for check in result["checks"] if check["name"] == "manual_reconcile"]
    assert manual and manual[0]["ok"] is False


def test_atr_fallback_warning_is_observable(config_factory, symbol_rules):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        exit_model=ExitModelType.ATR,
    )
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules)
    position = client.position_risk("BTCUSDT")
    assert position[0]["positionAmt"] == "0"
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    open_position = engine._empty_position().__class__(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.5"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    engine._create_protection_orders(open_position, "test", "trade")

    state = engine.state_store.load()
    assert state is not None
    assert "ATR sem historico suficiente" in state.protection_warning
    with open(config.trade_journal_path, encoding="utf-8") as file:
        events = [json.loads(line) for line in file]
    assert any(event["event_type"] == "protection_model_warning" for event in events)
