from decimal import Decimal

import pytest

from domain.orders import InvalidOrderTransition, OrderState
from execution.entry_intent_store import EntryIntentStore


def test_intent_is_recovered_after_restart_and_never_resubmitted(tmp_path) -> None:
    state_path = str(tmp_path / "state_BTCUSDT_TESTNET.json")
    store = EntryIntentStore(state_path)
    store.begin(
        symbol="BTCUSDT",
        side="BUY",
        quantity=Decimal("0.25"),
        trade_id="trade-1",
        client_order_id="ENTRY-1",
    )
    assert store.load().lifecycle.state is OrderState.PERSISTED

    store.advance(OrderState.SUBMITTING)
    recovered = EntryIntentStore(state_path).load()
    assert recovered.lifecycle.client_order_id == "ENTRY-1"
    assert recovered.quantity == Decimal("0.25")
    with pytest.raises(InvalidOrderTransition):
        store.advance(OrderState.SUBMITTING)
    with pytest.raises(ValueError, match="already exists"):
        store.begin(
            symbol="BTCUSDT",
            side="BUY",
            quantity=Decimal("0.25"),
            trade_id="trade-2",
            client_order_id="ENTRY-2",
        )


def test_corrupt_history_blocks_new_intent(tmp_path) -> None:
    store = EntryIntentStore(str(tmp_path / "state.json"))
    store.path.write_text('{"schema_version": 1, "history": ["SUBMITTING"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid entry intent"):
        store.load()


def test_clear_does_not_change_runtime_state_file(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"existing": true}', encoding="utf-8")
    store = EntryIntentStore(str(state_path))
    store.begin(
        symbol="BTCUSDT",
        side="SELL",
        quantity=Decimal("1"),
        trade_id="trade-1",
        client_order_id="ENTRY-1",
    )
    store.clear()
    assert store.load() is None
    assert state_path.read_text(encoding="utf-8") == '{"existing": true}'
