"""The single event API (utils.events): sinks decide where each channel goes (M7, ADR-006)."""

import json
from pathlib import Path

import pytest

import observability.application.service as service_module
import utils.audit_logger as audit_logger
from utils.events import (
    EventChannel,
    EventRecord,
    event_sinks,
    observe_error,
    observe_event,
    register_event_sink,
    unregister_event_sink,
)


class _CaptureSink:
    def __init__(self) -> None:
        self.records: list[EventRecord] = []

    def accepts(self, record: EventRecord) -> bool:
        return True

    def write(self, record: EventRecord) -> None:
        self.records.append(record)


class _FakeService:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.errors: list[tuple[str, BaseException, dict]] = []

    def emit(self, event_type, **kwargs):
        self.events.append((event_type, kwargs))

    def emit_error(self, event_type, *, error, **kwargs):
        self.errors.append((event_type, error, kwargs))


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    audit_logger.reset_for_testing(tmp_path)
    return tmp_path / audit_logger.get_run_id()


@pytest.fixture
def fake_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr(service_module, "_SERVICE", service)
    return service


def _audit_lines(run_dir: Path, category: str) -> list[dict]:
    """Events of a category, without the ones the lazy run initialization writes."""
    path = run_dir / "events" / f"{category}_events.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    startup = {"run_started", "operational_module_modes_loaded"}
    return [event for event in events if event["event_type"] not in startup]


def test_audit_and_service_sinks_are_registered():
    sinks = event_sinks()

    assert audit_logger.AUDIT_RUN_SINK in sinks
    assert service_module.SERVICE_EVENT_SINK in sinks


def test_audit_channel_goes_only_to_the_run_audit_trail(run_dir, fake_service):
    observe_event(
        "risk_check_blocked",
        component="tests.events",
        category="risk",
        channel=EventChannel.AUDIT,
        severity="warning",
        symbol="BTCUSDT",
    )

    (event,) = _audit_lines(run_dir, "risk")
    assert event["event_type"] == "risk_check_blocked"
    assert event["component"] == "tests.events"
    assert event["severity"] == "WARNING"
    assert event["symbol"] == "BTCUSDT"
    assert event["payload"] == {"symbol": "BTCUSDT"}
    assert fake_service.events == []


def test_diagnostic_channel_goes_only_to_the_service(run_dir, fake_service):
    observe_event("cache_refreshed", component="tests.events", category="market_data", size=3)

    assert _audit_lines(run_dir, "market_data") == []
    ((event_type, kwargs),) = fake_service.events
    assert event_type == "cache_refreshed"
    assert kwargs["channel"] is EventChannel.DIAGNOSTIC
    assert kwargs["size"] == 3


def test_audit_error_reaches_the_audit_trail_and_the_diagnostics(run_dir, fake_service):
    error = ValueError("boom")

    observe_error(
        "unhandled_exception",
        error=error,
        component="tests.events",
        category="system",
        channel=EventChannel.AUDIT,
        stage="boot",
    )

    (event,) = _audit_lines(run_dir, "system")
    assert event["severity"] == "ERROR"
    assert event["payload"]["error"] == {"type": "ValueError", "message": "boom"}
    assert event["payload"]["stage"] == "boot"
    ((event_type, raised, kwargs),) = fake_service.errors
    assert (event_type, raised, kwargs["stage"]) == ("unhandled_exception", error, "boot")


def test_diagnostic_error_stays_out_of_the_audit_trail(run_dir, fake_service):
    observe_error("parse_failed", error=RuntimeError("x"), component="tests.events")

    assert _audit_lines(run_dir, "system") == []
    assert len(fake_service.errors) == 1


def test_a_failing_sink_never_reaches_the_caller(run_dir):
    class _Broken:
        def accepts(self, record):
            return True

        def write(self, record):
            raise RuntimeError("sink down")

    broken, capture = _Broken(), _CaptureSink()
    register_event_sink(broken)
    register_event_sink(capture)
    try:
        observe_event("still_delivered", component="tests.events")
    finally:
        unregister_event_sink(broken)
        unregister_event_sink(capture)

    assert [record.event_type for record in capture.records] == ["still_delivered"]
