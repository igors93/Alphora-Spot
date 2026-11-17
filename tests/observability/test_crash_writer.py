"""Tests for atomic crash report writing."""

from __future__ import annotations

import json

from observability.infrastructure.crash_writer import write_crash_report


def test_crash_written_to_crashes_dir(tmp_path):
    """Crash report is written inside diagnostics/crashes/."""
    run_dir = tmp_path / "run"
    payload = {
        "timestamp": "2026-06-14T12:00:00+00:00",
        "crash_id": "abc123",
        "error": {"type": "ValueError", "message": "test"},
    }
    path = write_crash_report(run_dir, "abc123", payload, fsync=False)
    assert path.parent == run_dir / "diagnostics" / "crashes"
    assert path.exists()


def test_filename_format(tmp_path):
    """Filename includes timestamp prefix and crash_id."""
    run_dir = tmp_path / "run"
    payload = {
        "timestamp": "2026-06-14T12:00:00+00:00",
        "crash_id": "mycrash01",
        "error": {},
    }
    path = write_crash_report(run_dir, "mycrash01", payload, fsync=False)
    assert "mycrash01" in path.name
    assert path.suffix == ".json"


def test_write_is_atomic(tmp_path):
    """Crash report is written atomically (no temp file left behind)."""
    run_dir = tmp_path / "run"
    payload = {"timestamp": "2026-06-14T00:00:00+00:00", "crash_id": "c1", "error": {}}
    path = write_crash_report(run_dir, "c1", payload, fsync=False)
    # No .tmp files in the directory
    tmp_files = list(path.parent.glob("*.tmp"))
    assert tmp_files == []
    # File is readable and valid JSON
    with path.open(encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["crash_id"] == "c1"


def test_payload_sanitized_before_write(tmp_path):
    """Secret values should not appear in crash report if pre-sanitized."""
    from utils.logprivacy_adapter import sanitize_payload

    run_dir = tmp_path / "run"
    raw_payload = {
        "timestamp": "2026-06-14T00:00:00+00:00",
        "crash_id": "sanitize-test",
        "api_key": "VERY_SECRET_KEY",
        "error": {"type": "RuntimeError", "message": "crash"},
    }
    # Sanitize before writing (as the lifecycle handler does)
    safe_payload = sanitize_payload(raw_payload)
    path = write_crash_report(run_dir, "sanitize-test", safe_payload, fsync=False)
    content = path.read_text(encoding="utf-8")
    assert "VERY_SECRET_KEY" not in content


def test_multiple_crash_reports(tmp_path):
    """Multiple crash reports are written to separate files."""
    run_dir = tmp_path / "run"
    for i in range(3):
        payload = {
            "timestamp": f"2026-06-14T12:00:0{i}+00:00",
            "crash_id": f"crash{i}",
            "error": {},
        }
        write_crash_report(run_dir, f"crash{i}", payload, fsync=False)

    crashes_dir = run_dir / "diagnostics" / "crashes"
    files = list(crashes_dir.glob("*.json"))
    assert len(files) == 3
