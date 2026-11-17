from types import SimpleNamespace

from governor.control_requests import GovernorControlRequests


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type: str, **data):
        self.events.append((event_type, data))


def test_consume_governor_stop_request(tmp_path):
    journal = FakeJournal()
    control = GovernorControlRequests(
        runtime_dir=tmp_path,
        events_journal=journal,
    )

    stop_path = tmp_path / "governor_stop.request"
    stop_path.write_text("stop", encoding="utf-8")

    assert control.consume_governor_stop() is True
    assert not stop_path.exists()
    assert journal.events == [
        ("governor_stop_requested", {"path": str(stop_path)}),
    ]

    assert control.consume_governor_stop() is False


def test_consume_symbol_pause_resume_stop_requests(tmp_path):
    journal = FakeJournal()
    control = GovernorControlRequests(
        runtime_dir=tmp_path,
        events_journal=journal,
    )

    symbol_configs = [
        SimpleNamespace(symbol="BTCUSDT"),
        SimpleNamespace(symbol="ETHUSDT"),
        SimpleNamespace(symbol="SOLUSDT"),
    ]

    paused_symbols = {"ETHUSDT"}
    stopped_symbols = {"ETHUSDT"}

    pause_path = tmp_path / "pause_btcusdt.request"
    resume_path = tmp_path / "resume_ethusdt.request"
    stop_path = tmp_path / "stop_solusdt.request"

    pause_path.write_text("pause", encoding="utf-8")
    resume_path.write_text("resume", encoding="utf-8")
    stop_path.write_text("stop", encoding="utf-8")

    control.consume_symbol_requests(
        symbol_configs=symbol_configs,
        paused_symbols=paused_symbols,
        stopped_symbols=stopped_symbols,
    )

    assert paused_symbols == {"BTCUSDT"}
    assert stopped_symbols == {"SOLUSDT"}

    assert not pause_path.exists()
    assert not resume_path.exists()
    assert not stop_path.exists()

    assert journal.events == [
        ("symbol_paused", {"symbol": "BTCUSDT"}),
        ("symbol_resumed", {"symbol": "ETHUSDT"}),
        ("symbol_stopped", {"symbol": "SOLUSDT"}),
    ]
