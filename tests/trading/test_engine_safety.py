import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from binance.futures_client import BinanceFuturesAPIError, BinanceFuturesNetworkError
from config import ExecutionMode, ProtectionPolicy
from domain.models import FuturesPosition, PositionDirection, TradeAction
from execution.engine import FuturesTradingEngine
from futures.risk_manager import FuturesRiskManager
from operational_module_modes import (
    MODULE_SMART_SIZING,
    MODULE_STATE_RECONCILIATION,
    ModuleModeSource,
    ReconciliationMode,
    SmartSizingMode,
)
from tests.fakes import FakeFuturesClient


def make_engine(config, client, symbol_rules=None):
    engine = FuturesTradingEngine(
        config=config,
        client=client,
        risk_manager=FuturesRiskManager(
            position_notional_usdt=config.position_notional_usdt,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
        ),
    )
    if symbol_rules is not None:
        engine.rules = symbol_rules
    return engine


def _journal_events(path):
    with open(path, encoding="utf-8") as file:
        return [json.loads(line) for line in file]


def test_risk_guard_blocks_live_before_any_entry_mutation(config_factory, symbol_rules):
    config = config_factory(
        execution_mode=ExecutionMode.TESTNET_LIVE,
        max_exposure_usdt=Decimal("1"),
    )
    client = FakeFuturesClient()
    client.open_normal_orders = [{"clientOrderId": "ALPHORA_ENTRY_1", "orderId": 1}]
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")

    assert client.cancel_order_calls == []
    assert client.new_order_calls == []
    with open(config.trade_journal_path, encoding="utf-8") as file:
        events = [json.loads(line) for line in file]
    blocked = [event for event in events if event["event_type"] == "trade_blocked_by_risk_guard"]
    assert blocked
    assert blocked[0]["data"]["execution_mode"] == "TESTNET_LIVE"


def test_recovery_pauses_when_open_orders_query_fails(config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.open_orders_error = BinanceFuturesAPIError(500, -1000, "boom")
    engine = make_engine(config, client)

    engine._recover_state_on_boot()

    assert engine.paused is True


def test_recovery_records_state_reconciliation_block(config_factory, symbol_rules):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        state_reconciliation_mode=ReconciliationMode.BLOCK,
        operational_module_mode_sources={
            MODULE_STATE_RECONCILIATION: ModuleModeSource.DESKTOP.value,
        },
    )
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    engine._recover_state_on_boot()

    events = _journal_events(config.trade_journal_path)
    assert any(event["event_type"] == "reconciliation_snapshot_created" for event in events)
    assert any(
        event["event_type"] == "reconciliation_issue_detected"
        and event["data"]["code"] == "POSITION_WITHOUT_STOP"
        for event in events
    )
    assert any(event["event_type"] == "reconciliation_block_applied" for event in events)
    report_events = [
        event for event in events if event["event_type"] == "reconciliation_report_written"
    ]
    assert report_events
    report_path = Path(report_events[-1]["data"]["path"])
    assert report_path.exists()
    json.dumps(json.loads(report_path.read_text(encoding="utf-8")))


def test_exchange_config_skips_when_any_order_is_open(config_factory):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.open_normal_orders = [{"clientOrderId": "MANUAL_ORDER", "orderId": 1}]
    engine = make_engine(config, client)

    engine._configure_exchange()

    with open(config.trade_journal_path, encoding="utf-8") as file:
        events = [json.loads(line) for line in file]
    skipped = [event for event in events if event["event_type"] == "exchange_config_skipped"]
    assert skipped
    assert skipped[0]["data"]["reason"] == "orders_open"


