"""Tests for ObservabilityService."""

from __future__ import annotations

from pathlib import Path

from observability.application.service import (
    ObservabilityService,
    observe_error,
    observe_event,
    observe_health,
    set_service,
)
from observability.config import ObservabilitySettings
from observability.domain.health import HealthCounters
from observability.infrastructure.jsonl_writer import JsonlWriter


def _make_service(tmp_path: Path) -> ObservabilityService:
    settings = ObservabilitySettings(
        runs_dir=tmp_path,
        queue_size=100,
        retention_days=1,
        max_runs=10,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=1.0,
        legacy_text_log_enabled=False,
        crash_hooks_enabled=False,
    )
    run_dir = tmp_path / "test-run"
    for d in ("events", "diagnostics", "diagnostics/crashes", "logs", "reports"):
        (run_dir / d).mkdir(parents=True, exist_ok=True)

    health = HealthCounters()
    writer = JsonlWriter(
        max_file_bytes=1024 * 1024,
        backup_count=3,
        flush_interval_seconds=0.01,
        fsync_errors=False,
    )

    from observability.infrastructure.queue_listener import QueueListener

    def _router(event, rd):
        from observability.application.service import _event_path_for, _event_to_dict

        path = _event_path_for(rd, event.channel, event.category)
        path.parent.mkdir(parents=True, exist_ok=True)
        writer.write(path, _event_to_dict(event))
        health.increment("events_written")

    listener = QueueListener(
        queue_size=100,
        run_dir=run_dir,
        writer=writer,
        health=health,
        shutdown_timeout_seconds=1.0,
        flush_interval_seconds=0.01,
        event_router=_router,
    )
    listener.start()

    return ObservabilityService(
        settings=settings,
        run_id="test-run",
        run_dir=run_dir,
        writer=writer,
        listener=listener,
        health=health,
    )


def test_observe_event_does_not_raise(tmp_path):
    """observe_event never raises even without a service."""
    observe_event("test_event", component="test", category="system")


def test_observe_error_does_not_raise(tmp_path):
    """observe_error never raises even without a service."""
    observe_error("test_error", error=ValueError("oops"), component="test")


def test_observe_health_does_not_raise(tmp_path):
    """observe_health never raises even without a service."""
    observe_health()


def test_service_emit_does_not_raise(tmp_path):
    """ObservabilityService.emit never raises."""
    svc = _make_service(tmp_path)
    set_service(svc)
    svc.emit("test_event", component="test", category="system")
    svc.shutdown()


def test_service_emit_error_does_not_raise(tmp_path):
    """ObservabilityService.emit_error never raises."""
    svc = _make_service(tmp_path)
    set_service(svc)
    svc.emit_error("test_error", error=RuntimeError("err"), component="test")
    svc.shutdown()


def test_service_emit_health_does_not_raise(tmp_path):
    """ObservabilityService.emit_health never raises."""
    svc = _make_service(tmp_path)
    set_service(svc)
    svc.emit_health()
    svc.shutdown()


def test_events_enqueued_after_init(tmp_path):
    """Events are enqueued and health counter incremented."""
    svc = _make_service(tmp_path)
    set_service(svc)
    svc.emit("test_event", component="test", category="system")
    svc.flush()
    snapshot = svc.health_snapshot()
    assert snapshot["events_enqueued"] >= 1
    svc.shutdown()


def test_service_shutdown_graceful(tmp_path):
    """Service shuts down without deadlock or error."""
    svc = _make_service(tmp_path)
    set_service(svc)
    svc.emit("before_shutdown", component="test", category="system")
    svc.shutdown()
    # Double-shutdown is idempotent
    svc.shutdown()


def test_health_snapshot_returns_counters(tmp_path):
    """health_snapshot returns expected keys."""
    svc = _make_service(tmp_path)
    snapshot = svc.health_snapshot()
    assert "events_enqueued" in snapshot
    assert "events_dropped" in snapshot
    assert "write_failures" in snapshot
    svc.shutdown()


def test_observe_event_with_singleton(tmp_path):
    """observe_event uses singleton service when set."""
    svc = _make_service(tmp_path)
    set_service(svc)
    observe_event("singleton_event", component="test", category="system")
    svc.flush()
    snapshot = svc.health_snapshot()
    assert snapshot["events_enqueued"] >= 1
    svc.shutdown()
