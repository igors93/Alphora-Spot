"""Compact diagnostics of snapshot engine errors for observability error events.

Registered with observability by ``market_data.snapshot_provider`` under
``wire_names.PAYLOAD_KEY``; see ``observability.register_error_diagnostics``.
"""

from __future__ import annotations

import logging
from typing import Any

from market_data.snapshot_engine import ERROR_DIAGNOSTICS_SCHEMA, ERROR_DIAGNOSTICS_SCHEMA_VERSION

_MAX_MESSAGE_LENGTH = 2_000
_MAX_ERRORS_SUMMARY = 3
_LOGGER = logging.getLogger(__name__)
_diagnostic_extraction_failures = 0


def _record_diagnostic_extraction_failure(stage: str) -> None:
    global _diagnostic_extraction_failures

    _diagnostic_extraction_failures += 1
    _LOGGER.debug(
        "Snapshot engine diagnostic extraction fallback used | stage=%s | failures=%s",
        stage,
        _diagnostic_extraction_failures,
        exc_info=True,
    )


def extract_engine_error_diagnostics(error: BaseException) -> dict[str, Any] | None:
    """Capability-detect snapshot engine structured errors and extract diagnostics.

    If the error has a .to_dict() method, use it.
    For SnapshotBuildError and ResourceResolutionError, preserve:
    resource, source, error_type, message, attempts, partial_snapshot_available.

    Returns None if no engine error structure is detected.
    """
    if not isinstance(error, BaseException):
        return None

    # Attempt structured extraction via to_dict() protocol
    to_dict = getattr(error, "to_dict", None)
    if callable(to_dict):
        try:
            raw = to_dict()
            if isinstance(raw, dict):
                return _normalize_engine_dict(raw, error_type=type(error).__name__)
        except Exception:
            _record_diagnostic_extraction_failure("to_dict")

    # Fallback: extract known fields by attribute inspection
    result = _extract_known_fields(error)
    if result:
        return result

    # Check for SnapshotBuildError (has .errors mapping)
    errors_attr = getattr(error, "errors", None)
    if isinstance(errors_attr, dict) and errors_attr:
        return _extract_snapshot_build_error(error, errors_attr)

    return None


def _normalize_engine_dict(raw: dict[str, Any], *, error_type: str) -> dict[str, Any]:
    """Normalize a to_dict() result to a standard shape.

    Handles both the versioned schema (v0.5.5+) and the legacy dict format.
    Consumers should validate schema/schema_version and ignore unknown additive fields.
    """
    result: dict[str, Any] = {}

    # Detect and validate the versioned diagnostics schema
    schema = raw.get("schema")
    schema_version = raw.get("schema_version")
    if schema == ERROR_DIAGNOSTICS_SCHEMA and schema_version == ERROR_DIAGNOSTICS_SCHEMA_VERSION:
        return _normalize_versioned_schema(raw)

    # Legacy format: extract known scalar fields
    for key in ("resource", "source", "error_type", "message", "attempts"):
        val = raw.get(key)
        if val is not None:
            if key == "message" and isinstance(val, str):
                val = val[:_MAX_MESSAGE_LENGTH]
            result[key] = val

    # partial_snapshot_available detection
    partial = raw.get("partial_snapshot_available")
    if partial is not None:
        result["partial_snapshot_available"] = bool(partial)
    elif "snapshot" in raw:
        result["partial_snapshot_available"] = raw["snapshot"] is not None

    result["error_type"] = result.get("error_type") or error_type
    return result


def _normalize_versioned_schema(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract diagnostics from a versioned engine diagnostics dict."""
    result: dict[str, Any] = {
        "schema": raw["schema"],
        "schema_version": raw["schema_version"],
    }

    error_type = raw.get("error_type")
    if error_type:
        result["error_type"] = str(error_type)

    message = raw.get("message")
    if message is not None:
        result["message"] = str(message)[:_MAX_MESSAGE_LENGTH]

    resource = raw.get("resource")
    if resource is not None:
        result["resource"] = str(resource)

    source = raw.get("source")
    if source is not None:
        result["source"] = str(source)

    attempts = raw.get("attempts")
    if attempts is not None:
        result["attempts"] = attempts

    # Canonical partial-snapshot field (v0.5.5+); fall back to legacy alias
    partial = raw.get("partial_snapshot_available")
    if partial is None:
        partial = raw.get("has_partial_snapshot")
    if partial is not None:
        result["partial_snapshot_available"] = bool(partial)

    # Per-resource error summary from SnapshotBuildError
    errors = raw.get("errors")
    if isinstance(errors, list) and errors:
        result["failed_resources"] = len(errors)
        summary = []
        for entry in errors[:_MAX_ERRORS_SUMMARY]:
            if not isinstance(entry, dict):
                continue
            item: dict[str, Any] = {}
            for field in ("resource", "error_type", "message"):
                val = entry.get(field)
                if val is not None:
                    item[field] = str(val)[:_MAX_MESSAGE_LENGTH] if field == "message" else str(val)
            if item:
                summary.append(item)
        if summary:
            result["errors_summary"] = summary

    # Per-source failure summary from ResourceResolutionError
    failures = raw.get("failures")
    if isinstance(failures, list) and failures:
        if "attempts" not in result:
            result["attempts"] = len(failures)
        summary_f = []
        for entry in failures[:_MAX_ERRORS_SUMMARY]:
            if not isinstance(entry, dict):
                continue
            item = {}
            for field in ("source", "error_type", "message", "attempts"):
                val = entry.get(field)
                if val is not None:
                    item[field] = str(val)[:_MAX_MESSAGE_LENGTH] if field == "message" else val
            if item:
                summary_f.append(item)
        if summary_f:
            result["failures_summary"] = summary_f

    return result


def _extract_known_fields(error: BaseException) -> dict[str, Any]:
    """Extract well-known engine error attributes."""
    result: dict[str, Any] = {}

    key_attr = getattr(error, "key", None)
    if key_attr is not None:
        result["resource"] = str(key_attr)

    failures_attr = getattr(error, "failures", None)
    if failures_attr is not None:
        try:
            result["attempts"] = len(failures_attr)
        except (TypeError, AttributeError):
            _record_diagnostic_extraction_failure("failures_len")
        # Try to extract source from first failure
        try:
            first = failures_attr[0] if failures_attr else None
            if first is not None:
                source = getattr(first, "source_key", None) or getattr(first, "source", None)
                if source is not None:
                    result["source"] = str(source)
        except (IndexError, TypeError):
            _record_diagnostic_extraction_failure("first_failure_source")

    if result:
        result["error_type"] = type(error).__name__
        message = str(error)
        result["message"] = message[:_MAX_MESSAGE_LENGTH]
    return result


def _extract_snapshot_build_error(
    error: BaseException,
    errors_dict: dict[Any, Any],
) -> dict[str, Any]:
    """Extract diagnostics from a SnapshotBuildError-like object."""
    result: dict[str, Any] = {
        "error_type": type(error).__name__,
        "message": str(error)[:_MAX_MESSAGE_LENGTH],
        "failed_resources": len(errors_dict),
    }

    snapshot_attr = getattr(error, "snapshot", None)
    result["partial_snapshot_available"] = snapshot_attr is not None

    # Summarize first failed resource
    try:
        first_key, first_err = next(iter(errors_dict.items()))
        result["resource"] = str(first_key)
        result["first_resource_error_type"] = type(first_err).__name__
        result["first_resource_error_message"] = str(first_err)[:_MAX_MESSAGE_LENGTH]
    except StopIteration:
        return result

    return result
