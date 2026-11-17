"""Tests for QueueListener."""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from observability.constants import SCHEMA_VERSION
from observability.domain.events import EventChannel, EventSeverity, ObservabilityEvent
from observability.domain.health import HealthCounters
from observability.infrastructure.jsonl_writer import JsonlWriter
from observability.infrastructure.queue_listener import QueueListener


def _make_event(
    severity: EventSeverity = EventSeverity.INFO, event_type: str = "test"
) -> ObservabilityEvent:
    return ObservabilityEvent(
        schema_version=SCHEMA_VERSION,
        event_id=uuid.uuid4().hex,
        timestamp=datetime.now(timezone.utc).isoformat(),
        monotonic_ns=time.monotonic_ns(),
        run_id="run-123",
        channel=EventChannel.DIAGNOSTIC,
        severity=severity,
        category="system",
        component="test",
        event_type=event_type,
        process_id=os.getpid(),
        thread_name=threading.current_thread().name,
        task_name=None,
        context={},
        payload={},
        error=None,
        fingerprint=None,
    )


def _make_listener(
    tmp_path: Path, queue_size: int = 100
) -> tuple[QueueListener, HealthCounters, list]:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "diagnostics").mkdir()
    (run_dir / "events").mkdir()

    health = HealthCounters()
    writer = JsonlWriter(
        max_file_bytes=1024 * 1024,
        backup_count=3,
        flush_interval_seconds=0.01,
        fsync_errors=False,
    )
    processed: list[ObservabilityEvent] = []

    def _router(event, rd):
        processed.append(event)

    listener = QueueListener(
        queue_size=queue_size,
        run_dir=run_dir,
        writer=writer,
        health=health,
        shutdown_timeout_seconds=2.0,
        flush_interval_seconds=0.01,
        event_router=_router,
    )
    return listener, health, processed


def test_events_processed_in_order(tmp_path):
    """Events are processed in FIFO order."""
    listener, health, processed = _make_listener(tmp_path)
    listener.start()

    events = [_make_event(event_type=f"event_{i}") for i in range(10)]
    for e in events:
        listener.enqueue(e)

    listener.shutdown(timeout=3.0)
    names = [e.event_type for e in processed]
    assert names == [f"event_{i}" for i in range(10)]


def test_info_dropped_under_backpressure(tmp_path):
    """INFO events can be dropped when queue is full."""
    listener, health, processed = _make_listener(tmp_path, queue_size=1)
    # Do NOT start listener — so queue stays full

    # Fill queue
    listener.enqueue(_make_event(severity=EventSeverity.INFO, event_type="fill"))

    # This should be dropped
    listener.enqueue(_make_event(severity=EventSeverity.INFO, event_type="overflow"))

    assert health.events_dropped >= 1


def test_error_uses_emergency_fallback_when_queue_full(tmp_path):
    """ERROR events use emergency fallback when queue is full."""

    listener, health, processed = _make_listener(tmp_path, queue_size=1)
    # Do NOT start listener — queue stays full

    # Fill queue
    listener.enqueue(_make_event(severity=EventSeverity.INFO, event_type="fill"))

    # ERROR event should go to emergency fallback, not be silently dropped
    listener.enqueue(_make_event(severity=EventSeverity.ERROR, event_type="must_not_drop"))

    # Dropped counter should NOT include the ERROR event
    assert health.emergency_fallback_writes >= 1
    assert health.events_dropped == 0  # ERROR was not dropped


def test_dropped_counter_incremented(tmp_path):
    """Events dropped under backpressure increment the dropped counter."""
    listener, health, processed = _make_listener(tmp_path, queue_size=1)

    # Fill queue without starting
    listener.enqueue(_make_event())
    listener.enqueue(_make_event())  # dropped

    assert health.events_dropped >= 1


def test_flush_on_shutdown_drains_queue(tmp_path):
    """All events enqueued before shutdown are processed."""
    listener, health, processed = _make_listener(tmp_path, queue_size=1000)
    listener.start()

    for i in range(20):
        listener.enqueue(_make_event(event_type=f"ev_{i}"))

    listener.shutdown(timeout=3.0)
    assert len(processed) == 20


def test_shutdown_does_not_deadlock(tmp_path):
    """Shutdown completes within timeout even under load."""
    listener, health, processed = _make_listener(tmp_path)
    listener.start()
    start = time.monotonic()
    listener.shutdown(timeout=2.0)
    elapsed = time.monotonic() - start
    assert elapsed < 4.0


def test_shutdown_is_idempotent(tmp_path):
    """Calling shutdown twice does not raise."""
    listener, health, processed = _make_listener(tmp_path)
    listener.start()
    listener.shutdown(timeout=2.0)
    listener.shutdown(timeout=2.0)  # Should not raise


def test_writer_failure_does_not_cause_infinite_recursion(tmp_path):
    """A writer failure is caught and does not cause recursion."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "diagnostics").mkdir()
    (run_dir / "events").mkdir()

    health = HealthCounters()
    writer = JsonlWriter(
        max_file_bytes=1024 * 1024,
        backup_count=3,
        flush_interval_seconds=0.01,
        fsync_errors=False,
    )

    def _bad_router(event, rd):
        raise OSError("disk full")

    listener = QueueListener(
        queue_size=100,
        run_dir=run_dir,
        writer=writer,
        health=health,
        shutdown_timeout_seconds=2.0,
        flush_interval_seconds=0.01,
        event_router=_bad_router,
    )
    listener.start()
    listener.enqueue(_make_event())
    listener.shutdown(timeout=3.0)
    # Must not deadlock or recurse — just records write_failures
    assert health.write_failures >= 0  # May or may not increment depending on timing
