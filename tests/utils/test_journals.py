"""One TradeJournal per file: the registry the composition root owns (M7)."""

from pathlib import Path

from execution.trade_audit import TradeAuditRecorder
from utils.journals import JournalRegistry


def test_registry_returns_one_journal_per_file(tmp_path: Path):
    registry = JournalRegistry()

    first = registry.journal(tmp_path / "data" / "governor_events.jsonl")
    again = registry.journal(str(tmp_path / "data" / ".." / "data" / "governor_events.jsonl"))
    other = registry.journal(tmp_path / "data" / "scanner_scores.jsonl")

    assert first is again
    assert other is not first
    assert sorted(Path(path).name for path in registry.paths()) == [
        "governor_events.jsonl",
        "scanner_scores.jsonl",
    ]


def test_trade_audit_takes_its_journal_from_the_registry(tmp_path: Path):
    registry = JournalRegistry()
    path = tmp_path / "data" / "trade_audit.jsonl"

    recorder = TradeAuditRecorder(path, journals=registry)

    assert recorder.journal is registry.journal(path)
