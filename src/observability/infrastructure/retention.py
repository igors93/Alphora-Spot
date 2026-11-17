"""Run directory retention / cleanup logic."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

_MAX_CRASH_REPORTS_PER_RUN = 50


def cleanup_old_runs(
    runs_dir: Path,
    *,
    retention_days: int,
    max_runs: int,
    current_run_id: str | None = None,
) -> None:
    """Remove old run directories.

    Rules:
    - retention_days: delete runs older than N days (mtime-based)
    - max_runs: keep only the most recent N runs
    - Never remove the current run
    - Never follow symlinks
    - Never remove directories outside runs_dir
    """
    root = runs_dir.resolve()
    if not root.exists() or not root.is_dir():
        return

    now = datetime.now(timezone.utc).timestamp()
    max_age_seconds = max(0, int(retention_days)) * 24 * 60 * 60

    candidates: list[Path] = []
    for item in root.iterdir():
        # Never follow symlinks
        if item.is_symlink():
            continue
        if not item.is_dir():
            continue
        # Never remove current run
        if current_run_id and item.name == current_run_id:
            continue
        # Safety: must be a direct child of runs_dir
        try:
            item.resolve().relative_to(root)
        except ValueError:
            continue
        candidates.append(item)

    deletions: set[Path] = set()

    if max_age_seconds > 0:
        for item in candidates:
            try:
                age_seconds = now - item.stat().st_mtime
            except OSError:
                continue
            if age_seconds > max_age_seconds:
                deletions.add(item)

    if max_runs > 0:
        ordered = sorted(
            candidates,
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for item in ordered[max_runs:]:
            deletions.add(item)

    for item in sorted(deletions):
        if current_run_id and item.name == current_run_id:
            continue
        # Final safety: must still be under runs_dir
        try:
            item.resolve().relative_to(root)
        except ValueError:
            _log.warning("Skipping removal of path outside runs_dir: %s", item)
            continue
        try:
            shutil.rmtree(item)
        except Exception as exc:
            _log.warning("Failed to remove old run | path=%s | error=%s", item, exc)


def trim_crash_reports(run_dir: Path, *, max_count: int = _MAX_CRASH_REPORTS_PER_RUN) -> None:
    """Keep only the most recent crash reports per run directory.

    Oldest crash files are removed first when the count exceeds max_count.
    """
    crashes_dir = run_dir / "diagnostics" / "crashes"
    if not crashes_dir.exists():
        return

    reports = sorted(
        (p for p in crashes_dir.iterdir() if p.is_file() and p.suffix == ".json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old_report in reports[max_count:]:
        try:
            old_report.unlink()
        except Exception as exc:
            _log.warning("Failed to remove old crash report | path=%s | error=%s", old_report, exc)
