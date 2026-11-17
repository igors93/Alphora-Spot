"""Bootstrap and shutdown for the observability subsystem."""

from __future__ import annotations

import atexit
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from observability.config import ObservabilitySettings, settings_from_env
from observability.domain.events import EventChannel, EventSeverity
from observability.domain.exceptions import PrivacyBootFailure
from observability.domain.health import HealthCounters
from observability.infrastructure.atomic_files import atomic_write_json
from observability.infrastructure.jsonl_writer import JsonlWriter
from observability.infrastructure.queue_listener import QueueListener
from utils.run_context import initialize_run_context

if TYPE_CHECKING:
    from observability.application.lifecycle import ExceptionHooksHandle

_log = logging.getLogger(__name__)

_INIT_LOCK = threading.Lock()
_INITIALIZED = False
_SHUTDOWN = False
_STARTED_AT_UTC: datetime | None = None
_HOOKS_HANDLE: ExceptionHooksHandle | None = None


def initialize_observability(config: Any = None) -> Any:
    """Initialize the observability subsystem. Idempotent.

    Steps:
    1. Load ObservabilitySettings (from config or env)
    2. Initialize logprivacy (fail-closed on error -> PrivacyBootFailure)
    3. Create run directory structure
    4. Start QueueListener thread
    5. Configure text logging
    6. Install exception hooks (if crash_hooks_enabled)
    7. Emit observability_initialized event
    8. Register atexit shutdown
    """
    from observability.application.service import (
        NullObservabilityService,
        ObservabilityService,
        get_service,
        set_service,
    )

    global _INITIALIZED, _SHUTDOWN, _STARTED_AT_UTC, _HOOKS_HANDLE

    with _INIT_LOCK:
        existing = get_service()
        if existing is not None:
            return existing

        _SHUTDOWN = False

        # 1. Load settings
        settings = _load_settings(config)
        run_context = initialize_run_context(config=config)
        run_id = run_context.run_id

        if not settings.enabled:
            disabled_service = NullObservabilityService(settings=settings, run_id=run_id)
            set_service(disabled_service)
            _INITIALIZED = True
            return disabled_service

        _STARTED_AT_UTC = datetime.now(timezone.utc)

        # 2. Initialize privacy — fail-closed
        _boot_privacy(settings)

        # 3. Create run directory
        run_dir = _prepare_run_dir(settings, run_id)
        _cleanup_retention(settings, run_id)

        # 4. Create writer and health counters
        health = HealthCounters()
        writer = JsonlWriter(
            max_file_bytes=settings.max_file_bytes,
            backup_count=settings.backup_count,
            flush_interval_seconds=settings.flush_interval_seconds,
            fsync_errors=settings.fsync_errors,
        )

        # 5. Start QueueListener

        listener = QueueListener(
            queue_size=settings.queue_size,
            run_dir=run_dir,
            writer=writer,
            health=health,
            shutdown_timeout_seconds=settings.shutdown_timeout_seconds,
            flush_interval_seconds=settings.flush_interval_seconds,
            event_router=lambda event, rd: _route_and_write(event, rd, writer, settings),
        )
        listener.start()

        # 6. Configure text logging
        from observability.infrastructure.text_logging import configure_text_logging

        try:
            configure_text_logging(
                run_dir,
                run_id=run_id,
                legacy_enabled=settings.legacy_text_log_enabled,
            )
        except Exception:
            try:
                listener.shutdown(timeout=settings.shutdown_timeout_seconds)
            finally:
                writer.close()
            raise

        # 7. Create service
        service = ObservabilityService(
            settings=settings,
            run_id=run_id,
            run_dir=run_dir,
            writer=writer,
            listener=listener,
            health=health,
        )
        set_service(service)

        # 8. Install exception hooks
        if settings.crash_hooks_enabled:
            try:
                from observability.application.lifecycle import install_exception_hooks

                _HOOKS_HANDLE = install_exception_hooks(service)
            except Exception as exc:
                health.increment("hook_failures")
                _log.warning("Failed to install exception hooks | error=%s", exc)

        # 9. Emit initialized event
        service.emit(
            "observability_initialized",
            channel=EventChannel.DIAGNOSTIC,
            severity=EventSeverity.INFO,
            component="observability.bootstrap",
            category="system",
            run_id=run_id,
            run_dir=str(run_dir),
        )

        # 10. Register atexit
        atexit.register(shutdown_observability)

        _INITIALIZED = True

        return service