def test_unknown_entry_order_is_reconciled_and_protected(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.new_order_error = BinanceFuturesNetworkError("timeout")
    client.query_order_response = {
        "status": "FILLED",
        "executedQty": "0.5",
        "avgPrice": "100",
        "clientOrderId": "entry",
    }
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")

    assert engine.paused is False
    assert len(client.new_algo_order_calls) == 2
    assert engine.runtime_state is not None
    assert engine.runtime_state.stop_client_algo_id is not None
    assert engine.runtime_state.take_client_algo_id is not None


def test_close_not_confirmed_pauses_and_keeps_protections(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade"
    engine.position_opened_at = 1.0
    engine._wait_until_flat = lambda: False

    position = engine._get_position()
    engine._close_position(position, "test")

    assert engine.paused is True
    assert client.cancel_order_calls == []
    assert client.cancel_algo_order_calls == []


def test_live_close_revalidates_current_position_quantity(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.income_history_response = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "2.50"}
    ]
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade"
    engine._wait_until_flat = lambda: True
    requested_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    engine._close_position(requested_position, "test")

    assert client.new_order_calls[0]["quantity"] == "0.5"
    events = _journal_events(config.trade_journal_path)
    assert any(event["event_type"] == "position_close_revalidated" for event in events)


def test_live_close_blocks_when_position_direction_changed(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.SHORT
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    requested_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.5"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    result = engine._close_position(requested_position, "test")

    assert result.reason == "POSITION_DIRECTION_CHANGED_BEFORE_CLOSE"
    assert client.new_order_calls == []
    assert engine.paused is True


def test_live_close_skips_order_when_position_is_already_flat(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade"
    requested_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    engine._close_position(requested_position, "test")

    assert client.new_order_calls == []
    events = _journal_events(config.trade_journal_path)
    assert any(event["event_type"] == "position_close_skipped_already_flat" for event in events)


def test_live_close_records_confirmed_realized_pnl(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.income_history_response = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "4.25"}
    ]
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade"
    engine._wait_until_flat = lambda: True

    engine._close_position(engine._get_position(), "test")

    events = _journal_events(config.trade_journal_path)
    closed = [event for event in events if event["event_type"] == "position_closed"][0]
    assert closed["data"]["net_pnl"] == "4.25"
    assert closed["data"]["pnl_status"] == "confirmed_income_history"


def test_open_short_builds_sell_entry_order_in_dry_run(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.DRY_RUN)
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    engine._open_position(TradeAction.OPEN_SHORT, Decimal("100"), "test")

    events = _journal_events(config.trade_journal_path)
    dry_run = [event for event in events if event["event_type"] == "position_open_dry_run"][0]
    assert dry_run["data"]["action"] == "OPEN_SHORT"
    assert dry_run["data"]["side"] == "SELL"
    assert client.new_order_calls == []


def test_live_close_short_uses_buy_reduce_only_order(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.SHORT
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade"
    engine._wait_until_flat = lambda: True

    engine._close_position(engine._get_position(), "test")

    assert client.new_order_calls[0]["side"] == "BUY"
    assert client.new_order_calls[0]["reduceOnly"] == "true"


@pytest.mark.parametrize(
    "order_response",
    [
        {"status": "EXPIRED", "executedQty": "0"},
        {"status": "FILLED", "executedQty": "0.25"},
    ],
)
def test_live_close_does_not_confirm_incomplete_order_when_position_is_flat(
    config_factory, symbol_rules, order_response
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine._wait_until_flat = lambda: pytest.fail("unfilled order must not be confirmed")

    def unfilled_order(**params):
        client.new_order_calls.append(params)
        client.direction = PositionDirection.NONE
        client.quantity = Decimal("0")
        return order_response

    client.new_order = unfilled_order
    result = engine._close_position(engine._get_position(), "test")

    assert result.status.value == "UNCERTAIN"
    assert result.reason == "CLOSE_ORDER_FILL_NOT_CONFIRMED"
    assert engine.paused is True
    assert not any(
        event["event_type"] == "position_closed"
        for event in _journal_events(config.trade_journal_path)
    )


def test_unknown_close_does_not_treat_failed_position_read_as_flat(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.new_order_error = BinanceFuturesNetworkError("submit timeout")
    client.query_order_response = {"status": "FILLED", "executedQty": "0.5"}
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    original_position_risk = client.position_risk
    reads = 0

    def position_risk(symbol, *, timeout=None):
        nonlocal reads
        reads += 1
        if reads >= 3:
            raise BinanceFuturesNetworkError("position unavailable")
        return original_position_risk(symbol, timeout=timeout)

    client.position_risk = position_risk
    result = engine._close_position(engine._get_position(), "test")

    assert result.status.value == "UNCERTAIN"
    assert engine.paused is True
    assert not any(
        event["event_type"] in {"close_order_reconciled_flat", "position_closed"}
        for event in _journal_events(config.trade_journal_path)
    )


def test_unknown_close_requires_order_query_even_when_position_is_flat(
    config_factory, symbol_rules
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.new_order_error = BinanceFuturesNetworkError("submit timeout")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    original_position_risk = client.position_risk
    reads = 0

    def position_risk(symbol, *, timeout=None):
        nonlocal reads
        reads += 1
        if reads >= 3:
            client.direction = PositionDirection.NONE
            client.quantity = Decimal("0")
        return original_position_risk(symbol, timeout=timeout)

    client.position_risk = position_risk
    engine._query_order_for_reconcile = lambda _id: (None, "query unavailable")
    result = engine._close_position(engine._get_position(), "test")

    assert result.status.value == "UNCERTAIN"
    assert engine.paused is True
    assert not any(
        event["event_type"] == "position_closed"
        for event in _journal_events(config.trade_journal_path)
    )


def test_unknown_close_confirms_when_order_filled_and_position_flat(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.new_order_error = BinanceFuturesNetworkError("submit timeout")
    client.query_order_response = {
        "status": "FILLED",
        "executedQty": "0.5",
        "avgPrice": "101",
    }
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    original_position_risk = client.position_risk
    reads = 0

    def position_risk(symbol, *, timeout=None):
        nonlocal reads
        reads += 1
        if reads >= 3:
            client.direction = PositionDirection.NONE
            client.quantity = Decimal("0")
        return original_position_risk(symbol, timeout=timeout)

    client.position_risk = position_risk
    engine._wait_until_flat = lambda: True
    result = engine._close_position(engine._get_position(), "test")

    assert result.status.value == "CONFIRMED"
    assert result.diagnostics["order_response"]["executedQty"] == "0.5"
    assert engine.paused is False


def test_live_close_pauses_when_flat_confirmation_read_fails(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.SHORT
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    def unavailable():
        raise BinanceFuturesNetworkError("position unavailable")

    engine._wait_until_flat = unavailable
    result = engine._close_position(engine._get_position(), "test")

    assert result.status.value == "UNCERTAIN"
    assert result.diagnostics["confirmation_error"] == "position unavailable"
    assert engine.paused is True


def test_continuous_audit_repairs_missing_protections(config_factory, symbol_rules):
    config = config_factory(
        execution_mode=ExecutionMode.TESTNET_LIVE,
        protection_policy=ProtectionPolicy.AUTO_REPAIR,
    )
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.mark = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade"
    engine.position_opened_at = 1.0

    position = engine._get_position()
    position = engine._sync_local_position_state(position)
    engine._audit_open_position_protections(position, "test")

    assert engine.paused is False
    assert len(client.new_algo_order_calls) == 2


def test_continuous_audit_repairs_trade_plan_protections_preserving_source(
    config_factory,
    symbol_rules,
):
    config = config_factory(
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_exit_enabled=True,
        protection_policy=ProtectionPolicy.AUTO_REPAIR,
    )
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.mark = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade"
    engine.position_opened_at = 1.0

    position = engine._get_position()
    position = engine._sync_local_position_state(position)

    engine._create_protection_orders(
        position=position,
        reason="initial_trade_plan",
        trade_id="trade",
        planned_stop_price=Decimal("98"),
        planned_take_profit_price=Decimal("104"),
        trade_plan_id="tp_test",
    )

    assert engine.runtime_state is not None
    assert engine.runtime_state.protection_source == "TRADE_PLAN"
    assert engine.runtime_state.trade_plan_id == "tp_test"
    assert engine.runtime_state.planned_stop_price == Decimal("98.00")
    assert engine.runtime_state.planned_take_profit_price == Decimal("104.00")

    client.new_algo_order_calls.clear()
    client.open_algo_order_items.clear()

    engine._audit_open_position_protections(position, "governor_cycle")

    assert engine.paused is False
    assert len(client.new_algo_order_calls) == 2
    assert engine.runtime_state is not None
    assert engine.runtime_state.protection_source == "TRADE_PLAN"
    assert engine.runtime_state.trade_plan_id == "tp_test"
    assert engine.runtime_state.planned_stop_price == Decimal("98.00")
    assert engine.runtime_state.planned_take_profit_price == Decimal("104.00")

    events = _journal_events(config.trade_journal_path)
    repairs = [event for event in events if event["event_type"] == "protection_auto_repair"]
    created = [event for event in events if event["event_type"] == "protections_created"]

    assert repairs[-1]["data"]["protection_source"] == "TRADE_PLAN"
    assert repairs[-1]["data"]["trade_plan_id"] == "tp_test"
    assert created[-1]["data"]["protection_source"] == "TRADE_PLAN"
    assert created[-1]["data"]["trade_plan_id"] == "tp_test"
    assert Decimal(created[-1]["data"]["stop_price"]) == Decimal("98.00")
    assert Decimal(created[-1]["data"]["take_profit_price"]) == Decimal("104.00")


def test_engine_ignores_planned_quantity_when_dynamic_sizing_disabled(
    config_factory,
    symbol_rules,
):
    config = config_factory(
        execution_mode=ExecutionMode.DRY_RUN,
        dynamic_sizing_enabled=False,
        position_notional_usdt=20.0,
    )
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    engine._open_position(
        TradeAction.OPEN_LONG,
        Decimal("100"),
        "test",
        planned_quantity=Decimal("0.050"),
        trade_plan_id="tp_test",
    )

    events = _journal_events(config.trade_journal_path)
    selected = [event for event in events if event["event_type"] == "entry_quantity_selected"][0]
    assert selected["data"]["quantity_source"] == "RISK_MANAGER"
    assert Decimal(selected["data"]["quantity"]) == Decimal("0.200")
    assert selected["data"]["planned_quantity_applied"] is False
    assert selected["data"]["planned_quantity_ignored"] is True


def test_engine_uses_planned_quantity_when_dynamic_sizing_enabled(
    config_factory,
    symbol_rules,
):
    config = config_factory(
        execution_mode=ExecutionMode.DRY_RUN,
        dynamic_sizing_enabled=True,
        position_notional_usdt=20.0,
    )
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    engine._open_position(
        TradeAction.OPEN_LONG,
        Decimal("100"),
        "test",
        planned_quantity=Decimal("0.050"),
        trade_plan_id="tp_test",
    )

    events = _journal_events(config.trade_journal_path)
    selected = [event for event in events if event["event_type"] == "entry_quantity_selected"][0]
    dry_run = [event for event in events if event["event_type"] == "position_open_dry_run"][0]

    assert selected["data"]["quantity_source"] == "TRADE_PLAN"
    assert Decimal(selected["data"]["quantity"]) == Decimal("0.050")
    assert selected["data"]["planned_quantity_applied"] is True
    assert dry_run["data"]["quantity_source"] == "TRADE_PLAN"
    assert Decimal(dry_run["data"]["quantity"]) == Decimal("0.050")
    assert client.new_order_calls == []


def test_engine_reduce_only_sizing_never_increases_quantity(
    config_factory,
    symbol_rules,
):
    config = replace(
        config_factory(
            execution_mode=ExecutionMode.DRY_RUN,
            dynamic_sizing_enabled=True,
            position_notional_usdt=20.0,
        ),
        smart_sizing_mode=SmartSizingMode.REDUCE_ONLY,
        operational_module_mode_sources={
            MODULE_SMART_SIZING: ModuleModeSource.DESKTOP.value,
        },
    )
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    engine._open_position(
        TradeAction.OPEN_LONG,
        Decimal("100"),
        "test",
        planned_quantity=Decimal("0.500"),
        trade_plan_id="tp_test",
    )

    events = _journal_events(config.trade_journal_path)
    selected = [event for event in events if event["event_type"] == "entry_quantity_selected"][0]

    assert selected["data"]["quantity_source"] == "RISK_MANAGER"
    assert Decimal(selected["data"]["quantity"]) == Decimal("0.200")
    assert selected["data"]["planned_quantity_applied"] is False
    assert selected["data"]["planned_quantity_reduce_only_rejected"] is True


def test_engine_creates_planned_protections_when_dynamic_exit_enabled(
    config_factory,
    symbol_rules,
):
    config = config_factory(
        execution_mode=ExecutionMode.DRY_RUN,
        dynamic_exit_enabled=True,
    )
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.2"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    result = engine._create_protection_orders(
        position=position,
        reason="test",
        trade_id="trade",
        planned_stop_price=Decimal("98"),
        planned_take_profit_price=Decimal("104"),
        trade_plan_id="tp_test",
    )

    assert result["stop_price"] == Decimal("98.00")
    assert result["take_profit_price"] == Decimal("104.00")
    assert result["protection_source"] == "TRADE_PLAN"
    assert result["trade_plan_id"] == "tp_test"
    assert engine.runtime_state.protection_source == "TRADE_PLAN"
    assert engine.runtime_state.planned_stop_price == Decimal("98.00")
    assert engine.runtime_state.planned_take_profit_price == Decimal("104.00")


def test_engine_ignores_planned_protections_when_dynamic_exit_disabled(
    config_factory,
    symbol_rules,
):
    config = config_factory(
        execution_mode=ExecutionMode.DRY_RUN,
        dynamic_exit_enabled=False,
    )
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.2"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    result = engine._create_protection_orders(
        position=position,
        reason="test",
        trade_id="trade",
        planned_stop_price=Decimal("98"),
        planned_take_profit_price=Decimal("104"),
        trade_plan_id="tp_test",
    )

    assert result["stop_price"] == Decimal("99.00")
    assert result["take_profit_price"] == Decimal("102.00")
    assert result["protection_source"] == "STATIC_PROTECTION_MODEL"
    assert result["trade_plan_id"] == "tp_test"
    assert engine.runtime_state.trade_plan_id == "tp_test"


def test_auto_repair_skips_when_position_is_flat_before_repair(
    config_factory,
    symbol_rules,
):
    config = config_factory(
        execution_mode=ExecutionMode.TESTNET_LIVE,
        protection_policy=ProtectionPolicy.AUTO_REPAIR,
    )
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    stale_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.5"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )
    engine._save_state_for_position(
        position=stale_position,
        trade_id="trade-flat",
        entry_client_order_id="entry-flat",
    )

    engine._audit_open_position_protections(stale_position, "governor_cycle")

    assert client.new_algo_order_calls == []
    assert engine.runtime_state is None
    assert engine.paused is False
    events = _journal_events(config.trade_journal_path)
    skipped = [
        event
        for event in events
        if event["event_type"] == "protection_operation_skipped_position_flat"
    ]
    assert skipped
    assert skipped[-1]["data"]["stage"] == "before_protection_repair"


def test_protection_creation_revalidates_position_between_stop_and_take(
    config_factory,
    symbol_rules,
):
    class PositionClosesAfterStopClient(FakeFuturesClient):
        def new_algo_order(self, **params):
            response = super().new_algo_order(**params)
            if params.get("type") == "STOP_MARKET":
                self.direction = PositionDirection.NONE
                self.quantity = Decimal("0")
                self.entry_price = Decimal("0")
            return response

    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = PositionClosesAfterStopClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    position = engine._get_position()
    engine.active_trade_id = "trade-between"

    result = engine._create_protection_orders(
        position=position,
        reason="test_between_stop_and_take",
        trade_id="trade-between",
    )

    assert result["status"] == "SKIPPED_POSITION_FLAT"
    assert result["reason"] == "POSITION_FLAT_BETWEEN_STOP_AND_TAKE"
    assert [call["type"] for call in client.new_algo_order_calls] == ["STOP_MARKET"]
    assert len(client.cancel_algo_order_calls) == 1
    assert engine.runtime_state is None
    assert engine.paused is False


def test_auto_repair_preserves_valid_stop_and_only_creates_missing_take(
    config_factory,
    symbol_rules,
):
    config = config_factory(
        execution_mode=ExecutionMode.TESTNET_LIVE,
        protection_policy=ProtectionPolicy.AUTO_REPAIR,
    )
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.mark = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade-idempotent"
    position = engine._get_position()

    engine._create_protection_orders(
        position=position,
        reason="initial",
        trade_id="trade-idempotent",
    )
    stop_order = next(
        order for order in client.open_algo_order_items if order["orderType"] == "STOP_MARKET"
    )
    client.open_algo_order_items = [stop_order]
    client.new_algo_order_calls.clear()

    engine._audit_open_position_protections(position, "governor_cycle")
    engine._audit_open_position_protections(position, "governor_cycle")

    assert [call["type"] for call in client.new_algo_order_calls] == ["TAKE_PROFIT_MARKET"]
    assert client.cancel_algo_order_calls == []
    assert engine.runtime_state is not None
    assert engine.runtime_state.stop_client_algo_id == stop_order["clientAlgoId"]
    assert engine.runtime_state.take_client_algo_id is not None
    assert engine.paused is False


def test_auto_repair_reconciles_exchange_flat_error_without_pausing(
    config_factory,
    symbol_rules,
):
    class PositionFlattensDuringSubmissionClient(FakeFuturesClient):
        def new_algo_order(self, **params):
            if params.get("type") == "STOP_MARKET":
                self.direction = PositionDirection.NONE
                self.quantity = Decimal("0")
                self.entry_price = Decimal("0")
                raise BinanceFuturesAPIError(
                    status_code=400,
                    code=-4509,
                    message="TIF GTE can only be used with open positions",
                )
            return super().new_algo_order(**params)

    config = config_factory(
        execution_mode=ExecutionMode.TESTNET_LIVE,
        protection_policy=ProtectionPolicy.AUTO_REPAIR,
    )
    client = PositionFlattensDuringSubmissionClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade-flat-error"
    position = engine._get_position()

    result = engine._create_protection_orders(
        position=position,
        reason="repair_flat_error",
        trade_id="trade-flat-error",
        repair_key="stable-repair-key",
    )

    assert result["status"] == "SKIPPED_POSITION_FLAT"
    assert result["reason"] == "POSITION_FLAT_DURING_STOP_SUBMISSION"
    assert engine.runtime_state is None
    assert engine.paused is False
    events = _journal_events(config.trade_journal_path)
    reconciled = [
        event
        for event in events
        if event["event_type"] == "protection_submission_reconciled_position_flat"
    ]
    assert reconciled
    assert reconciled[-1]["data"]["error_code"] == -4509
