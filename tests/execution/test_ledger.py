from dataclasses import dataclass
from decimal import Decimal

import pytest

from domain.models import PositionDirection
from execution.ledger import PositionLedger
from futures.state import RuntimeState, RuntimeStateStore


@dataclass(frozen=True)
class PositionState:
    trade_id: str


class MemoryStore:
    def __init__(self, value=None) -> None:
        self.value = value
        self.fail_save = False
        self.fail_clear = False

    def load(self):
        return self.value

    def save(self, state):
        if self.fail_save:
            raise OSError("write failed")
        self.value = state

    def clear(self):
        if self.fail_clear:
            raise OSError("delete failed")
        self.value = None


def test_ledger_reads_existing_position_and_updates_only_after_write(tmp_path) -> None:
    prior = PositionState("old")
    store = MemoryStore(prior)
    ledger = PositionLedger[PositionState](
        state_path=str(tmp_path / "state.json"), position_store=store
    )
    assert ledger.position is prior

    store.fail_save = True
    with pytest.raises(OSError):
        ledger.save(PositionState("new"))
    assert ledger.position is prior

    store.fail_save = False
    updated = PositionState("new")
    ledger.save(updated)
    assert ledger.position is updated

    store.fail_clear = True
    with pytest.raises(OSError):
        ledger.clear()
    assert ledger.position is updated

    store.fail_clear = False
    ledger.clear()
    assert ledger.position is None


def test_entry_and_close_intents_have_distinct_paths(tmp_path) -> None:
    ledger = PositionLedger[PositionState](
        state_path=str(tmp_path / "state.json"), position_store=MemoryStore()
    )
    assert ledger.entry_intents.path != ledger.close_intents.path


def test_failed_runtime_state_flush_keeps_previous_ledger_projection(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state_btcusdt_testnet.json"
    prior = RuntimeState(
        active_trade_id="old",
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.5"),
    )
    store = RuntimeStateStore(str(path))
    store.save(prior)
    ledger = PositionLedger[RuntimeState](state_path=str(path), position_store=store)

    def fail_flush(_fd):
        raise OSError("disk flush failed")

    monkeypatch.setattr("futures.state.os.fsync", fail_flush)
    with pytest.raises(OSError, match="disk flush failed"):
        ledger.save(
            RuntimeState(
                active_trade_id="new",
                symbol="BTCUSDT",
                direction=PositionDirection.LONG,
                opened_at=2.0,
                entry_price=Decimal("101"),
                quantity=Decimal("0.6"),
            )
        )

    assert ledger.position == prior
    assert store.load() == prior
    assert not path.with_suffix(".json.tmp").exists()
