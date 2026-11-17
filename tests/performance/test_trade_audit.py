import json
from decimal import Decimal
from pathlib import Path

from config import ExecutionMode
from config.symbol_context import symbol_config_from_app_config
from domain.models import FuturesPosition, PositionDirection, TradeAction
from execution.trade_audit import TradeAuditRecorder
from futures.state import RuntimeState
from governor.execution_engine import ExecutionEngine
from tests.fakes import FakeFuturesClient
from tests.trading.test_engine_safety import make_engine


def _events(path):
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_trade_audit_recorder_writes_opened_event(tmp_path):
    path = tmp_path / "data" / "trade_audit.jsonl"
    recorder = TradeAuditRecorder(
        path,
        environment="TESTNET",
        execution_mode="PAPER_LIVE",
        operational_mode="GOVERNOR_SINGLE_SYMBOL",
    )

    recorder.record_open(
        symbol="BTCUSDT",
        trade_id="trade-1",
        trade_plan_id="tp-1",
        entry_time="2026-05-25T10:00:00+00:00",
        entry_reason="strategy ready",
        entry_price=Decimal("100"),
        quantity=Decimal("0.5"),
        direction="LONG",
    )

    events = _events(path)
    assert events[0]["event_type"] == "trade_audit_opened"
    assert events[0]["data"]["symbol"] == "BTCUSDT"
    assert events[0]["data"]["environment"] == "TESTNET"
    assert events[0]["data"]["execution_mode"] == "PAPER_LIVE"
    assert events[0]["data"]["operational_mode"] == "GOVERNOR_SINGLE_SYMBOL"


def test_trade_audit_recorder_writes_completed_event_with_pnl_and_timing(tmp_path):
    path = tmp_path / "data" / "trade_audit.jsonl"
    recorder = TradeAuditRecorder(
        path,
        environment="TESTNET",
        execution_mode="PAPER_LIVE",
        operational_mode="GOVERNOR_SINGLE_SYMBOL",
    )

    recorder.record_completed(
        symbol="BTCUSDT",
        trade_id="trade-1",
        trade_plan_id="tp-1",
        entry_time="2026-05-25T10:00:00+00:00",
        exit_time="2026-05-25T10:01:05+00:00",
        exit_reason="strategy close",
        strategy_module="strategies.test",
        entry_price=Decimal("100"),
        close_price=Decimal("101"),
        quantity=Decimal("0.5"),
        direction="LONG",
        gross_pnl=Decimal("0.5"),
        net_pnl=Decimal("0.42"),
        pnl_status="confirmed",
        pnl_source="trade_result",
    )

    completed = _events(path)[0]
    assert completed["event_type"] == "trade_audit_completed"
    assert completed["data"]["entry_time"] == "2026-05-25T10:00:00+00:00"
    assert completed["data"]["exit_time"] == "2026-05-25T10:01:05+00:00"
    assert completed["data"]["seconds_in_position"] == "65.0"
    assert completed["data"]["gross_pnl"] == "0.5"
    assert completed["data"]["net_pnl"] == "0.42"
    assert completed["data"]["exit_source"] == "STRATEGY"
    assert completed["data"]["exited_by_strategy"] is True
    assert completed["data"]["exited_by_stop"] is False


def test_completed_exit_source_overrides_cached_open_flags(tmp_path):
    path = tmp_path / "data" / "trade_audit.jsonl"
    recorder = TradeAuditRecorder(path, strategy_module="strategies.test")
    recorder.record_open(symbol="BTCUSDT", trade_id="trade-1")

    recorder.record_completed(
        symbol="BTCUSDT",
        trade_id="trade-1",
        exit_source="STRATEGY",
        net_pnl=Decimal("1"),
    )

    completed = _events(path)[-1]["data"]
    assert completed["exit_source"] == "STRATEGY"
    assert completed["exited_by_strategy"] is True
    assert completed["exited_by_stop"] is False


def test_trade_audit_single_file_receives_multiple_symbols(tmp_path):
    path = tmp_path / "data" / "trade_audit.jsonl"
    recorder = TradeAuditRecorder(path, environment="TESTNET", execution_mode="PAPER_LIVE")

    recorder.record_open(symbol="BTCUSDT", trade_id="btc")
    recorder.record_open(symbol="ETHUSDT", trade_id="eth")

    symbols = [event["data"]["symbol"] for event in _events(path)]
    assert symbols == ["BTCUSDT", "ETHUSDT"]


def test_trade_audit_single_file_receives_multiple_environments_and_modes(tmp_path):
    path = tmp_path / "data" / "trade_audit.jsonl"
    paper = TradeAuditRecorder(
        path,
        environment="TESTNET",
        execution_mode="PAPER_LIVE",
        operational_mode="GOVERNOR_SINGLE_SYMBOL",
    )
    live = TradeAuditRecorder(
        path,
        environment="REAL",
        execution_mode="REAL_LIVE",
        operational_mode="GOVERNOR_MULTI_SYMBOL",
    )

    paper.record_open(symbol="BTCUSDT", trade_id="paper")
    live.record_open(symbol="ETHUSDT", trade_id="real")

    rows = _events(path)
    assert rows[0]["data"]["environment"] == "TESTNET"
    assert rows[0]["data"]["execution_mode"] == "PAPER_LIVE"
    assert rows[0]["data"]["operational_mode"] == "GOVERNOR_SINGLE_SYMBOL"
    assert rows[1]["data"]["environment"] == "REAL"
    assert rows[1]["data"]["execution_mode"] == "REAL_LIVE"
    assert rows[1]["data"]["operational_mode"] == "GOVERNOR_MULTI_SYMBOL"


