"""Background queue listener that drains observability events to writers."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from observability.constants import EMERGENCY_FALLBACK_PATH, SENTINEL
from observability.domain.events import EventSeverity, ObservabilityEvent
from observability.domain.health import HealthCounters

if TYPE_CHECKING:
    from observability.infrastructure.jsonl_writer import JsonlWriter

_log = logging.getLogger(__name__)

# Severities that must never be silently dropped
_HIGH_PRIORITY_SEVERITIES = {
    EventSeverity.WARNING,
    EventSeverity.ERROR,
    EventSeverity.CRITICAL,
}

# Recursion guard for emergency fallback
_EMERGENCY_WRITING = threading.local()

# How many drops to batch before emitting an aggregate event
_DROP_BATCH_SIZE = 1


class QueueListener:
    """Single background thread consuming events from a bounded queue.

    Backpressure policy:
    - DEBUG/INFO: may be dropped under pressure; dropped counter incremented.
    - WARNING/ERROR/CRITICAL: never silently dropped; use emergency fallback.
    - Aggregate drop event emitted periodically (not one per drop).

    Shutdown:
    - Accepts ERROR/CRITICAL briefly after shutdown signal.
    - SENTINEL sent to signal thread stop.
    - Waits shutdown_timeout_seconds for drain.
    - Flushes all writers on exit.
    """

    def __init__(
        self,
        *,
        queue_size: int,
        run_dir: Path,
        writer: JsonlWriter,
        health: HealthCounters,
        shutdown_timeout_seconds: float,
        flush_interval_seconds: float,
        health_interval_seconds: float = 60.0,
        event_router: Callable[[ObservabilityEvent, Path], None] | None = None,
    ) -> None:
        self._queue: queue.Queue[ObservabilityEvent | object] = queue.Queue(maxsize=queue_size)
        self._run_dir = run_dir
        self._writer = writer
        self._health = health
        self._shutdown_timeout = shutdown_timeout_seconds
        self._flush_interval = flush_interval_seconds
        self._health_interval = health_interval_seconds
        self._event_router = event_router
        self._thread: threading.Thread | None = None
        self._shutting_down = threading.Event()
        self._fully_stopped = threading.Event()
        self._pending_drops = 0
        self._drops_lock = threading.Lock()

    def start(self) -> None:
        """Start the background consumer thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="observability-queue-listener",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, event: ObservabilityEvent) -> None:
        """Enqueue an event. High-priority events use emergency fallback on full queue."""
        is_high_priority = event.severity in _HIGH_PRIORITY_SEVERITIES

        if self._shutting_down.is_set() or self._fully_stopped.is_set():
            if is_high_priority:
                self.write_emergency_fallback(event)
                self._health.increment("emergency_fallback_writes")
            else:
                self._health.increment("events_ignored_after_shutdown")
            return

        try:
            self._queue.put_nowait(event)
            self._health.increment("events_enqueued")
            # Track high watermark
            self._health.update_high_watermark(self._queue.qsize())
        except queue.Full:
            if is_high_priority:
                # High-priority: use emergency fallback, never silently drop
                self.write_emergency_fallback(event)
                self._health.increment("emergency_fallback_writes")
            else:
                # Low-priority: drop with counter
                with self._drops_lock:
                    self._pending_drops += 1
                self._health.increment("events_dropped")

    def flush(self) -> None:
        """Block until the queue is drained (best-effort)."""
        try:
            self._queue.join()
        except Exception:
            pass
        if self._writer is not None:
            try:
                self._writer.flush_all()
            except Exception:
                pass

    def shutdown(self, timeout: float) -> None:
        """Signal shutdown and wait for the listener thread to finish."""
        if self._fully_stopped.is_set():
            return
        self._shutting_down.set()
        # Put sentinel to wake thread
        try:
            self._queue.put_nowait(SENTINEL)
        except queue.Full:
            # If queue is full during shutdown, force drain with blocking put
            try:
                self._queue.put(SENTINEL, timeout=max(0.5, timeout / 2))
            except Exception:
                pass

        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                self._health.increment("shutdown_flush_timeout")
                _log.warning("Observability queue listener did not stop within timeout")

        self._fully_stopped.set()

        # Final flush
        try:
            self._writer.flush_all()
            self._writer.close()
        except Exception:
            pass

    def _run(self) -> None:
        """Main loop of the background thread."""
        last_flush = time.monotonic()
        last_health = time.monotonic()

        while True:
            try:
                timeout = self._flush_interval
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                self._maybe_flush(last_flush)
                last_flush = time.monotonic()
                # Same gate as the busy path below: without it, an idle queue
                # wrote a health snapshot every flush timeout (0.25s) instead
                # of every health_interval_seconds (~31k lines per session).
                if last_flush - last_health >= self._health_interval:
                    last_health = self._maybe_emit_health(last_health)
                self._maybe_emit_drops()
                continue

            if item is SENTINEL:
                self._queue.task_done()
                # Drain remaining items quickly
                self._drain_remaining()
                break

            if isinstance(item, ObservabilityEvent):
                self._process_event(item)

            try:
                self._queue.task_done()
            except ValueError:
                pass

            now = time.monotonic()
            if now - last_flush >= self._flush_interval:
                self._maybe_flush(last_flush)
                last_flush = now
            if now - last_health >= self._health_interval:
                last_health = self._maybe_emit_health(last_health)

            self._maybe_emit_drops()

    def _drain_remaining(self) -> None:
        """Drain remaining events after sentinel received."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is SENTINEL:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass
                continue
            if isinstance(item, ObservabilityEvent):
                self._process_event(item)
            try:
                self._queue.task_done()
            except ValueError:
                pass

    def _process_event(self, event: ObservabilityEvent) -> None:
        """Write an event to the appropriate JSONL file."""
        if self._event_router is not None:
            try:
                self._event_router(event, self._run_dir)
                self._health.increment("events_written")
            except Exception as exc:
                self._health.increment("write_failures")
                _log.warning("Event write failed | event_type=%s | error=%s", event.event_type, exc)

    def _maybe_flush(self, last_flush: float) -> None:
        try:
            self._writer.flush_all()
        except Exception:
            pass

    def _maybe_emit_health(self, last_health: float) -> float:
        # Health events written to health.jsonl periodically
        try:
            health_path = self._run_dir / "diagnostics" / "health.jsonl"
            snapshot = self._health.snapshot()
            self._writer.write(
                health_path,
                {
                    "timestamp": _utc_now_iso(),
                    "event_type": "health_snapshot",
                    **snapshot,
                },
            )
        except Exception:
            pass
        return time.monotonic()

    def _maybe_emit_drops(self) -> None:
        with self._drops_lock:
            count = self._pending_drops
            self._pending_drops = 0

        if count <= 0:
            return
        # Emit ONE aggregate drop event
        try:
            drop_path = self._run_dir / "diagnostics" / "dropped_events.jsonl"
            self._writer.write(
                drop_path,
                {
                    "timestamp": _utc_now_iso(),
                    "event_type": "observability_events_dropped",
                    "dropped_count": count,
                    "process_id": os.getpid(),
                },
            )
        except Exception:
            pass

    def write_emergency_fallback(self, event: ObservabilityEvent) -> None:
        """Write high-priority event to emergency fallback file.

        Protected against infinite recursion via thread-local guard.
        """
        write_emergency_fallback(event, health=self._health)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def write_emergency_fallback(
    event: ObservabilityEvent,
    *,
    health: HealthCounters | None = None,
) -> None:
    """Write a high-priority event to the process emergency fallback."""
    if getattr(_EMERGENCY_WRITING, "active", False):
        return
    _EMERGENCY_WRITING.active = True
    try:
        from utils.logprivacy_adapter import sanitize_text

        fallback = Path(EMERGENCY_FALLBACK_PATH)
        fallback.parent.mkdir(parents=True, exist_ok=True)
        line = (
            f"{sanitize_text(event.timestamp)} | {sanitize_text(event.severity.value)} | "
            f"{sanitize_text(event.channel.value)} | {sanitize_text(event.event_type)} | "
            f"{sanitize_text(event.component)}\n"
        )
        with fallback.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        if health is not None:
            health.increment("emergency_fallback_failures")
    finally:
        _EMERGENCY_WRITING.active = False
