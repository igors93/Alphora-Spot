"""Core observability event types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

# The channel and severity of the single event API (utils.events, migration M7).
from utils.events import EventChannel, EventSeverity


class ObservabilityLifecycleState(str, Enum):
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class ObservabilityEvent:
    schema_version: int
    event_id: str
    timestamp: str
    monotonic_ns: int
    run_id: str
    channel: EventChannel
    severity: EventSeverity
    category: str
    component: str
    event_type: str
    process_id: int
    thread_name: str
    task_name: str | None
    context: Mapping[str, Any]
    payload: Mapping[str, Any]
    error: Mapping[str, Any] | None
    fingerprint: str | None