def test_trade_audit_exit_source_inference():
    assert (
        TradeAuditRecorder.infer_exit_source(
            reason="CLOSE_POSITION from strategy",
            strategy_class="ExampleStrategy",
        )
        == "STRATEGY"
    )
    assert (
        TradeAuditRecorder.infer_exit_source(reason="governor risk daily_loss") == "GOVERNOR_RISK"
    )
    assert TradeAuditRecorder.infer_exit_source(reason="recovery_close_on_boot") == "RECOVERY"


def test_trade_audit_write_failure_does_not_raise(tmp_path):
    class BrokenJournal:
        def append(self, *_args, **_kwargs):
            raise OSError("disk full")

    recorder = TradeAuditRecorder(tmp_path / "data" / "trade_audit.jsonl")
    recorder.journal = BrokenJournal()

    recorder.record_open(symbol="BTCUSDT", trade_id="trade-1")


def test_paper_trade_records_audit(config_factory):
    config = config_factory(
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_exit_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    execution = ExecutionEngine(base_config=config, client=FakeFuturesClient())
    heavy = _heavy_snapshot_for_execution(profile.symbol)

    execution.open_position(
        symbol_config=profile,
        action=TradeAction.OPEN_LONG,
        price=Decimal("100"),
        reason="paper open",
        heavy_snapshot=heavy,
        planned_stop_price=Decimal("98"),
        planned_take_profit_price=Decimal("104"),
        trade_plan_id="tp_paper",
    )
    position = execution.get_position(profile)
    execution.close_position(
        symbol_config=profile,
        position=position,
        reason="strategy close",
        price=Decimal("104"),
    )

    events = _events(Path(config.trade_audit_path))
    event_types = [event["event_type"] for event in events]
    assert "trade_audit_opened" in event_types
    assert "trade_audit_protected" in event_types
    assert "trade_audit_completed" in event_types
    completed = [event for event in events if event["event_type"] == "trade_audit_completed"][0]
    assert completed["data"]["trade_plan_id"] == "tp_paper"
    assert completed["data"]["net_pnl"] is not None
    assert completed["data"]["exit_source"] == "STRATEGY"


def test_testnet_live_close_records_audit_with_trade_plan_and_pnl(
    config_factory,
    symbol_rules,
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.income_history_response = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "4.25"}
    ]
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "trade_live"
    engine.position_opened_at = 1000.0
    engine.runtime_state = RuntimeState(
        active_trade_id="trade_live",
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        opened_at=1000.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.5"),
        trade_plan_id="tp_live",
        planned_stop_price=Decimal("98"),
        planned_take_profit_price=Decimal("104"),
    )
    engine._wait_until_flat = lambda: True

    engine._close_position(engine._get_position(), "testnet strategy close")

    completed = [
        event
        for event in _events(Path(config.trade_audit_path))
        if event["event_type"] == "trade_audit_completed"
    ][0]
    assert completed["data"]["trade_id"] == "trade_live"
    assert completed["data"]["trade_plan_id"] == "tp_live"
    assert completed["data"]["net_pnl"] == "4.25"
    assert completed["data"]["pnl_source"] == "binance_income_history"
    assert completed["data"]["execution_mode"] == "TESTNET_LIVE"


def test_governor_live_close_records_revalidated_quantity(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    execution = ExecutionEngine(base_config=config, client=client)
    profile = symbol_config_from_app_config(config)
    engine = execution._get_engine(profile)
    engine.rules = symbol_rules
    engine._wait_until_flat = lambda: True
    requested_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    result = execution.close_position(
        symbol_config=profile,
        position=requested_position,
        reason="testnet strategy close",
    )

    assert result.position_changed is True
    assert client.new_order_calls[0]["quantity"] == "0.5"
    close_requested = [
        event
        for event in _events(Path(config.trade_audit_path))
        if event["event_type"] == "trade_audit_close_requested"
    ]
    assert close_requested[-1]["data"]["quantity"] == "0.5"
    governor_closed = [
        event
        for event in _events(execution.governor_journal.path)
        if event["event_type"] == "close_position_confirmed"
    ]
    assert governor_closed[-1]["data"]["quantity"] == "0.5"


def _heavy_snapshot_for_execution(symbol):
    from domain.models import FuturesPosition, MarketSnapshot
    from futures.exchange_rules import extract_symbol_rules
    from governor.market_data_router import HeavyMarketSnapshot

    client = FakeFuturesClient()
    return HeavyMarketSnapshot(
        symbol=symbol,
        market=MarketSnapshot(
            symbol=symbol,
            price=Decimal("100"),
            prices=[Decimal("100")],
        ),
        mark_price=Decimal("100"),
        exchange_rules=extract_symbol_rules(client.exchange_info(), symbol),
        position=FuturesPosition(
            symbol=symbol,
            direction=PositionDirection.NONE,
            quantity=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        ),
        open_orders=[],
        open_algo_orders=[],
        account_info={},
        timestamp=1,
        data_age_seconds=0,
        stale=False,
    )
