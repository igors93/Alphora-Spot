from decimal import Decimal

from binance.futures_client import BinanceFuturesNetworkError
from config import ExecutionMode
from domain.models import PositionDirection, TradeAction
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


def test_entry_intent_is_durable_before_exchange_dispatch(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class FillingClient(FakeFuturesClient):
        def new_order(self, **params):
            pending = engine.entry_intent_store.load()
            assert pending is not None
            assert pending.lifecycle.state is OrderState.SUBMITTING
            assert pending.lifecycle.client_order_id == params["newClientOrderId"]
            self.direction = PositionDirection.LONG
            self.quantity = Decimal(params["quantity"])
            self.entry_price = Decimal("100")
            return super().new_order(**params)

    client = FillingClient()
    engine = _engine(config, client, symbol_rules)
    result = engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")

    assert result.status is PositionExecutionStatus.CONFIRMED
    assert len(client.new_order_calls) == 1
    assert engine.entry_intent_store.load() is None


def test_intent_write_failure_blocks_exchange_dispatch(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    engine = _engine(config, client, symbol_rules)

    def fail_begin(**_kwargs):
        raise OSError("disk unavailable")

    engine.entry_intent_store.begin = fail_begin
    result = engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")

    assert result.reason == "ENTRY_INTENT_PERSIST_FAILED"
    assert engine.paused
    assert client.new_order_calls == []


def test_unknown_result_stays_pending_after_restart_and_blocks_duplicate(
    config_factory, symbol_rules
) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.new_order_error = BinanceFuturesNetworkError("timeout after possible acceptance")
    client.query_order_response = {"status": "NEW", "executedQty": "0"}
    engine = _engine(config, client, symbol_rules)

    first = engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")
    assert first.status is PositionExecutionStatus.UNCERTAIN
    assert engine.entry_intent_store.load().lifecycle.state is OrderState.UNKNOWN
    assert len(client.new_order_calls) == 1

    restarted = _engine(config, client, symbol_rules)
    restarted.recover()
    assert restarted.paused
    second = restarted._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")
    assert second.reason == "ENTRY_INTENT_PENDING"
    assert len(client.new_order_calls) == 1


def test_timeout_before_exchange_acceptance_never_resends_ambiguous_intent(
    config_factory, symbol_rules
) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.new_order_error = BinanceFuturesNetworkError("request timed out before acceptance")
    client.query_order_response = None
    engine = _engine(config, client, symbol_rules)

    first = engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")
    restarted = _engine(config, client, symbol_rules)
    restarted.recover()
    second = restarted._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")

    assert first.status is PositionExecutionStatus.UNCERTAIN
    assert second.reason == "ENTRY_INTENT_PENDING"
    assert len(client.new_order_calls) == 1
    assert restarted.entry_intent_store.load() is not None


def test_recovery_clears_confirmed_unfilled_intent(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.query_order_response = {"status": "CANCELED", "executedQty": "0"}
    engine = _engine(config, client, symbol_rules)
    engine.entry_intent_store.begin(
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.5"),
        trade_id="trade-1",
        client_order_id="ENTRY-1",
    )
    engine.entry_intent_store.advance(OrderState.SUBMITTING)

    engine.recover()

    assert not engine.paused
    assert engine.entry_intent_store.load() is None
    assert client.new_order_calls == []


def test_recovery_protects_accepted_entry_without_resending(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.query_order_response = {"status": "FILLED", "executedQty": "0.5"}
    engine = _engine(config, client, symbol_rules)
    engine.entry_intent_store.begin(
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.5"),
        trade_id="trade-1",
        client_order_id="ENTRY-1",
    )
    engine.entry_intent_store.advance(OrderState.SUBMITTING)

    engine.recover()

    assert not engine.paused
    assert client.new_order_calls == []
    assert engine.runtime_state is not None
    assert engine.runtime_state.entry_client_order_id == "ENTRY-1"
    assert engine.entry_intent_store.load() is None


def test_crash_before_submission_clears_only_persisted_intent(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    engine = _engine(config, client, symbol_rules)
    engine.entry_intent_store.begin(
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.5"),
        trade_id="trade-1",
        client_order_id="ENTRY-1",
    )

    restarted = _engine(config, client, symbol_rules)
    restarted.recover()

    assert not restarted.paused
    assert restarted.entry_intent_store.load() is None
    assert client.new_order_calls == []


def test_transition_write_failure_before_send_never_calls_exchange(
    config_factory, symbol_rules
) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    engine = _engine(config, client, symbol_rules)
    original_advance = engine.entry_intent_store.advance

    def fail_submitting(state):
        if state is OrderState.SUBMITTING:
            raise OSError("disk unavailable before send")
        return original_advance(state)

    engine.entry_intent_store.advance = fail_submitting
    result = engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")

    assert result.reason == "ENTRY_INTENT_PERSIST_FAILED"
    assert engine.entry_intent_store.load().lifecycle.state is OrderState.PERSISTED
    assert client.new_order_calls == []


def test_timeout_after_acceptance_recovers_fill_without_second_submission(
    config_factory, symbol_rules
) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class AcceptedThenTimedOut(FakeFuturesClient):
        def new_order(self, **params):
            self.new_order_calls.append(params)
            self.direction = PositionDirection.LONG
            self.quantity = Decimal(params["quantity"])
            self.entry_price = Decimal("100")
            raise BinanceFuturesNetworkError("response lost after acceptance")

    client = AcceptedThenTimedOut()
    client.query_order_response = {"status": "FILLED", "executedQty": "0.5"}
    engine = _engine(config, client, symbol_rules)

    result = engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")

    assert result.status is PositionExecutionStatus.CONFIRMED
    assert len(client.new_order_calls) == 1
    assert engine.runtime_state is not None
    assert engine.entry_intent_store.load() is None


def test_late_fill_after_reconnection_keeps_original_order_id(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.query_order_response = {"status": "NEW", "executedQty": "0"}
    engine = _engine(config, client, symbol_rules)
    engine.entry_intent_store.begin(
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.5"),
        trade_id="trade-1",
        client_order_id="ENTRY-1",
    )
    engine.entry_intent_store.advance(OrderState.SUBMITTING)

    first_restart = _engine(config, client, symbol_rules)
    first_restart.recover()
    assert first_restart.paused
    assert client.new_order_calls == []

    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.query_order_response = {"status": "FILLED", "executedQty": "0.5"}
    second_restart = _engine(config, client, symbol_rules)
    second_restart.recover()

    assert not second_restart.paused
    assert second_restart.runtime_state.entry_client_order_id == "ENTRY-1"
    assert client.new_order_calls == []


def test_partial_fill_cancels_remainder_before_protecting(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class PartialClient(FakeFuturesClient):
        def new_order(self, **params):
            self.new_order_calls.append(params)
            self.direction = PositionDirection.LONG
            self.quantity = Decimal("0.25")
            self.entry_price = Decimal("100")
            return {"status": "PARTIALLY_FILLED", "executedQty": "0.25"}

        def cancel_order(self, **params):
            self.cancel_order_calls.append(params)
            self.query_order_response = {"status": "CANCELED", "executedQty": "0.25"}
            return self.query_order_response

    client = PartialClient()
    client.query_order_response = {"status": "PARTIALLY_FILLED", "executedQty": "0.25"}
    engine = _engine(config, client, symbol_rules)

    result = engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")

    assert result.status is PositionExecutionStatus.CONFIRMED
    assert len(client.new_order_calls) == 1
    assert len(client.cancel_order_calls) == 1
    assert engine.runtime_state.quantity == Decimal("0.25")
    assert engine.entry_intent_store.load() is None
    assert len(client.new_algo_order_calls) == 2


def test_fill_during_cancel_protects_final_quantity(config_factory, symbol_rules) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class ConcurrentFillClient(FakeFuturesClient):
        def new_order(self, **params):
            self.new_order_calls.append(params)
            self.direction = PositionDirection.LONG
            self.quantity = Decimal("0.25")
            self.entry_price = Decimal("100")
            return {"status": "PARTIALLY_FILLED", "executedQty": "0.25"}

        def cancel_order(self, **params):
            self.cancel_order_calls.append(params)
            self.quantity = Decimal("0.5")
            self.query_order_response = {"status": "FILLED", "executedQty": "0.5"}
            return self.query_order_response

    client = ConcurrentFillClient()
    client.query_order_response = {"status": "PARTIALLY_FILLED", "executedQty": "0.25"}
    engine = _engine(config, client, symbol_rules)

    result = engine._open_position(TradeAction.OPEN_LONG, Decimal("100"), "test")

    assert result.status is PositionExecutionStatus.CONFIRMED
    assert engine.runtime_state.quantity == Decimal("0.5")
    assert len(client.new_order_calls) == 1
    assert engine.entry_intent_store.load() is None


def test_recovery_rejects_unrelated_position_when_entry_never_filled(
    config_factory, symbol_rules
) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class UnfilledClient(FakeFuturesClient):
        def cancel_order(self, **params):
            self.cancel_order_calls.append(params)
            self.query_order_response = {"status": "CANCELED", "executedQty": "0"}
            return self.query_order_response

    client = UnfilledClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.5")
    client.entry_price = Decimal("100")
    client.query_order_response = {"status": "NEW", "executedQty": "0"}
    engine = _engine(config, client, symbol_rules)
    engine.entry_intent_store.begin(
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.5"),
        trade_id="trade-1",
        client_order_id="ENTRY-1",
    )
    engine.entry_intent_store.advance(OrderState.SUBMITTING)

    engine.recover()

    assert engine.paused
    assert engine.entry_intent_store.load() is not None
    assert client.new_order_calls == []
    assert client.new_algo_order_calls == []


def test_restart_cancels_remaining_partial_entry_before_reprotecting(
    config_factory, symbol_rules
) -> None:
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class PendingPartialClient(FakeFuturesClient):
        def cancel_order(self, **params):
            self.cancel_order_calls.append(params)
            self.query_order_response = {"status": "CANCELED", "executedQty": "0.25"}
            return self.query_order_response

    client = PendingPartialClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.25")
    client.entry_price = Decimal("100")
    client.query_order_response = {"status": "PARTIALLY_FILLED", "executedQty": "0.25"}
    engine = _engine(config, client, symbol_rules)
    engine.entry_intent_store.begin(
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.5"),
        trade_id="trade-1",
        client_order_id="ENTRY-1",
    )
    engine.entry_intent_store.advance(OrderState.SUBMITTING)
    engine.entry_intent_store.advance(OrderState.ACKNOWLEDGED)
    engine.entry_intent_store.advance(OrderState.PARTIALLY_FILLED)

    engine.recover()

    assert not engine.paused
    assert len(client.cancel_order_calls) == 1
    assert client.new_order_calls == []
    assert engine.runtime_state.quantity == Decimal("0.25")
    assert engine.entry_intent_store.load() is None
