"""Payload size limits for structured observability events."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

PRIORITY_KEYS = (
    "symbol",
    "cycle_id",
    "candidate_id",
    "error_type",
    "resource",
    "source",
    "attempts",
)

_MAX_DEPTH = 8
_MAX_DICT_ITEMS = 64
_MAX_LIST_ITEMS = 128
_MAX_STRING_CHARS = 4096


def enforce_payload_limits(
    payload: dict[str, Any],
    *,
    max_payload_bytes: int,
) -> tuple[dict[str, Any], bool]:
    """Return a JSON-safe payload bounded by max_payload_bytes."""
    limited = _limit_value(payload, depth=0, seen=set())
    if not isinstance(limited, dict):
        limited = {"value": limited}

    original_size = _json_size_bytes(limited)
    if original_size <= max_payload_bytes:
        return limited, False

    truncated = {
        key: _limit_value(limited[key], depth=0, seen=set())
        for key in PRIORITY_KEYS
        if key in limited
    }
    truncated.update(
        {
            "payload_truncated": True,
            "original_size_bytes": original_size,
            "max_payload_bytes": max_payload_bytes,
        }
    )
    while _json_size_bytes(truncated) > max_payload_bytes and truncated:
        removable = [
            key for key in truncated if key not in {"payload_truncated", "max_payload_bytes"}
        ]
        if not removable:
            break
        truncated.pop(removable[-1], None)
    return truncated, True


def _limit_value(value: Any, *, depth: int, seen: set[int]) -> Any:
    if depth > _MAX_DEPTH:
        return "[TRUNCATED:depth]"

    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > _MAX_STRING_CHARS:
            return value[:_MAX_STRING_CHARS] + "[TRUNCATED:string]"
        return value

    value_id = id(value)
    if value_id in seen:
        return "[TRUNCATED:cycle]"

    if isinstance(value, Mapping):
        seen.add(value_id)
        try:
            items = list(value.items())
            limited_items = items[:_MAX_DICT_ITEMS]
            result = {
                _safe_key(key): _limit_value(item, depth=depth + 1, seen=seen)
                for key, item in limited_items
            }
            if len(items) > _MAX_DICT_ITEMS:
                result["payload_items_truncated"] = len(items) - _MAX_DICT_ITEMS
            return result
        finally:
            seen.discard(value_id)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        seen.add(value_id)
        try:
            items = list(value)
            list_result: list[Any] = [
                _limit_value(item, depth=depth + 1, seen=seen) for item in items[:_MAX_LIST_ITEMS]
            ]
            if len(items) > _MAX_LIST_ITEMS:
                list_result.append({"payload_items_truncated": len(items) - _MAX_LIST_ITEMS})
            return list_result
        finally:
            seen.discard(value_id)

    return f"[UNSUPPORTED:{type(value).__name__}]"


def _safe_key(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return f"[UNSUPPORTED_KEY:{type(value).__name__}]"


def _json_size_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))
