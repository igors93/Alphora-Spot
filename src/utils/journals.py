"""One ``TradeJournal`` per file (migration M7).

The composition root of each process (the Governor's ``build_governor_components``, an ``ops``
command, a research job) creates one ``JournalRegistry`` and hands it down; every component asks
it for the journal of a path instead of building its own. The architecture ratchet counts
``TradeJournal(...)`` constructions outside this module.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from utils.trade_journal import TradeJournal


class JournalRegistry:
    """The owner of every journal file of a process: one ``TradeJournal`` per path."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._journals: dict[str, TradeJournal] = {}

    def journal(self, path: str | Path) -> TradeJournal:
        key = os.path.normcase(os.path.abspath(path))
        with self._lock:
            journal = self._journals.get(key)
            if journal is None:
                journal = TradeJournal(str(path))
                self._journals[key] = journal
            return journal

    def paths(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(str(journal.path) for journal in self._journals.values())


__all__ = ["JournalRegistry"]
