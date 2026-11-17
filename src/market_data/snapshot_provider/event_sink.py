"""Snapshot engine EventSink adapter that routes operational events into Alphora's audit log."""

from __future__ import annotations

import logging
from typing import Any

from market_data.snapshot_provider import wire_names
from utils.events import EventChannel, observe_event

# Event types emitted by the snapshot engine (capability set 0.6.0) that carry operationally
# significant information worth recording. Unknown events are routed through a bounded,
# sanitized generic event so new engine events do not disappear silently.
_ROUTED_EVENT_TYPES = frozenset(
    {
        "source_circuit_open",
        "source_failed",
        "source_payload_rejected",
        "resource_resolved",
        "resource_published",
        "resource_publish_ignored",
        "resource_invalidated",
        "resource_refresh_scheduled",
        "resource_refresh_completed",
        "resource_refresh_failed",
        "snapshot_built",
        "snapshot_session_resolved",
        "snapshot_session_revalidated",
        "cache_bypassed",
        "cache_hit",
        "stale_fallback_used",
        # 0.6.0 additions
        "snapshot_acceptance_failed",
        "snapshot_consistency_failed",
        "source_transport_timeout_violated",
        "source_transport_budget_rejected",
        "payload_copy_deadline_exceeded",
        # Snapshot-engine events used by Alphora's try/safety paths.
        "snapshot_try_request_resolved",
        "source_dispatch_under_deadline_pressure",
    }
)

# Only these payload keys are safe to record — they have bounded cardinality.
_SAFE_PAYLOAD_KEYS = frozenset(
    {
        "resource",
        "resource_namespace",
        "resource_name",
        "source",
        "status",
        "reason",
        "error_type",
        "committed",
        "failed",
        "retained_previous",
        "candidate_authority_rank",
        "cached_authority_rank",
        "forced",
        "replaced",
        "strict",
        "force_refresh",
        "requested",
        "resolved",
        "refreshed",
        "affected",
        # 0.6.0 additions
        "snapshot_id",
        "violations",
        "resources",
        "phase",
        "state",
        "errors",
        "required_errors",
        "optional_errors",
        "acceptance_violations",
        "consistency_failed",
        "allowed_actions",
        "transport_timeout_seconds",
        "available_budget_seconds",
        "deadline_dispatch_grace_seconds",
        "gap_seconds",
    }
)

_LOGGER = logging.getLogger(__name__)
_MAX_PAYLOAD_VALUE_LENGTH = 160
_UNKNOWN_EVENT_SAMPLE_LIMIT = 20


def _sanitize(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in _SAFE_PAYLOAD_KEYS:
        val = payload.get(key)
        if val is not None and isinstance(val, (str, int, float, bool)):
            rendered = str(val)
            if len(rendered) > _MAX_PAYLOAD_VALUE_LENGTH:
                rendered = f"{rendered[:_MAX_PAYLOAD_VALUE_LENGTH]}..."
            result[key] = rendered
    return result


class SnapshotEventSink:
    """EventSink that routes snapshot engine operational events to the Alphora audit log.

    Implements the engine EventSink protocol (emit).
    Lifecycle is idempotent: calling start/stop multiple times is safe.
    Errors inside the sink are swallowed so they never propagate back into the engine.
    """

    def __init__(self, *, component: str = "market_data.snapshot_events") -> None:
        self._component = component
        self._active = True
        self._failure_count = 0
        self._unknown_event_count = 0

    def start(self) -> None:
        self._active = True

    def stop(self) -> None:
        self._active = False

    def close(self) -> None:
        self.stop()

    def emit(self, event_type: str, **payload: Any) -> None:
        if not self._active:
            return
        if event_type not in _ROUTED_EVENT_TYPES:
            self._unknown_event_count += 1
            if self._unknown_event_count > _UNKNOWN_EVENT_SAMPLE_LIMIT:
                return
            try:
                observe_event(
                    wire_names.UNKNOWN_ENGINE_EVENT,
                    category="market_data",
                    channel=EventChannel.AUDIT,
                    component=self._component,
                    unknown_event_count=self._unknown_event_count,
                    **{
                        wire_names.UNKNOWN_ENGINE_EVENT_TYPE_KEY: str(event_type)[
                            :_MAX_PAYLOAD_VALUE_LENGTH
                        ]
                    },
                    **_sanitize(payload),
                )
            except Exception as error:
                self._failure_count += 1
                _LOGGER.debug("Snapshot event sink failed: %s", error)
            return
        try:
            safe = _sanitize(payload)
            observe_event(
                f"{wire_names.ENGINE_EVENT_PREFIX}{event_type}",
                category="market_data",
                channel=EventChannel.AUDIT,
                component=self._component,
                **safe,
            )
        except Exception as error:
            self._failure_count += 1
            _LOGGER.debug("Snapshot event sink failed: %s", error)


__all__ = ["SnapshotEventSink"]
