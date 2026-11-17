"""Shared process run context for Alphora."""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: str
    parent_run_id: str | None = None


_RUN_CONTEXT: RunContext | None = None
_RUN_CONTEXT_LOCK = threading.Lock()


def initialize_run_context(
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    config: Any = None,
) -> RunContext:
    """Initialize the process-wide run context once."""
    global _RUN_CONTEXT

    with _RUN_CONTEXT_LOCK:
        if _RUN_CONTEXT is not None:
            return _RUN_CONTEXT

        resolved_run_id = _first_non_empty(
            run_id,
            os.getenv("ALPHORA_RUN_ID"),
            getattr(config, "run_id", None),
        )
        if resolved_run_id is None:
            resolved_run_id = uuid.uuid4().hex[:12]

        resolved_parent_run_id = _first_non_empty(
            parent_run_id,
            os.getenv("ALPHORA_PARENT_RUN_ID"),
            getattr(config, "parent_run_id", None),
        )
        _RUN_CONTEXT = RunContext(
            run_id=resolved_run_id,
            parent_run_id=resolved_parent_run_id,
        )
        return _RUN_CONTEXT


def get_run_context() -> RunContext:
    """Return the process-wide run context, creating it if needed."""
    ctx = _RUN_CONTEXT
    if ctx is not None:
        return ctx
    return initialize_run_context()


def get_run_id() -> str:
    """Return the shared process run_id."""
    return get_run_context().run_id


def reset_run_context_for_testing() -> None:
    """Clear the shared run context. Tests can then initialize a fresh one."""
    global _RUN_CONTEXT
    with _RUN_CONTEXT_LOCK:
        _RUN_CONTEXT = None


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
