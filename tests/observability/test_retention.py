"""Tests for run directory retention/cleanup."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from observability.infrastructure.retention import cleanup_old_runs


def _make_run_dir(runs_dir: Path, name: str, age_days: float = 0) -> Path:
    """Create a fake run directory with the given age."""
    d = runs_dir / name
    d.mkdir(parents=True)
    # Set mtime to simulate age
    if age_days > 0:
        past = time.time() - (age_days * 24 * 3600)
        os.utime(d, (past, past))
    return d


def test_runs_older_than_retention_removed(tmp_path):
    """Runs older than retention_days are removed."""
    _make_run_dir(tmp_path, "old-run", age_days=31)
    _make_run_dir(tmp_path, "new-run", age_days=0)

    cleanup_old_runs(tmp_path, retention_days=30, max_runs=100)

    assert not (tmp_path / "old-run").exists()
    assert (tmp_path / "new-run").exists()


def test_max_runs_enforced(tmp_path):
    """Only max_runs most-recent runs are kept."""
    for i in range(5):
        _make_run_dir(tmp_path, f"run-{i:02d}", age_days=float(5 - i))

    cleanup_old_runs(tmp_path, retention_days=365, max_runs=3)

    remaining = [d.name for d in tmp_path.iterdir() if d.is_dir()]
    assert len(remaining) == 3


def test_max_runs_one_keeps_newest(tmp_path):
    """max_runs=1 keeps exactly the most-recent run (regression for off-by-one)."""
    _make_run_dir(tmp_path, "old-run", age_days=2)
    _make_run_dir(tmp_path, "new-run", age_days=0)

    cleanup_old_runs(tmp_path, retention_days=365, max_runs=1)

    remaining = [d.name for d in tmp_path.iterdir() if d.is_dir()]
    assert remaining == ["new-run"]


def test_max_runs_zero_deletes_nothing_by_count(tmp_path):
    """max_runs=0 disables the count limit — no runs deleted by count alone."""
    for i in range(3):
        _make_run_dir(tmp_path, f"run-{i}", age_days=0)

    cleanup_old_runs(tmp_path, retention_days=365, max_runs=0)

    remaining = [d.name for d in tmp_path.iterdir() if d.is_dir()]
    assert len(remaining) == 3


def test_current_run_never_removed(tmp_path):
    """The current run is never removed regardless of age."""
    _make_run_dir(tmp_path, "current-run", age_days=100)

    cleanup_old_runs(
        tmp_path,
        retention_days=1,
        max_runs=1,
        current_run_id="current-run",
    )

    assert (tmp_path / "current-run").exists()


def test_symlinks_not_followed(tmp_path):
    """Symlinked directories are not deleted."""
    real_dir = tmp_path / "real-target"
    real_dir.mkdir()
    link = tmp_path / "runs" / "symlink-run"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(real_dir)
    except (NotImplementedError, OSError) as exc:
        # Windows without Developer Mode raises OSError [WinError 1314] when
        # creating symlinks without the SeCreateSymbolicLinkPrivilege privilege.
        pytest.skip(f"Symlinks not available on this platform/configuration: {exc}")

    cleanup_old_runs(link.parent, retention_days=0, max_runs=1)

    # Symlink should not be followed to delete real_dir
    assert real_dir.exists()


def test_files_outside_runs_dir_not_removed(tmp_path):
    """No files outside runs_dir are removed."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    outside = tmp_path / "important_outside"
    outside.mkdir()

    cleanup_old_runs(runs_dir, retention_days=0, max_runs=0)

    assert outside.exists()


def test_empty_runs_dir_no_error(tmp_path):
    """cleanup_old_runs on an empty directory does not raise."""
    cleanup_old_runs(tmp_path, retention_days=30, max_runs=100)


def test_nonexistent_runs_dir_no_error(tmp_path):
    """cleanup_old_runs on a nonexistent directory does not raise."""
    cleanup_old_runs(tmp_path / "does-not-exist", retention_days=30, max_runs=100)
