import json
from pathlib import Path

from desktop.services.observability_service import (
    build_observability_snapshot,
    first_existing_source,
    resolve_project_path,
    rotate_observability_journals,
)
from desktop.settings_store import DEFAULT_SETTINGS


def _settings() -> dict:
    values = dict(DEFAULT_SETTINGS)
    values["environment"] = "TESTNET"
    return values


def _profiles() -> list[dict]:
    return [{"enabled": True, "symbol": "BTCUSDT"}]


def test_build_observability_snapshot_reads_events_journals_scanner_and_state(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    runtime = tmp_path / "runtime"
    data.mkdir()
    runtime.mkdir()

    event = {
        "timestamp": "2026-06-13T10:00:00Z",
        "event_type": "intelligent_execution_plan_evaluated",
        "data": {
            "symbol": "BTCUSDT",
            "status": "APPLIED",
            "action": "MARKET_SAFE",
            "reason": "economic_gate_ok",
        },
    }
    (data / "governor_events.jsonl").write_text(
        json.dumps(event) + "\n",
        encoding="utf-8",
    )
    (data / "trades.jsonl").write_text('{"event":"trade"}\n', encoding="utf-8")
    (data / "scanner_scores.jsonl").write_text(
        '{"symbol":"BTCUSDT","score":0.8}\n',
        encoding="utf-8",
    )
    (runtime / "governor_state.json").write_text(
        json.dumps({"status": "RUNNING", "api_key": "secret-value"}),
        encoding="utf-8",
    )
    (runtime / "governor.lock").write_text("1234", encoding="utf-8")

    snapshot = build_observability_snapshot(
        settings=_settings(),
        symbol_profiles=_profiles(),
        root_path=tmp_path,
        event_limit=20,
        journal_tail_lines=20,
        scanner_tail_lines=20,
    )

    assert len(snapshot.events) == 1
    assert snapshot.events[0]["symbol"] == "BTCUSDT"
    assert "governor_events.jsonl" in snapshot.journal_text
    assert "scanner_scores.jsonl" in snapshot.scanner_text
    assert "governor_state.json" in snapshot.state_text
    assert "governor.lock" in snapshot.state_text
    assert "secret-value" not in snapshot.state_text
    assert snapshot.existing_sources >= 4


def test_rotate_observability_journals_archives_and_recreates_files(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    source = data / "trades.jsonl"
    source.write_text('{"event":"trade"}\n', encoding="utf-8")

    result = rotate_observability_journals(
        settings=_settings(),
        symbol_profiles=[],
        root_path=tmp_path,
        kind="journals",
    )

    assert result.moved_count == 1
    assert source.exists()
    assert source.read_text(encoding="utf-8") == ""
    assert result.archive_directory
    assert Path(result.archive_directory).is_dir()


def test_project_path_resolution_rejects_paths_outside_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    try:
        resolve_project_path(tmp_path, outside)
    except ValueError as error:
        assert "fora da raiz" in str(error)
    else:
        raise AssertionError("Caminho externo deveria ser rejeitado")


def test_first_existing_source_returns_first_available_path(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "state.json"
    target.parent.mkdir()
    target.write_text("{}", encoding="utf-8")

    result = first_existing_source(
        root_path=tmp_path,
        paths=("runtime/missing.json", "runtime/state.json"),
    )

    assert result == target.resolve()


def test_snapshot_builds_only_the_requested_sections(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "trades.jsonl").write_text('{"event":"trade"}\n', encoding="utf-8")

    snapshot = build_observability_snapshot(
        settings=_settings(),
        symbol_profiles=_profiles(),
        root_path=tmp_path,
        sections=["events"],
    )

    assert snapshot.journal_text == ""
    assert snapshot.scanner_text == ""
    assert snapshot.state_text == ""
    assert snapshot.sources == ()


def test_memoized_journal_lines_stay_sanitized(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "trades.jsonl").write_text(
        json.dumps({"event": "trade", "api_key": "secret-value"}) + "\n",
        encoding="utf-8",
    )

    texts = [
        build_observability_snapshot(
            settings=_settings(),
            symbol_profiles=_profiles(),
            root_path=tmp_path,
            sections=["journals"],
        ).journal_text
        for _ in range(2)
    ]

    assert texts[0] == texts[1]
    assert "trades.jsonl" in texts[0]
    assert "secret-value" not in texts[0]
