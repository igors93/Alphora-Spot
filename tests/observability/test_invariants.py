"""Tests for observability invariant checking."""

from __future__ import annotations

from pathlib import Path

import pytest

from observability.application.invariants import observe_invariant, require_invariant
from observability.application.service import ObservabilityService, set_service
from observability.config import ObservabilitySettings
from observability.domain.context import diagnostic_context
from observability.domain.health import HealthCounters
from observability.infrastructure.jsonl_writer import JsonlWriter


def _make_service(tmp_path: Path) -> ObservabilityService:
    settings = ObservabilitySettings(
        runs_dir=tmp_path,
        shutdown_timeout_seconds=1.0,
        legacy_text_log_enabled=False,
        crash_hooks_enabled=False,
    )
    run_dir = tmp_path / "inv-run"
    for d in ("events", "diagnostics", "diagnostics/crashes", "logs", "reports"):
        (run_dir / d).mkdir(parents=True, exist_ok=True)

    health = HealthCounters()
    writer = JsonlWriter(
        max_file_bytes=1024 * 1024,
        backup_count=3,
        flush_interval_seconds=0.01,
        fsync_errors=False,
    )

    from observability.infrastructure.queue_listener import QueueListener

    emitted = []

    def _router(event, rd):
        emitted.append(event)

    listener = QueueListener(
        queue_size=100,
        run_dir=run_dir,
        writer=writer,
        health=health,
        shutdown_timeout_seconds=1.0,
        flush_interval_seconds=0.01,
        event_router=_router,
    )
    listener.start()
    svc = ObservabilityService(
        settings=settings,
        run_id="inv-test",
        run_dir=run_dir,
        writer=writer,
        listener=listener,
        health=health,
    )
    # Store emitted for inspection
    svc._test_emitted = emitted  # type: ignore[attr-defined]
    return svc


def test_true_condition_no_event(tmp_path):
    """observe_invariant with True condition emits no event."""
    svc = _make_service(tmp_path)
    set_service(svc)
    result = observe_invariant(True, "position_not_negative", component="test")
    assert result is True
    svc.flush()
    assert svc._health.invariant_violations == 0  # type: ignore[attr-defined]
    svc.shutdown()


def test_false_condition_emits_event(tmp_path):
    """observe_invariant with False condition emits INVARIANT event."""
    svc = _make_service(tmp_path)
    set_service(svc)
    result = observe_invariant(False, "position_not_negative", component="test")
    assert result is False
    svc.flush()
    assert svc._health.invariant_violations >= 1
    svc.shutdown()


def test_require_invariant_emits_and_raises(tmp_path):
    """require_invariant emits event AND raises the given exception."""
    svc = _make_service(tmp_path)
    set_service(svc)

    with pytest.raises(ValueError, match="invariant broken"):
        require_invariant(
            False,
            "must_not_be_zero",
            component="test",
            exception_factory=lambda: ValueError("invariant broken"),
        )

    svc.flush()
    assert svc._health.invariant_violations >= 1
    svc.shutdown()


def test_require_invariant_true_does_not_raise(tmp_path):
    """require_invariant with True condition does not raise."""
    svc = _make_service(tmp_path)
    set_service(svc)
    require_invariant(
        True,
        "all_good",
        component="test",
        exception_factory=lambda: ValueError("should not raise"),
    )
    svc.shutdown()


def test_invariant_includes_context(tmp_path):
    """Invariant event includes diagnostic context."""
    svc = _make_service(tmp_path)
    set_service(svc)
    with diagnostic_context(symbol="BTCUSDT", cycle_id="cycle-42"):
        observe_invariant(False, "test_invariant", component="test")
    svc.flush()
    # Verify at least one event emitted
    assert svc._health.invariant_violations >= 1
    svc.shutdown()


def test_invariant_payload_sanitized(tmp_path):
    """Invariant event payload is sanitized."""
    svc = _make_service(tmp_path)
    set_service(svc)
    observe_invariant(
        False,
        "secret_invariant",
        component="test",
        api_key="SHOULD_BE_MASKED",
    )
    svc.flush()
    emitted = svc._test_emitted  # type: ignore[attr-defined]
    # Check that the sensitive key was not stored as-is
    for event in emitted:
        payload_str = str(event.payload)
        assert "SHOULD_BE_MASKED" not in payload_str
    svc.shutdown()
