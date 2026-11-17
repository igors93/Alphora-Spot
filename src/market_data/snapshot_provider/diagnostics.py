"""JSON-safe diagnostics of snapshots and of snapshot errors, for journals and logs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from market_data.snapshot_engine import Snapshot

_MESSAGE_LIMIT = 300


def snapshot_diagnostics_payload(snapshot: Snapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {"enabled": False}
    diagnostics = snapshot.diagnostics
    return {
        "enabled": True,
        "snapshot_id": snapshot.snapshot_id,
        "complete": snapshot.complete,
        "duration_ms": diagnostics.duration_ms,
        "requested_resources": diagnostics.requested_resources,
        "resolved_resources": diagnostics.resolved_resources,
        "failed_resources": diagnostics.failed_resources,
        "cache_hits": diagnostics.cache_hits,
        "cache_misses": diagnostics.cache_misses,
        "source_calls": diagnostics.source_calls,
        "source_calls_by_source": dict(diagnostics.source_calls_by_source),
        "batch_calls": diagnostics.batch_calls,
        "derived_calls": diagnostics.derived_calls,
        "coalesced_requests": diagnostics.coalesced_requests,
        "stale_values": diagnostics.stale_values,
        "refresh_scheduled": diagnostics.refresh_scheduled,
        "refresh_completed": diagnostics.refresh_completed,
        "refresh_failed": diagnostics.refresh_failed,
        "observation_skew_ms": diagnostics.observation_skew_ms,
    }


def snapshot_error_diagnostics_payload(error: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error_type": type(error).__name__,
        "message": str(error),
    }

    to_dict = getattr(error, "to_dict", None)

    if callable(to_dict):
        try:
            details = to_dict(max_message_length=_MESSAGE_LIMIT)
        except TypeError:
            details = to_dict()
        except Exception as diagnostics_error:
            payload["diagnostics_error"] = {
                "error_type": type(diagnostics_error).__name__,
                "message": str(diagnostics_error),
            }
        else:
            if isinstance(details, Mapping):
                payload["details"] = dict(details)
    else:
        fallback = _error_details_fallback(error)
        if fallback:
            payload["details"] = fallback

    partial_snapshot = getattr(error, "snapshot", None)

    if partial_snapshot is not None:
        try:
            payload["partial_snapshot"] = snapshot_diagnostics_payload(partial_snapshot)
        except Exception as diagnostics_error:
            payload["partial_snapshot_error"] = {
                "error_type": type(diagnostics_error).__name__,
                "message": str(diagnostics_error),
            }

    return payload


def snapshot_error_failure_summary(
    error: BaseException,
    *,
    max_items: int = 8,
) -> list[dict[str, Any]]:
    """Return a compact, JSON-safe list of the resources and sources that failed."""

    details = snapshot_error_diagnostics_payload(error).get("details")
    if not isinstance(details, Mapping):
        return []

    result: list[dict[str, Any]] = []
    for resource_error in tuple(details.get("errors", ()) or ()):
        if not isinstance(resource_error, Mapping):
            continue

        resource = str(resource_error.get("resource", ""))
        failures = tuple(resource_error.get("failures", ()) or ())
        if not failures:
            result.append(
                {
                    "resource": resource,
                    "source": "",
                    "error_type": str(resource_error.get("error_type", "")),
                    "message": str(resource_error.get("message", ""))[:_MESSAGE_LIMIT],
                    "attempts": None,
                }
            )
            continue

        for failure in failures:
            if not isinstance(failure, Mapping):
                continue
            result.append(
                {
                    "resource": resource,
                    "source": str(failure.get("source", "")),
                    "error_type": str(failure.get("error_type", "")),
                    "message": str(failure.get("message", ""))[:_MESSAGE_LIMIT],
                    "attempts": failure.get("attempts"),
                }
            )

    return result[: max(0, int(max_items))]


def snapshot_error_failure_summary_text(error: BaseException, *, max_items: int = 4) -> str:
    parts = []
    for item in snapshot_error_failure_summary(error, max_items=max_items):
        resource = item.get("resource") or "?"
        source = item.get("source") or "?"
        error_type = item.get("error_type") or "?"
        attempts = item.get("attempts")
        attempt_text = f", attempts={attempts}" if attempts is not None else ""
        parts.append(f"{resource} <- {source}: {error_type}{attempt_text}")
    return "; ".join(parts)


def _error_details_fallback(error: BaseException) -> dict[str, Any] | None:
    errors = getattr(error, "errors", None)
    if not isinstance(errors, Mapping):
        return None

    result: list[dict[str, Any]] = []
    for resource, resource_error in errors.items():
        failures = [
            {
                "source": str(getattr(failure, "source", "")),
                "error_type": str(getattr(failure, "error_type", "")),
                "message": str(getattr(failure, "message", ""))[:_MESSAGE_LIMIT],
                "attempts": getattr(failure, "attempts", None),
            }
            for failure in tuple(getattr(resource_error, "failures", ()) or ())
        ]
        result.append(
            {
                "resource": str(resource),
                "error_type": type(resource_error).__name__,
                "message": str(resource_error)[:_MESSAGE_LIMIT],
                "failures": failures,
            }
        )
    return {"errors": result}


__all__ = [
    "snapshot_diagnostics_payload",
    "snapshot_error_diagnostics_payload",
    "snapshot_error_failure_summary",
    "snapshot_error_failure_summary_text",
]
