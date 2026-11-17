"""Single owner of local position state and pending order intents.

The legacy runtime state file remains the on-disk position projection. Entry
and close intents are sidecars so a crash before a fill preserves order IDs
without changing the existing recovery file format.
"""

from typing import Generic, Protocol, TypeVar

from execution.entry_intent_store import CloseIntentStore, EntryIntentStore

StateT = TypeVar("StateT")


class PositionStateStore(Protocol[StateT]):
    def load(self) -> StateT | None: ...

    def save(self, state: StateT) -> None: ...

    def clear(self) -> None: ...


class PositionLedger(Generic[StateT]):
    def __init__(self, *, state_path: str, position_store: PositionStateStore[StateT]) -> None:
        self.position_store = position_store
        self.entry_intents = EntryIntentStore(state_path)
        self.close_intents = CloseIntentStore(state_path)
        self._position = position_store.load()

    @property
    def position(self) -> StateT | None:
        return self._position

    def load(self) -> StateT | None:
        self._position = self.position_store.load()
        return self._position

    def save(self, state: StateT) -> None:
        self.position_store.save(state)
        self._position = state

    def clear(self) -> None:
        self.position_store.clear()
        self._position = None

    def observe_local_state(self, state: StateT | None) -> None:
        """Set an injected in-memory snapshot for test and adapter compatibility."""
        self._position = state
