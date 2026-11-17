"""Exception serialization to structured diagnostics dict."""

from __future__ import annotations

import traceback
from typing import Any

from observability.constants import MAX_CHAIN_DEPTH


def exception_diagnostics(
    error: BaseException,
    *,
    include_traceback: bool,
    max_frames: int,
    max_message_length: int,
) -> dict[str, Any]:
    """Serialize exception to structured dict.

    Includes: type, module, message, traceback (limited frames),
    cause (__cause__), context (__context__), notes (__notes__),
    ExceptionGroup support.
    Chain depth is limited to MAX_CHAIN_DEPTH.
    """
    return _serialize_exception(
        error,
        include_traceback=include_traceback,
        max_frames=max_frames,
        max_message_length=max_message_length,
        depth=0,
    )


def _serialize_exception(
    error: BaseException,
    *,
    include_traceback: bool,
    max_frames: int,
    max_message_length: int,
    depth: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": type(error).__name__,
        "module": type(error).__module__,
        "message": str(error)[:max_message_length],
    }

    if include_traceback and error.__traceback__ is not None:
        frames = traceback.extract_tb(error.__traceback__)
        limited = frames[-max_frames:] if len(frames) > max_frames else frames
        result["traceback"] = [
            {
                "filename": f.filename,
                "lineno": f.lineno,
                "name": f.name,
                "line": (f.line or "").strip(),
            }
            for f in limited
        ]

    # PEP 678 notes
    notes = getattr(error, "__notes__", None)
    if notes:
        result["notes"] = list(notes)

    # ExceptionGroup support (Python 3.11+)
    exceptions = getattr(error, "exceptions", None)
    if isinstance(exceptions, (list, tuple)) and exceptions:
        if depth < MAX_CHAIN_DEPTH:
            result["exceptions"] = [
                _serialize_exception(
                    exc,
                    include_traceback=include_traceback,
                    max_frames=max_frames,
                    max_message_length=max_message_length,
                    depth=depth + 1,
                )
                for exc in exceptions
            ]

    # Chained exceptions
    if depth < MAX_CHAIN_DEPTH:
        cause = error.__cause__
        if cause is not None:
            result["cause"] = _serialize_exception(
                cause,
                include_traceback=include_traceback,
                max_frames=max_frames,
                max_message_length=max_message_length,
                depth=depth + 1,
            )

        context = error.__context__
        if context is not None and context is not cause:
            result["context"] = _serialize_exception(
                context,
                include_traceback=include_traceback,
                max_frames=max_frames,
                max_message_length=max_message_length,
                depth=depth + 1,
            )

    return result


def top_traceback_frames(error: BaseException, *, n: int = 3) -> str:
    """Return a string of the top N traceback frames for fingerprinting."""
    if error.__traceback__ is None:
        return ""
    frames = traceback.extract_tb(error.__traceback__)
    # Take the innermost N frames
    selected = frames[-n:] if len(frames) > n else frames
    return "|".join(f"{f.filename}:{f.lineno}:{f.name}" for f in selected)
