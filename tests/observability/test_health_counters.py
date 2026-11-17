"""Tests for observability.domain.health.HealthCounters."""

from __future__ import annotations

import threading

from observability.domain.health import HealthCounters


def test_increment_known_field():
    h = HealthCounters()
    h.increment("events_enqueued")
    assert h.events_enqueued == 1


def test_increment_amount():
    h = HealthCounters()
    h.increment("events_dropped", 5)
    assert h.events_dropped == 5


def test_increment_unknown_field_does_not_raise():
    """Incrementing an unknown field must be silently ignored (regression guard)."""
    h = HealthCounters()
    h.increment("field_that_does_not_exist")


def test_increment_unknown_field_does_not_corrupt_state():
    """Valid counters remain correct after an invalid increment call."""
    h = HealthCounters()
    h.increment("events_enqueued", 3)
    h.increment("nonexistent_counter")
    assert h.events_enqueued == 3


def test_update_high_watermark():
    h = HealthCounters()
    h.update_high_watermark(10)
    h.update_high_watermark(5)
    h.update_high_watermark(20)
    assert h.queue_high_watermark == 20


def test_snapshot_contains_all_fields():
    h = HealthCounters()
    snap = h.snapshot()
    assert "events_enqueued" in snap
    assert "events_dropped" in snap
    assert "write_failures" in snap
    assert "emergency_fallback_writes" in snap
    assert "queue_high_watermark" in snap


def test_increment_thread_safe():
    h = HealthCounters()
    threads = [threading.Thread(target=lambda: h.increment("events_enqueued")) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert h.events_enqueued == 100
