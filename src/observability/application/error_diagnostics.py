"""Structured diagnostics that the owner of an error type attaches to error events.

Observability does not know the error types of other subsystems. A subsystem that raises
structured errors registers an extractor under a key; ``emit_error`` runs every registered
extractor on the error and stores each non-empty result under its key in ``error_info``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

ErrorDiagnosticsExtractor = Callable[[BaseException], Mapping[str, Any] | None]

_LOGGER = logging.getLogger(__name__)
_LOCK = threading.Lock()
_EXTRACTORS: dict[str, ErrorDiagnosticsExtractor] = {}


def register_error_diagnostics(key: str, extractor: ErrorDiagnosticsExtractor) -> None:
    """Attach ``extractor(error)`` to every error event under ``error_info[key]``."""

    with _LOCK:
        _EXTRACTORS[str(key)] = extractor


def unregister_error_diagnostics(key: str) -> None:
    with _LOCK:
        _EXTRACTORS.pop(str(key), None)


def collect_error_diagnostics(error: BaseException) -> dict[str, Any]:
    """Run every registered extractor; an extractor that fails is skipped, never raised."""

    with _LOCK:
        extractors = tuple(_EXTRACTORS.items())
    collected: dict[str, Any] = {}
    for key, extractor in extractors:
        try:
            diagnostics = extractor(error)
        except Exception:
            _LOGGER.debug("Error diagnostics extractor failed | key=%s", key, exc_info=True)
            continue
        if diagnostics:
            collected[key] = dict(diagnostics)
    return collected


__all__ = [
    "ErrorDiagnosticsExtractor",
    "collect_error_diagnostics",
    "register_error_diagnostics",
    "unregister_error_diagnostics",
]
