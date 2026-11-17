"""ObservabilityService: the central hub for emitting structured events."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from observability.config import ObservabilitySettings
from observability.constants import SCHEMA_VERSION
from observability.domain.context import get_diagnostic_context
from observability.domain.events import (
    EventChannel,
    EventSeverity,
    ObservabilityEvent,
    ObservabilityLifecycleState,
)
from observability.domain.health import HealthCounters
from observability.infrastructure.jsonl_writer import JsonlWriter
from utils.events import EventRecord, register_event_sink

# The single event API lives in utils.events; these names stay importable from here.
from utils.events import observe_error as observe_error
from utils.events import observe_event as observe_event

_log = logging.getLogger(__name__)

# Module-level singleton
_SERVICE: ObservabilityService | NullObservabilityService | None = None
_SERVICE_LOCK = threading.Lock()
_LOW_PRIORITY_SEVERITIES = {EventSeverity.DEBUG, EventSeverity.INFO}
_HIGH_PRIORITY_SEVERITIES = {
    EventSeverity.WARNING,
    EventSeverity.ERROR,
    EventSeverity.CRITICAL,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_fingerprint(
    *,
    channel: EventChannel,
    component: str,
    event_type: str,
    error_type: str = "",
    top_frames: str = "",
    invariant_name: str = "",
) -> str:
    parts = "|".join(
        sorted(
            [
                channel.value,
                component,
                event_type,
                error_type,
                top_frames,
                invariant_name,
            ]
        )
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


def _get_task_name() -> str | None:
    try:
        task = asyncio.current_task()
        if task is not None:
            return task.get_name()
    except RuntimeError:
        pass
    return None


def _event_path_for(run_dir: Path, channel: EventChannel, category: str) -> Path:
    """Route event to the appropriate JSONL file."""
    diagnostics_dir = run_dir / "diagnostics"
    if channel == EventChannel.CRASH:
        return diagnostics_dir / "errors.jsonl"
    if channel == EventChannel.INVARIANT:
        return diagnostics_dir / "invariants.jsonl"
    if channel == EventChannel.HEALTH:
        return diagnostics_dir / "health.jsonl"
    if channel == EventChannel.DIAGNOSTIC:
        return diagnostics_dir / "diagnostics.jsonl"
    # AUDIT channel: goes to events/ by category
    return run_dir / "events" / f"{category}_events.jsonl"


def _event_to_dict(event: ObservabilityEvent) -> dict[str, Any]:
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "timestamp": event.timestamp,
        "monotonic_ns": event.monotonic_ns,
        "run_id": event.run_id,
        "channel": event.channel.value,
        "severity": event.severity.value,
        "category": event.category,
        "component": event.component,
        "event_type": event.event_type,
        "process_id": event.process_id,
        "thread_name": event.thread_name,
        "task_name": event.task_name,
        "context": dict(event.context),
        "payload": dict(event.payload),
        "error": event.error,
        "fingerprint": event.fingerprint,
    }


class ObservabilityService:
    """Central service for emitting observability events.

    After initialization, public methods NEVER raise exceptions to the caller.
    Exception: PrivacyBootFailure during boot (must prevent boot).
    """

    def __init__(
        self,
        *,
        settings: ObservabilitySettings,
        run_id: str,
        run_dir: Path,
        writer: JsonlWriter,
        listener: Any,  # QueueListener — avoid circular import
        health: HealthCounters,
    ) -> None:
        self._settings = settings
        self._run_id = run_id
        self._run_dir = run_dir
        self._writer = writer
        self._listener = listener
        self._health = health
        self._state = ObservabilityLifecycleState.RUNNING
        self._state_lock = threading.Lock()

    def emit(
        self,
        event_type: str,
        *,
        channel: EventChannel = EventChannel.DIAGNOSTIC,
        severity: EventSeverity = EventSeverity.INFO,
        component: str,
        category: str = "system",
        error_info: dict[str, Any] | None = None,
        fingerprint: str | None = None,
        **payload: Any,
    ) -> None:
        """Emit a structured event. Never raises."""
        try:
            self._emit_internal(
                event_type=event_type,
                channel=channel,
                severity=severity,
                component=component,
                category=category,
                error_info=error_info,
                fingerprint=fingerprint,
                payload=payload,
                allow_while_draining=False,
            )
        except Exception as exc:
            try:
                _log.warning(
                    "ObservabilityService.emit failed | event_type=%s | error=%s",
                    event_type,
                    exc,
                )
            except Exception:
                pass

    def emit_error(
        self,
        event_type: str,
        *,
        error: BaseException,
        component: str,
        category: str = "system",
        **payload: Any,
    ) -> None:
        """Emit an error event with structured exception info. Never raises."""
        try:
            from observability.application.diagnostics import (
                exception_diagnostics,
                top_traceback_frames,
            )
            from observability.application.error_diagnostics import collect_error_diagnostics
            from utils.logprivacy_adapter import sanitize_payload

            error_dict = exception_diagnostics(
                error,
                include_traceback=self._settings.full_traceback,
                max_frames=self._settings.max_traceback_frames,
                max_message_length=self._settings.max_message_length,
            )
            error_dict.update(collect_error_diagnostics(error))

            top_frames = top_traceback_frames(error, n=3)
            fp = _compute_fingerprint(
                channel=EventChannel.DIAGNOSTIC,
                component=component,
                event_type=event_type,
                error_type=type(error).__name__,
                top_frames=top_frames,
            )
            safe_payload = sanitize_payload(payload)
            if not isinstance(safe_payload, dict):
                safe_payload = {}
            self._emit_internal(
                event_type=event_type,
                channel=EventChannel.DIAGNOSTIC,
                severity=EventSeverity.ERROR,
                component=component,
                category=category,
                error_info=error_dict,
                fingerprint=fp,
                payload=safe_payload,
                allow_while_draining=False,
            )
        except Exception as exc:
            try:
                _log.warning(
                    "ObservabilityService.emit_error failed | event_type=%s | error=%s",
                    event_type,
                    exc,
                )
            except Exception:
                pass

    def emit_health(self) -> None:
        """Emit a health snapshot event. Never raises."""
        try:
            snapshot = self._health.snapshot()
            self._emit_internal(
                event_type="health_snapshot",
                channel=EventChannel.HEALTH,
                severity=EventSeverity.INFO,
                component="observability",
                category="system",
                error_info=None,
                fingerprint=None,
                payload=snapshot,
                allow_while_draining=False,
            )
        except Exception as exc:
            try:
                _log.warning("ObservabilityService.emit_health failed | error=%s", exc)
            except Exception:
                pass

    def flush(self) -> None:
        """Flush pending events. Never raises."""
        try:
            if self._listener is not None:
                self._listener.flush()
            if self._writer is not None:
                self._writer.flush_all()
        except Exception:
            pass

    def shutdown(self, *, status: str = "COMPLETED") -> None:
        """Initiate graceful shutdown. Idempotent."""
        if not self.begin_draining():
            return
        try:
            self._emit_internal(
                event_type="observability_shutdown",
                channel=EventChannel.DIAGNOSTIC,
                severity=EventSeverity.INFO,
                component="observability.bootstrap",
                category="system",
                error_info=None,
                fingerprint=None,
                payload={"status": status},
                allow_while_draining=True,
            )
            if self._listener is not None:
                self._listener.shutdown(timeout=self._settings.shutdown_timeout_seconds)
        except Exception as exc:
            _log.warning("Observability shutdown error | error=%s", exc)
        finally:
            with self._state_lock:
                self._state = ObservabilityLifecycleState.STOPPED

    def begin_draining(self) -> bool:
        """Move RUNNING to DRAINING. Returns False if already stopping."""
        with self._state_lock:
            if self._state is not ObservabilityLifecycleState.RUNNING:
                return False
            self._state = ObservabilityLifecycleState.DRAINING
            return True

    @property
    def lifecycle_state(self) -> ObservabilityLifecycleState:
        with self._state_lock:
            return self._state

    def health_snapshot(self) -> dict[str, Any]:
        """Return current health counters. Never raises."""
        try:
            return self._health.snapshot()
        except Exception:
            return {}

    def _emit_internal(
        self,
        *,
        event_type: str,
        channel: EventChannel,
        severity: EventSeverity,
        component: str,
        category: str,
        error_info: dict[str, Any] | None,
        fingerprint: str | None,
        payload: dict[str, Any],
        allow_while_draining: bool,
    ) -> None:
        from utils.logprivacy_adapter import sanitize_payload

        if channel is EventChannel.DIAGNOSTIC and not self._settings.diagnostics_enabled:
            self._health.increment("diagnostics_suppressed")
            return

        ctx = get_diagnostic_context()
        ctx_dict = ctx.as_dict()

        # Sanitize the whole payload dict at once so logprivacy can use key names for masking
        try:
            sanitized = sanitize_payload(payload)
            if isinstance(sanitized, dict):
                safe_payload = sanitized
            else:
                safe_payload = {}
                self._health.increment("sanitization_failures")
        except Exception:
            self._health.increment("sanitization_failures")
            safe_payload = {
                k: f"[SANITIZATION_FAILED:{type(v).__name__}]" for k, v in payload.items()
            }

        try:
            from observability.application.payload_limits import enforce_payload_limits

            safe_payload, truncated = enforce_payload_limits(
                safe_payload,
                max_payload_bytes=self._settings.max_payload_bytes,
            )
            if truncated:
                self._health.increment("payloads_truncated")
        except Exception:
            self._health.increment("payloads_truncated")
            safe_payload = {
                "payload_truncated": True,
                "max_payload_bytes": self._settings.max_payload_bytes,
            }

        event = ObservabilityEvent(
            schema_version=SCHEMA_VERSION,
            event_id=uuid.uuid4().hex,
            timestamp=_utc_now_iso(),
            monotonic_ns=time.monotonic_ns(),
            run_id=safe_payload.pop("run_id", None) or ctx_dict.get("run_id", self._run_id),
            channel=channel,
            severity=severity,
            category=category,
            component=component,
            event_type=event_type,
            process_id=os.getpid(),
            thread_name=threading.current_thread().name,
            task_name=_get_task_name(),
            context=ctx_dict,
            payload=safe_payload,
            error=error_info,
            fingerprint=fingerprint,
        )

        if not self._accept_event_for_current_state(event, allow_while_draining):
            return

        if self._listener is not None:
            self._listener.enqueue(event)
        else:
            # Direct write fallback (before listener starts)
            self._write_direct(event)

    def _accept_event_for_current_state(
        self,
        event: ObservabilityEvent,
        allow_while_draining: bool,
    ) -> bool:
        with self._state_lock:
            state = self._state

        if state is ObservabilityLifecycleState.RUNNING:
            return True
        if allow_while_draining and state is ObservabilityLifecycleState.DRAINING:
            return True
        if event.severity in _LOW_PRIORITY_SEVERITIES:
            self._health.increment("events_ignored_after_shutdown")
            return False
        if event.severity in _HIGH_PRIORITY_SEVERITIES:
            self._write_emergency_fallback(event)
            self._health.increment("emergency_fallback_writes")
            return False
        self._health.increment("events_ignored_after_shutdown")
        return False

    def _write_direct(self, event: ObservabilityEvent) -> None:
        """Write event directly (used before queue listener starts)."""
        path = _event_path_for(self._run_dir, event.channel, event.category)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._writer.write(
                path, _event_to_dict(event), fsync=_event_requires_fsync(self, event)
            )
            self._health.increment("events_written")
        except Exception as exc:
            self._health.increment("write_failures")
            _log.warning("Direct event write failed | error=%s", exc)

    def _write_emergency_fallback(self, event: ObservabilityEvent) -> None:
        try:
            if self._listener is not None and hasattr(self._listener, "write_emergency_fallback"):
                self._listener.write_emergency_fallback(event)
                return
            from observability.infrastructure.queue_listener import write_emergency_fallback

            write_emergency_fallback(event, health=self._health)
        except Exception:
            self._health.increment("emergency_fallback_failures")


class NullObservabilityService:
    """No-op service used when observability is disabled."""

    def __init__(self, *, settings: ObservabilitySettings, run_id: str) -> None:
        self._settings = settings
        self._run_id = run_id
        self._run_dir: Path | None = None
        self._health = HealthCounters()

    def emit(self, *args: Any, **kwargs: Any) -> None:
        return None

    def emit_error(self, *args: Any, **kwargs: Any) -> None:
        return None

    def emit_health(self) -> None:
        return None

    def flush(self) -> None:
        return None

    def shutdown(self, *, status: str = "COMPLETED") -> None:
        return None

    def health_snapshot(self) -> dict[str, Any]:
        return self._health.snapshot()

    @property
    def lifecycle_state(self) -> ObservabilityLifecycleState:
        return ObservabilityLifecycleState.STOPPED


def _route_event(event: ObservabilityEvent, run_dir: Path) -> None:
    """Router called by QueueListener to write events to appropriate files."""
    path = _event_path_for(run_dir, event.channel, event.category)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Get the service singleton's writer
    svc = get_service()
    if isinstance(svc, ObservabilityService):
        try:
            svc._writer.write(path, _event_to_dict(event), fsync=_event_requires_fsync(svc, event))
        except Exception:
            raise


def _event_requires_fsync(service: ObservabilityService, event: ObservabilityEvent) -> bool:
    if event.channel is EventChannel.CRASH:
        return bool(service._settings.fsync_crashes)
    return bool(
        service._settings.fsync_errors
        and event.severity in {EventSeverity.ERROR, EventSeverity.CRITICAL}
    )


def get_service() -> ObservabilityService | NullObservabilityService | None:
    """Return the active singleton service, or None."""
    return _SERVICE


def set_service(service: ObservabilityService | NullObservabilityService | None) -> None:
    """Set the active singleton service."""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service


# ---- Public module-level API ----


# AUDIT has its own sink (the run audit trail); TELEMETRY only feeds in-process aggregators.
_NOT_FOR_SERVICE = frozenset({EventChannel.AUDIT, EventChannel.TELEMETRY})


class _ServiceEventSink:
    """Every diagnostic channel, and every error, through the singleton service."""

    def accepts(self, record: EventRecord) -> bool:
        return record.error is not None or record.channel not in _NOT_FOR_SERVICE

    def write(self, record: EventRecord) -> None:
        svc = _SERVICE
        if svc is None:
            return
        if record.error is not None:
            svc.emit_error(
                record.event_type,
                error=record.error,
                component=record.component,
                category=record.category,
                **record.payload,
            )
            return
        svc.emit(
            record.event_type,
            channel=record.channel,
            severity=_severity(record.severity),
            component=record.component,
            category=record.category,
            **record.payload,
        )


def _severity(value: str) -> EventSeverity:
    try:
        return EventSeverity(value)
    except ValueError:
        return EventSeverity.INFO


SERVICE_EVENT_SINK = _ServiceEventSink()
register_event_sink(SERVICE_EVENT_SINK)


def observe_health(**payload: Any) -> None:
    """Emit a health event using the singleton service. Never raises."""
    svc = _SERVICE
    if svc is None:
        return
    svc.emit_health()
