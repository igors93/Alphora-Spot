"""The single event API of Alphora (migration M7, OPT-032).

Code of every layer emits with ``observe_event`` and ``observe_error``. The infrastructure
decides where each record goes by registering sinks, so this module depends on nothing but the
standard library and lives in the base layer:

- ``utils.audit_logger`` registers the run audit sink: records of the ``AUDIT`` channel go to
  ``runtime/runs/<run_id>/events/<category>_events.jsonl`` and to the run summary counters;
- ``observability`` registers the service sink: every other channel but ``TELEMETRY``, and every
  error, goes to the diagnostic files through the observability queue;
- ``TELEMETRY`` records feed in-process aggregators, such as the Governor stage telemetry.

A sink never lets an exception reach the caller, and neither does this module.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class EventChannel(str, Enum):
    AUDIT = "AUDIT"
    DIAGNOSTIC = "DIAGNOSTIC"
    INVARIANT = "INVARIANT"
    HEALTH = "HEALTH"
    CRASH = "CRASH"
    # In-process metrics (stage telemetry): read by sinks that aggregate, never written per event.
    TELEMETRY = "TELEMETRY"


class EventSeverity(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class EventRecord:
    """One emitted event, before any sink formats or sanitizes it."""

    event_type: str
    component: str
    category: str
    channel: EventChannel
    # Upper-case text: one of EventSeverity for new code; journals may carry other labels.
    severity: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    error: BaseException | None = None


class EventSink(Protocol):
    def accepts(self, record: EventRecord) -> bool: ...

    def write(self, record: EventRecord) -> None: ...


_LOGGER = logging.getLogger(__name__)
_LOCK = threading.Lock()
_SINKS: tuple[EventSink, ...] = ()
_DEFAULT_SINKS_INSTALLED = False


def register_event_sink(sink: EventSink) -> None:
    global _SINKS
    with _LOCK:
        if sink not in _SINKS:
            _SINKS = (*_SINKS, sink)


def unregister_event_sink(sink: EventSink) -> None:
    global _SINKS
    with _LOCK:
        _SINKS = tuple(item for item in _SINKS if item is not sink)


def event_sinks() -> tuple[EventSink, ...]:
    _install_default_sinks()
    return _SINKS


def observe_event(
    event_type: str,
    *,
    component: str,
    category: str = "system",
    channel: EventChannel = EventChannel.DIAGNOSTIC,
    severity: EventSeverity | str = EventSeverity.INFO,
    **payload: Any,
) -> None:
    """Emit an event to every sink that accepts it. Never raises."""

    _dispatch(
        EventRecord(
            event_type=str(event_type),
            component=str(component),
            category=str(category),
            channel=channel,
            severity=normalize_severity(severity),
            payload=payload,
        )
    )


def observe_error(
    event_type: str,
    *,
    error: BaseException,
    component: str,
    category: str = "system",
    channel: EventChannel = EventChannel.DIAGNOSTIC,
    **payload: Any,
) -> None:
    """Emit an error event; ``channel=AUDIT`` also records it in the run audit trail."""

    _dispatch(
        EventRecord(
            event_type=str(event_type),
            component=str(component),
            category=str(category),
            channel=channel,
            severity=EventSeverity.ERROR.value,
            payload=payload,
            error=error,
        )
    )


def observe_record(record: EventRecord) -> None:
    """Emit a record built by an adapter that already holds a free-form payload."""

    _dispatch(record)


def normalize_severity(severity: EventSeverity | str | None) -> str:
    if isinstance(severity, EventSeverity):
        return severity.value
    return str(severity or "INFO").upper().strip() or "INFO"


def _dispatch(record: EventRecord) -> None:
    for sink in event_sinks():
        try:
            if sink.accepts(record):
                sink.write(record)
        except Exception as error:
            try:
                _LOGGER.debug(
                    "Event sink failed | event_type=%s | error=%s", record.event_type, error
                )
            except Exception:
                pass


def _install_default_sinks() -> None:
    """Register the run audit sink, which every process has, on first use."""

    global _DEFAULT_SINKS_INSTALLED
    if _DEFAULT_SINKS_INSTALLED:
        return
    _DEFAULT_SINKS_INSTALLED = True
    from utils.audit_logger import AUDIT_RUN_SINK  # noqa: PLC0415 - same layer, avoids a cycle

    register_event_sink(AUDIT_RUN_SINK)


__all__ = [
    "EventChannel",
    "EventRecord",
    "EventSeverity",
    "EventSink",
    "event_sinks",
    "normalize_severity",
    "observe_error",
    "observe_event",
    "observe_record",
    "register_event_sink",
    "unregister_event_sink",
]
