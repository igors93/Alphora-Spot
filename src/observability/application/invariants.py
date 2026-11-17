"""Invariant checking for observability."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from observability.domain.events import EventChannel, EventSeverity


def observe_invariant(
    condition: bool,
    name: str,
    *,
    component: str,
    severity: EventSeverity = EventSeverity.ERROR,
    **payload: Any,
) -> bool:
    """If condition is False, emit an INVARIANT event.

    Returns the condition value so callers can use it for branching.
    Never raises.
    """
    if not condition:
        _emit_invariant(name=name, component=component, severity=severity, payload=payload)
    return condition


def require_invariant(
    condition: bool,
    name: str,
    *,
    component: str,
    exception_factory: Callable[[], Exception],
    **payload: Any,
) -> None:
    """If condition is False, emit an INVARIANT event AND raise exception_factory().

    The invariant event is emitted before the exception is raised.
    """
    if not condition:
        _emit_invariant(
            name=name,
            component=component,
            severity=EventSeverity.CRITICAL,
            payload=payload,
        )
        raise exception_factory()


def _emit_invariant(
    *,
    name: str,
    component: str,
    severity: EventSeverity,
    payload: dict[str, Any],
) -> None:
    """Internal: emit the invariant violation event."""
    try:
        from observability.application.service import _SERVICE

        if _SERVICE is None:
            return
        settings = getattr(_SERVICE, "_settings", None)
        if settings is not None and not getattr(settings, "invariants_enabled", True):
            return

        # Increment invariant counter
        _SERVICE._health.increment("invariant_violations")

        _SERVICE.emit(
            "invariant_violated",
            channel=EventChannel.INVARIANT,
            severity=severity,
            component=component,
            category="system",
            invariant_name=name,
            **payload,
        )
    except Exception:
        pass
