"""Observability health counters."""

from __future__ import annotations

import threading


class HealthCounters:
    """Thread-safe health counters for the observability subsystem."""

    __slots__ = (
        "events_enqueued",
        "events_written",
        "events_dropped",
        "write_failures",
        "sanitization_failures",
        "queue_high_watermark",
        "emergency_fallback_writes",
        "crash_reports_written",
        "invariant_violations",
        "hook_failures",
        "events_ignored_after_shutdown",
        "diagnostics_suppressed",
        "payloads_truncated",
        "retention_failures",
        "hook_restore_failures",
        "text_privacy_failures",
        "emergency_fallback_failures",
        "shutdown_flush_timeout",
        "_lock",
    )

    def __init__(self) -> None:
        self.events_enqueued: int = 0
        self.events_written: int = 0
        self.events_dropped: int = 0
        self.write_failures: int = 0
        self.sanitization_failures: int = 0
        self.queue_high_watermark: int = 0
        self.emergency_fallback_writes: int = 0
        self.crash_reports_written: int = 0
        self.invariant_violations: int = 0
        self.hook_failures: int = 0
        self.events_ignored_after_shutdown: int = 0
        self.diagnostics_suppressed: int = 0
        self.payloads_truncated: int = 0
        self.retention_failures: int = 0
        self.hook_restore_failures: int = 0
        self.text_privacy_failures: int = 0
        self.emergency_fallback_failures: int = 0
        self.shutdown_flush_timeout: int = 0
        self._lock: threading.Lock = threading.Lock()

    def increment(self, field_name: str, amount: int = 1) -> None:
        """Atomically increment a named counter. Silently ignores unknown fields."""
        with self._lock:
            try:
                current = getattr(self, field_name)
                setattr(self, field_name, current + amount)
            except AttributeError:
                pass

    def update_high_watermark(self, value: int) -> None:
        """Track the highest observed queue size."""
        with self._lock:
            if value > self.queue_high_watermark:
                self.queue_high_watermark = value

    def snapshot(self) -> dict[str, int]:
        """Return a point-in-time snapshot of all counters."""
        with self._lock:
            return {
                "events_enqueued": self.events_enqueued,
                "events_written": self.events_written,
                "events_dropped": self.events_dropped,
                "write_failures": self.write_failures,
                "sanitization_failures": self.sanitization_failures,
                "queue_high_watermark": self.queue_high_watermark,
                "emergency_fallback_writes": self.emergency_fallback_writes,
                "crash_reports_written": self.crash_reports_written,
                "invariant_violations": self.invariant_violations,
                "hook_failures": self.hook_failures,
                "events_ignored_after_shutdown": self.events_ignored_after_shutdown,
                "diagnostics_suppressed": self.diagnostics_suppressed,
                "payloads_truncated": self.payloads_truncated,
                "retention_failures": self.retention_failures,
                "hook_restore_failures": self.hook_restore_failures,
                "text_privacy_failures": self.text_privacy_failures,
                "emergency_fallback_failures": self.emergency_fallback_failures,
                "shutdown_flush_timeout": self.shutdown_flush_timeout,
            }
