from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from observability.application.service import observe_error
from observability.bootstrap import initialize_observability, shutdown_observability
from observability.config import ObservabilitySettings
from observability.infrastructure import queue_listener
from utils.run_context import initialize_run_context


def test_shutdown_race_uses_fallback_for_late_errors(tmp_path, monkeypatch):
    fallback = tmp_path / "emergency.log"
    monkeypatch.setattr(queue_listener, "EMERGENCY_FALLBACK_PATH", str(fallback))
    settings = ObservabilitySettings(
        runs_dir=tmp_path / "runs",
        queue_size=8,
        legacy_text_log_enabled=False,
        crash_hooks_enabled=False,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=10.0,
    )
    initialize_run_context(run_id="race-run")
    service = initialize_observability(SimpleNamespace(observability=settings))
    stop = threading.Event()

    def producer(index: int) -> None:
        count = 0
        while not stop.is_set() and count < 200:
            observe_error(
                "race_error",
                error=RuntimeError(f"race {index} {count}"),
                component="tests.shutdown_race",
                category="system",
            )
            count += 1

    threads = [threading.Thread(target=producer, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()

    time.sleep(0.05)
    shutdown_observability(status="FAILED")
    stop.set()
    for thread in threads:
        thread.join(timeout=2)

    service.emit_error(
        "late_error",
        error=RuntimeError("after stop"),
        component="tests.shutdown_race",
        category="system",
    )

    assert service.lifecycle_state.value == "STOPPED"
    assert service._listener._queue.empty()
    assert not service._listener._thread.is_alive()
    assert fallback.exists()
    assert "late_error" in fallback.read_text(encoding="utf-8")
