from decimal import Decimal

from binance.futures_client import BinanceFuturesNetworkError
from config import ExecutionMode
from domain.models import PositionDirection
from domain.orders import OrderState
from execution.engine import FuturesTradingEngine
from futures.position_execution import PositionExecutionStatus
from futures.risk_manager import FuturesRiskManager
from tests.fakes import FakeFuturesClient


def _engine(config, client, symbol_rules) -> FuturesTradingEngine:
    engine = FuturesTradingEngine(
        config=config,
        client=client,
        risk_manager=FuturesRiskManager(
            position_notional_usdt=config.position_notional_usdt,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
        ),
    )
    engine.rules = symbol_rules
    return engine


def _open_client() -> FakeFuturesClient:
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    return client


def test_close_intent_is_durable_before_reduce_only_dispatch(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class ClosingClient(FakeFuturesClient):
        def new_order(self, **params):
            pending = engine.close_intent_store.load()
            assert pending is not None
            assert pending.lifecycle.state is OrderState.SUBMITTING
            assert pending.lifecycle.client_order_id == params["newClientOrderId"]
            assert params["reduceOnly"] == "true"
            self.direction = PositionDirection.NONE
            self.quantity = Decimal("0")
            return super().new_order(**params)

    client = ClosingClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    engine = _engine(config, client, symbol_rules)

    result = engine._close_position(engine._get_position(), "test")

    assert result.status is PositionExecutionStatus.CONFIRMED
    assert len(client.new_order_calls) == 1
    assert engine.close_intent_store.load() is None


def test_close_intent_write_failure_blocks_reduce_only_dispatch(
    config_factory, symbol_rules
) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = _open_client()
    engine = _engine(config, client, symbol_rules)

    def fail_begin(**_kwargs):
        raise OSError("disk unavailable")

    engine.close_intent_store.begin = fail_begin
    result = engine._close_position(engine._get_position(), "test")

    assert result.reason == "CLOSE_INTENT_PERSIST_FAILED"
    assert engine.paused
    assert client.new_order_calls == []


def test_uncertain_close_blocks_second_reduce_only_after_restart(
    config_factory, symbol_rules
) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = _open_client()
    client.new_order_error = BinanceFuturesNetworkError("timeout after possible acceptance")
    client.query_order_response = {"status": "NEW", "executedQty": "0"}
    engine = _engine(config, client, symbol_rules)

    first = engine._close_position(engine._get_position(), "test")
    assert first.status is PositionExecutionStatus.UNCERTAIN
    assert engine.close_intent_store.load().lifecycle.state is OrderState.UNKNOWN
    assert len(client.new_order_calls) == 1

    restarted = _engine(config, client, symbol_rules)
    restarted.recover()
    assert restarted.paused
    second = restarted._close_position(restarted._get_position(), "test")
    assert second.reason == "CLOSE_INTENT_PENDING"
    assert len(client.new_order_calls) == 1


def test_recovery_resolves_confirmed_flat_close(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.query_order_response = {"status": "FILLED", "executedQty": "0.5"}
    engine = _engine(config, client, symbol_rules)
    engine.close_intent_store.begin(
        symbol="BTCUSDT",
        side="SELL",
        quantity=Decimal("0.5"),
        trade_id="trade-1",
        client_order_id="CLOSE-1",
    )
    engine.close_intent_store.advance(OrderState.SUBMITTING)

    engine.recover()

    assert not engine.paused
    assert engine.close_intent_store.load() is None
    assert client.new_order_calls == []


def test_partial_close_stays_pending_and_pauses_after_restart(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class PartialClient(FakeFuturesClient):
        def new_order(self, **params):
            self.new_order_calls.append(params)
            self.quantity = Decimal("0.25")
            return {"status": "PARTIALLY_FILLED", "executedQty": "0.25"}

    client = PartialClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.query_order_response = {"status": "PARTIALLY_FILLED", "executedQty": "0.25"}
    engine = _engine(config, client, symbol_rules)

    result = engine._close_position(engine._get_position(), "test")
    assert result.status is PositionExecutionStatus.UNCERTAIN
    assert engine.close_intent_store.load().lifecycle.state is OrderState.UNKNOWN

    restarted = _engine(config, client, symbol_rules)
    restarted.recover()
    assert restarted.paused
    assert len(client.new_order_calls) == 1
