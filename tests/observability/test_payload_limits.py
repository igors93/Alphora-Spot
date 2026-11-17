from __future__ import annotations

import json
from pathlib import Path

from observability.application.service import ObservabilityService, set_service
from observability.config import ObservabilitySettings
from observability.domain.health import HealthCounters
from observability.infrastructure.jsonl_writer import JsonlWriter


def _make_service(tmp_path: Path) -> ObservabilityService:
    settings = ObservabilitySettings(
        runs_dir=tmp_path,
        max_payload_bytes=1024,
        legacy_text_log_enabled=False,
        crash_hooks_enabled=False,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=1.0,
    )
    run_dir = tmp_path / "payload-run"
    for child in ("events", "diagnostics", "diagnostics/crashes", "logs", "reports"):
        (run_dir / child).mkdir(parents=True, exist_ok=True)

    health = HealthCounters()
    writer = JsonlWriter(flush_interval_seconds=0.01)
    return ObservabilityService(
        settings=settings,
        run_id="payload-run",
        run_dir=run_dir,
        writer=writer,
        listener=None,
        health=health,
    )


def test_payload_is_truncated_without_storing_original(tmp_path):
    service = _make_service(tmp_path)
    set_service(service)
    service.emit(
        "huge_payload",
        component="tests.payload",
        category="system",
        symbol="BTCUSDT",
        cycle_id="cycle-1",
        candidate_id="candidate-1",
        huge="X" * 20_000,
    )
    service.flush()

    path = tmp_path / "payload-run" / "diagnostics" / "diagnostics.jsonl"
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    payload = event["payload"]
    assert payload["payload_truncated"] is True
    assert payload["symbol"] == "BTCUSDT"
    assert payload["cycle_id"] == "cycle-1"
    assert payload["candidate_id"] == "candidate-1"
    assert "X" * 100 not in json.dumps(payload)
    assert service.health_snapshot()["payloads_truncated"] == 1
