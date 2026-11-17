"""``ObservabilityConfig`` group: Governor cycle metrics and run audit retention."""

from __future__ import annotations

from typing import TypedDict

from config.env_readers import _get_bool, _get_env, _get_non_negative_float, _get_non_negative_int


class ObservabilityFields(TypedDict):
    governor_observability_enabled: bool
    governor_cycle_metrics_interval_seconds: float
    governor_slow_cycle_ms: float
    audit_runs_dir: str
    audit_retention_days: int
    audit_max_runs: int
    audit_jsonl_enabled: bool
    audit_text_logs_enabled: bool
    audit_full_traceback: bool


def load_observability_fields() -> ObservabilityFields:
    return {
        "governor_observability_enabled": _get_bool(
            "ALPHORA_GOVERNOR_OBSERVABILITY_ENABLED",
            True,
        ),
        "governor_cycle_metrics_interval_seconds": _get_non_negative_float(
            "ALPHORA_GOVERNOR_CYCLE_METRICS_INTERVAL_SECONDS",
            30.0,
        ),
        "governor_slow_cycle_ms": _get_non_negative_float(
            "ALPHORA_GOVERNOR_SLOW_CYCLE_MS",
            1500.0,
        ),
        "audit_runs_dir": _get_env("ALPHORA_AUDIT_RUNS_DIR", "runtime/runs"),
        "audit_retention_days": _get_non_negative_int("ALPHORA_AUDIT_RETENTION_DAYS", 30),
        "audit_max_runs": _get_non_negative_int("ALPHORA_AUDIT_MAX_RUNS", 100),
        "audit_jsonl_enabled": _get_bool("ALPHORA_AUDIT_JSONL_ENABLED", True),
        "audit_text_logs_enabled": _get_bool("ALPHORA_AUDIT_TEXT_LOGS_ENABLED", True),
        "audit_full_traceback": _get_bool("ALPHORA_AUDIT_FULL_TRACEBACK", False),
    }


__all__ = ["ObservabilityFields", "load_observability_fields"]