def shutdown_observability(*, status: str = "COMPLETED") -> None:
    """Shutdown. Idempotent.

    Steps:
    1. Block new INFO/DEBUG (handled by shutting_down flag)
    2. Accept ERROR/CRITICAL briefly
    3. Enqueue sentinel
    4. Wait for listener (shutdown_timeout_seconds)
    5. Flush
    6. Write observability_summary.json atomically
    7. Close handlers
    """
    from observability.application.service import get_service, set_service

    global _SHUTDOWN, _HOOKS_HANDLE

    with _INIT_LOCK:
        if _SHUTDOWN:
            return
        _SHUTDOWN = True

        service = get_service()
        if service is None:
            return

        # Shutdown listener (waits for drain)
        service.shutdown(status=status)

        # Write summary
        _write_summary(service, status=status)

        if _HOOKS_HANDLE is not None:
            try:
                _HOOKS_HANDLE.restore()
            except Exception:
                try:
                    service._health.increment("hook_restore_failures")
                except Exception:
                    pass
            finally:
                _HOOKS_HANDLE = None

        set_service(None)


def _load_settings(config: Any) -> ObservabilitySettings:
    """Load settings from config object or environment variables."""
    if config is not None and hasattr(config, "observability"):
        obs = config.observability
        if isinstance(obs, ObservabilitySettings):
            return obs
    return settings_from_env()


def _boot_privacy(settings: ObservabilitySettings) -> None:
    """Initialize privacy subsystem. Raises PrivacyBootFailure on error."""
    try:
        from utils.logprivacy_adapter import (
            ALPHORA_POLICY,  # noqa: F401
        )
    except PrivacyBootFailure:
        raise
    except Exception as exc:
        raise PrivacyBootFailure(f"Privacy initialization failed: {exc}") from exc


def _prepare_run_dir(settings: ObservabilitySettings, run_id: str) -> Path:
    """Create the run directory structure."""
    run_dir = settings.runs_dir / run_id
    for subdir in ("logs", "events", "diagnostics", "diagnostics/crashes", "snapshots", "reports"):
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)
    return run_dir


def _cleanup_retention(settings: ObservabilitySettings, run_id: str) -> None:
    try:
        from observability.infrastructure.retention import cleanup_old_runs

        cleanup_old_runs(
            settings.runs_dir,
            retention_days=settings.retention_days,
            max_runs=settings.max_runs,
            current_run_id=run_id,
        )
    except Exception as exc:
        _log.warning("Failed to clean old observability runs | error=%s", exc)


def _route_and_write(
    event: Any,
    run_dir: Path,
    writer: JsonlWriter,
    settings: ObservabilitySettings,
) -> None:
    """Route an event to the correct path and write it."""
    from observability.application.service import _event_path_for, _event_to_dict
    from observability.domain.events import EventChannel

    path = _event_path_for(run_dir, event.channel, event.category)
    path.parent.mkdir(parents=True, exist_ok=True)

    fsync = (event.channel is EventChannel.CRASH and settings.fsync_crashes) or (
        settings.fsync_errors and event.severity in {EventSeverity.ERROR, EventSeverity.CRITICAL}
    )
    writer.write(path, _event_to_dict(event), fsync=fsync)


def _write_summary(service: Any, *, status: str) -> None:
    """Write observability_summary.json atomically on shutdown."""
    try:
        finished_at = datetime.now(timezone.utc)
        started_at = _STARTED_AT_UTC or finished_at
        summary = {
            "run_id": getattr(service, "_run_id", ""),
            "started_at_utc": started_at.isoformat(),
            "finished_at_utc": finished_at.isoformat(),
            "duration_seconds": max(0.0, (finished_at - started_at).total_seconds()),
            "status": status,
            **service.health_snapshot(),
        }
        run_dir = getattr(service, "_run_dir", None)
        if run_dir is None:
            return
        path = run_dir / "reports" / "observability_summary.json"
        atomic_write_json(path, summary, fsync=False)
    except Exception as exc:
        _log.warning("Failed to write observability summary | error=%s", exc)


def reset_observability_bootstrap_for_testing() -> None:
    """Reset bootstrap globals for tests."""
    from observability.application.service import set_service

    global _INITIALIZED, _SHUTDOWN, _STARTED_AT_UTC, _HOOKS_HANDLE
    with _INIT_LOCK:
        if _HOOKS_HANDLE is not None:
            try:
                _HOOKS_HANDLE.restore()
            except Exception:
                pass
        _HOOKS_HANDLE = None
        _INITIALIZED = False
        _SHUTDOWN = False
        _STARTED_AT_UTC = None
        set_service(None)
