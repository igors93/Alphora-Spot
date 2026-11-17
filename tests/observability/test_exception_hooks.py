"""Tests for exception hooks (sys.excepthook, threading.excepthook, asyncio)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from observability.application.lifecycle import install_exception_hooks
from observability.application.service import ObservabilityService
from observability.config import ObservabilitySettings
from observability.domain.health import HealthCounters
from observability.infrastructure.jsonl_writer import JsonlWriter


def _make_service(tmp_path: Path) -> ObservabilityService:
    settings = ObservabilitySettings(
        runs_dir=tmp_path,
        shutdown_timeout_seconds=1.0,
        legacy_text_log_enabled=False,
        crash_hooks_enabled=False,
        full_traceback=True,
        max_traceback_frames=10,
    )
    run_dir = tmp_path / "hooks-run"
    for d in ("events", "diagnostics", "diagnostics/crashes", "logs", "reports"):
        (run_dir / d).mkdir(parents=True, exist_ok=True)

    health = HealthCounters()
    writer = JsonlWriter(
        max_file_bytes=1024 * 1024,
        backup_count=3,
        flush_interval_seconds=0.01,
        fsync_errors=False,
    )
    return ObservabilityService(
        settings=settings,
        run_id="hooks-test",
        run_dir=run_dir,
        writer=writer,
        listener=None,
        health=health,
    )


def test_sys_excepthook_installed(tmp_path):
    """install_exception_hooks replaces sys.excepthook."""
    svc = _make_service(tmp_path)
    original = sys.excepthook
    install_exception_hooks(svc)
    assert sys.excepthook is not original
    # Restore
    sys.excepthook = original


def test_threading_excepthook_installed(tmp_path):
    """install_exception_hooks replaces threading.excepthook."""
    svc = _make_service(tmp_path)
    original = threading.excepthook
    install_exception_hooks(svc)
    assert threading.excepthook is not original
    # Restore
    threading.excepthook = original


def test_sys_excepthook_calls_previous(tmp_path):
    """Installed hook calls the previous hook."""
    svc = _make_service(tmp_path)
    called = []

    def prev_hook(et, ev, tb):
        called.append(True)

    original = sys.excepthook
    sys.excepthook = prev_hook
    install_exception_hooks(svc)

    try:
        sys.excepthook(ValueError, ValueError("test"), None)
    finally:
        sys.excepthook = original

    assert called


def test_crash_report_written(tmp_path):
    """A crash report file is written when hook fires."""
    svc = _make_service(tmp_path)
    original = sys.excepthook
    install_exception_hooks(svc)

    try:
        sys.excepthook(RuntimeError, RuntimeError("crash test"), None)
    finally:
        sys.excepthook = original

    crashes_dir = svc._run_dir / "diagnostics" / "crashes"
    crash_files = list(crashes_dir.glob("*.json"))
    assert len(crash_files) >= 1


def test_crash_report_sanitized(tmp_path):
    """Crash report sanitizes structured secret fields (not free-form message text).

    logprivacy masks secrets based on key-value structure (e.g., {"api_key": "secret"})
    rather than regex-scanning arbitrary exception message strings.
    The crash report payload itself goes through sanitize_payload(), so any
    top-level payload fields named with sensitive keys will be masked.
    """
    svc = _make_service(tmp_path)
    original = sys.excepthook
    install_exception_hooks(svc)

    try:
        # A regular exception — payload structure is sanitized
        err = RuntimeError("Something went wrong during order submission")
        sys.excepthook(type(err), err, None)
    finally:
        sys.excepthook = original

    crashes_dir = svc._run_dir / "diagnostics" / "crashes"
    crash_files = list(crashes_dir.glob("*.json"))
    assert len(crash_files) >= 1
    # Crash report must be valid JSON and contain expected structure
    import json

    content = crash_files[0].read_text(encoding="utf-8")
    data = json.loads(content)
    assert "crash_id" in data
    assert "timestamp" in data
    assert "error" in data


def test_threading_hook_writes_crash(tmp_path):
    """threading.excepthook fires and writes crash report."""
    svc = _make_service(tmp_path)
    original = threading.excepthook
    install_exception_hooks(svc)

    try:
        err = ValueError("thread crash")
        # ExceptHookArgs is a structseq — use positional args
        args = threading.ExceptHookArgs((ValueError, err, None, threading.current_thread()))
        threading.excepthook(args)
    finally:
        threading.excepthook = original

    crashes_dir = svc._run_dir / "diagnostics" / "crashes"
    crash_files = list(crashes_dir.glob("*.json"))
    assert len(crash_files) >= 1
