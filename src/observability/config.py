"""Observability configuration dataclass and env-var loader."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ObservabilitySettings:
    enabled: bool = True
    runs_dir: Path = Path("runtime/runs")
    queue_size: int = 10_000
    retention_days: int = 30
    max_runs: int = 100
    max_file_bytes: int = 10 * 1024 * 1024
    backup_count: int = 10
    full_traceback: bool = False
    max_traceback_frames: int = 50
    max_message_length: int = 2_000
    max_payload_bytes: int = 256 * 1024
    flush_interval_seconds: float = 0.25
    shutdown_timeout_seconds: float = 5.0
    fsync_errors: bool = True
    fsync_crashes: bool = True
    legacy_text_log_enabled: bool = True
    crash_hooks_enabled: bool = True
    diagnostics_enabled: bool = True
    invariants_enabled: bool = True

    def __post_init__(self) -> None:
        checks = (
            ("queue_size", self.queue_size, 1),
            ("retention_days", self.retention_days, 0),
            ("max_runs", self.max_runs, 1),
            ("max_file_bytes", self.max_file_bytes, 1024),
            ("backup_count", self.backup_count, 1),
            ("max_traceback_frames", self.max_traceback_frames, 1),
            ("max_message_length", self.max_message_length, 100),
            ("max_payload_bytes", self.max_payload_bytes, 1024),
        )
        for field_name, value, minimum in checks:
            if int(value) < minimum:
                raise ValueError(f"{field_name} must be >= {minimum}")
        if self.flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


def _path_env(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return Path(raw.strip())


def settings_from_env() -> ObservabilitySettings:
    """Build ObservabilitySettings from environment variables."""
    return ObservabilitySettings(
        enabled=_bool_env("ALPHORA_OBSERVABILITY_ENABLED", True),
        runs_dir=_path_env("ALPHORA_OBSERVABILITY_RUNS_DIR", Path("runtime/runs")),
        queue_size=_int_env("ALPHORA_OBSERVABILITY_QUEUE_SIZE", 10_000),
        retention_days=_int_env("ALPHORA_OBSERVABILITY_RETENTION_DAYS", 30),
        max_runs=_int_env("ALPHORA_OBSERVABILITY_MAX_RUNS", 100),
        max_file_bytes=_int_env("ALPHORA_OBSERVABILITY_MAX_FILE_BYTES", 10 * 1024 * 1024),
        backup_count=_int_env("ALPHORA_OBSERVABILITY_BACKUP_COUNT", 10),
        full_traceback=_bool_env("ALPHORA_OBSERVABILITY_FULL_TRACEBACK", False),
        max_traceback_frames=_int_env("ALPHORA_OBSERVABILITY_MAX_TRACEBACK_FRAMES", 50),
        max_message_length=_int_env("ALPHORA_OBSERVABILITY_MAX_MESSAGE_LENGTH", 2_000),
        max_payload_bytes=_int_env("ALPHORA_OBSERVABILITY_MAX_PAYLOAD_BYTES", 256 * 1024),
        flush_interval_seconds=_float_env("ALPHORA_OBSERVABILITY_FLUSH_INTERVAL_SECONDS", 0.25),
        shutdown_timeout_seconds=_float_env("ALPHORA_OBSERVABILITY_SHUTDOWN_TIMEOUT_SECONDS", 5.0),
        fsync_errors=_bool_env("ALPHORA_OBSERVABILITY_FSYNC_ERRORS", True),
        fsync_crashes=_bool_env("ALPHORA_OBSERVABILITY_FSYNC_CRASHES", True),
        legacy_text_log_enabled=_bool_env("ALPHORA_OBSERVABILITY_LEGACY_TEXT_LOG_ENABLED", True),
        crash_hooks_enabled=_bool_env("ALPHORA_OBSERVABILITY_CRASH_HOOKS_ENABLED", True),
        diagnostics_enabled=_bool_env("ALPHORA_OBSERVABILITY_DIAGNOSTICS_ENABLED", True),
        invariants_enabled=_bool_env("ALPHORA_OBSERVABILITY_INVARIANTS_ENABLED", True),
    )
