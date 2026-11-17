"""Exception hooks for crash detection and reporting."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from observability.application.service import ObservabilityService

_log = logging.getLogger(__name__)
_ACTIVE_HANDLE: ExceptionHooksHandle | None = None


@dataclass
class AsyncioHookHandle:
    loop: asyncio.AbstractEventLoop
    previous_handler: Any
    installed_handler: Any
    restored: bool = False

    def restore(self) -> None:
        if self.restored:
            return
        if self.loop.get_exception_handler() is self.installed_handler:
            self.loop.set_exception_handler(self.previous_handler)
        self.restored = True


@dataclass
class ExceptionHooksHandle:
    previous_sys_hook: Any
    previous_thread_hook: Any
    asyncio_loop: asyncio.AbstractEventLoop | None
    previous_asyncio_handler: Any
    installed_sys_hook: Any
    installed_thread_hook: Any
    installed_asyncio_handler: Any
    restored: bool = False

    def restore(self) -> None:
        if self.restored:
            return
        if sys.excepthook is self.installed_sys_hook:
            sys.excepthook = self.previous_sys_hook
        if threading.excepthook is self.installed_thread_hook:
            threading.excepthook = self.previous_thread_hook
        if self.asyncio_loop is not None and self.installed_asyncio_handler is not None:
            if self.asyncio_loop.get_exception_handler() is self.installed_asyncio_handler:
                self.asyncio_loop.set_exception_handler(self.previous_asyncio_handler)
        self.restored = True


def install_exception_hooks(service: ObservabilityService) -> ExceptionHooksHandle:
    """Install sys.excepthook, threading.excepthook, asyncio exception handler.

    Each hook:
    1. Writes crash report atomically.
    2. Calls the previous hook.
    Never suppresses fatal exceptions.
    """
    global _ACTIVE_HANDLE
    if _ACTIVE_HANDLE is not None:
        _ACTIVE_HANDLE.restore()
        _ACTIVE_HANDLE = None

    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    sys_hook = _make_sys_excepthook(service, previous_sys_hook)
    thread_hook = _make_threading_excepthook(service, previous_thread_hook)
    sys.excepthook = sys_hook
    threading.excepthook = thread_hook

    asyncio_loop: asyncio.AbstractEventLoop | None = None
    previous_asyncio_handler: Any = None
    installed_asyncio_handler: Any = None
    asyncio_handle: AsyncioHookHandle | None = None
    loop = _get_asyncio_loop()
    if loop is not None:
        try:
            asyncio_handle = install_asyncio_exception_handler(loop, service)
        except Exception:
            asyncio_handle = None
    if asyncio_handle is not None:
        asyncio_loop = asyncio_handle.loop
        previous_asyncio_handler = asyncio_handle.previous_handler
        installed_asyncio_handler = asyncio_handle.installed_handler

    handle = ExceptionHooksHandle(
        previous_sys_hook=previous_sys_hook,
        previous_thread_hook=previous_thread_hook,
        asyncio_loop=asyncio_loop,
        previous_asyncio_handler=previous_asyncio_handler,
        installed_sys_hook=sys_hook,
        installed_thread_hook=thread_hook,
        installed_asyncio_handler=installed_asyncio_handler,
    )
    _ACTIVE_HANDLE = handle
    return handle


def _make_sys_excepthook(service: ObservabilityService, prev_hook: Any) -> Any:
    def _hook(exc_type: type, exc_value: BaseException, exc_tb: Any) -> None:
        try:
            _handle_crash(service, exc_value, source="sys.excepthook")
        except Exception:
            pass
        if callable(prev_hook):
            prev_hook(exc_type, exc_value, exc_tb)

    return _hook


def _make_threading_excepthook(service: ObservabilityService, prev_hook: Any) -> Any:
    def _hook(args: threading.ExceptHookArgs) -> None:
        try:
            if args.exc_value is not None:
                _handle_crash(service, args.exc_value, source="threading.excepthook")
        except Exception:
            pass
        if callable(prev_hook):
            prev_hook(args)

    return _hook


def install_asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,
    service: ObservabilityService,
) -> AsyncioHookHandle:
    """Install an asyncio exception handler on a concrete loop."""
    prev_handler = loop.get_exception_handler()

    def _async_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        try:
            exc = context.get("exception")
            if exc is not None:
                _handle_crash(service, exc, source="asyncio.exception_handler")
        except Exception:
            pass
        if callable(prev_handler):
            prev_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_async_handler)
    return AsyncioHookHandle(
        loop=loop,
        previous_handler=prev_handler,
        installed_handler=_async_handler,
    )


def _get_asyncio_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def reset_exception_hooks_for_testing() -> None:
    """Restore active exception hooks installed by this module."""
    global _ACTIVE_HANDLE
    if _ACTIVE_HANDLE is not None:
        try:
            _ACTIVE_HANDLE.restore()
        finally:
            _ACTIVE_HANDLE = None


def _sanitize_error_dict_messages(error_dict: dict, sanitize_text_fn: Any) -> None:
    """Recursively sanitize 'message' fields in an exception dict."""
    if not isinstance(error_dict, dict):
        return
    if "message" in error_dict:
        try:
            error_dict["message"] = sanitize_text_fn(error_dict["message"])
        except Exception:
            pass
    for key in ("cause", "context"):
        if key in error_dict and isinstance(error_dict[key], dict):
            _sanitize_error_dict_messages(error_dict[key], sanitize_text_fn)
    for exc in error_dict.get("exceptions", []):
        if isinstance(exc, dict):
            _sanitize_error_dict_messages(exc, sanitize_text_fn)


def _handle_crash(
    service: ObservabilityService,
    error: BaseException,
    *,
    source: str,
) -> None:
    """Write crash report and emit crash event."""
    try:
        from observability.application.diagnostics import exception_diagnostics
        from observability.infrastructure.crash_writer import write_crash_report
        from utils.logprivacy_adapter import sanitize_payload

        crash_id = uuid.uuid4().hex[:16]
        timestamp = datetime.now(timezone.utc).isoformat()

        from utils.logprivacy_adapter import sanitize_text

        error_dict = exception_diagnostics(
            error,
            include_traceback=True,
            max_frames=service._settings.max_traceback_frames,
            max_message_length=service._settings.max_message_length,
        )

        # Sanitize the error message text before storing (it may contain secrets)
        _sanitize_error_dict_messages(error_dict, sanitize_text)

        payload: dict[str, Any] = {
            "crash_id": crash_id,
            "timestamp": timestamp,
            "source": source,
            "error": sanitize_payload(error_dict),
        }

        write_crash_report(
            service._run_dir,
            crash_id,
            payload,
            fsync=service._settings.fsync_crashes,
        )
        try:
            from observability.infrastructure.retention import trim_crash_reports

            trim_crash_reports(service._run_dir)
        except Exception:
            pass
        service._health.increment("crash_reports_written")

        # Also emit a structured CRASH event
        from observability.domain.events import EventChannel, EventSeverity

        service.emit(
            "unhandled_exception",
            channel=EventChannel.CRASH,
            severity=EventSeverity.CRITICAL,
            component="lifecycle",
            category="system",
            crash_id=crash_id,
            exception_source=source,
            error_type=type(error).__name__,
        )
    except Exception as exc:
        try:
            _log.warning("Crash handler failed | source=%s | error=%s", source, exc)
        except Exception:
            pass
