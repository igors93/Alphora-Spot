from governor.events import SYMBOL_OPERATIONAL_STATE_TRANSITION
from governor.symbol_state_machine import (
    SymbolOperationalState,
    SymbolStateTracker,
)


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


class FailingJournal:
    def append(self, event_type, **data):
        raise RuntimeError("journal unavailable")


def test_symbol_state_tracker_records_transition_event():
    journal = FakeJournal()
    tracker = SymbolStateTracker(journal=journal)

    transition = tracker.transition(
        symbol="btcusdt",
        to_state=SymbolOperationalState.LIGHT_EVALUATED,
        reason="LIGHT_EVALUATION_STARTED",
        stage="LIGHT_SNAPSHOT",
        cycle_id="cycle-1",
        symbol_eval_id="cycle-1:BTCUSDT:000001",
        action="OPEN_LONG",
    )

    assert transition.from_state == "FLAT"
    assert transition.to_state == "LIGHT_EVALUATED"
    assert tracker.state_for("BTCUSDT") == "LIGHT_EVALUATED"
    event_type, payload = journal.events[0]
    assert event_type == SYMBOL_OPERATIONAL_STATE_TRANSITION
    assert payload["symbol"] == "BTCUSDT"
    assert payload["diagnostics"]["observational_only"] is True


def test_symbol_state_tracker_journal_failure_does_not_break_transition():
    tracker = SymbolStateTracker(journal=FailingJournal())

    transition = tracker.transition(
        symbol="BTCUSDT",
        to_state=SymbolOperationalState.BLOCKED,
        reason="test",
        stage="UNIT_TEST",
    )

    assert transition.to_state == "BLOCKED"
    assert tracker.state_for("BTCUSDT") == "BLOCKED"
