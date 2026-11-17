from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from observability.application.service import NullObservabilityService, get_service
from observability.bootstrap import initialize_observability, shutdown_observability
from observability.config import ObservabilitySettings
from observability.domain.events import ObservabilityLifecycleState
from utils.run_context import initialize_run_context


def test_initialize_observability_creates_run_and_summary(tmp_path):
    settings = ObservabilitySettings(
        runs_dir=tmp_path,
        legacy_text_log_enabled=False,
        crash_hooks_enabled=False,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=1.0,
    )
    initialize_run_context(run_id="boot-run")

    service = initialize_observability(SimpleNamespace(observability=settings))
    service.emit("probe", component="test.bootstrap", category="system")
    shutdown_observability(status="COMPLETED")

    run_dir = tmp_path / "boot-run"
    summary = json.loads(
        (run_dir / "reports" / "observability_summary.json").read_text(encoding="utf-8")
    )
    assert summary["run_id"] == "boot-run"
    assert summary["status"] == "COMPLETED"
    assert get_service() is None


def test_initialize_observability_is_idempotent(tmp_path):
    settings = ObservabilitySettings(
        runs_dir=tmp_path,
        legacy_text_log_enabled=False,
        crash_hooks_enabled=False,
    )

    first = initialize_observability(SimpleNamespace(observability=settings))
    second = initialize_observability(SimpleNamespace(observability=settings))

    assert second is first


def test_disabled_observability_creates_no_files(tmp_path):
    settings = ObservabilitySettings(enabled=False, runs_dir=tmp_path)

    service = initialize_observability(SimpleNamespace(observability=settings))
    service.emit("ignored", component="test.bootstrap")
    shutdown_observability(status="COMPLETED")

    assert isinstance(service, NullObservabilityService)
    assert list(tmp_path.iterdir()) == []


def test_shutdown_is_idempotent(tmp_path):
    settings = ObservabilitySettings(
        runs_dir=tmp_path,
        legacy_text_log_enabled=False,
        crash_hooks_enabled=False,
    )
    service = initialize_observability(SimpleNamespace(observability=settings))

    shutdown_observability(status="FAILED")
    shutdown_observability(status="FAILED")

    assert service.lifecycle_state is ObservabilityLifecycleState.STOPPED


def test_invalid_env_value_fails_clearly(monkeypatch):
    monkeypatch.setenv("ALPHORA_OBSERVABILITY_QUEUE_SIZE", "not-an-int")

    with pytest.raises(ValueError, match="ALPHORA_OBSERVABILITY_QUEUE_SIZE must be an integer"):
        initialize_observability(None)
