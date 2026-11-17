"""Versioned JSON-line contract for task subprocess output."""

from __future__ import annotations

import json
from typing import Any

SUBPROCESS_PROTOCOL_VERSION = 1


def encode_task_event(event_type: str, **payload: Any) -> str:
    return json.dumps(
        {**payload, "type": event_type, "version": SUBPROCESS_PROTOCOL_VERSION},
        ensure_ascii=False,
        default=str,
    )


def decode_task_event(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(event, dict)
        or type(event.get("version")) is not int
        or event["version"] != SUBPROCESS_PROTOCOL_VERSION
        or not isinstance(event.get("type"), str)
    ):
        return None
    return event
