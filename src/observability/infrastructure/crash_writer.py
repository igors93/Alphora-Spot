"""Atomic crash report writer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from observability.infrastructure.atomic_files import atomic_write_json


def write_crash_report(
    run_dir: Path,
    crash_id: str,
    payload: dict[str, Any],
    *,
    fsync: bool = True,
) -> Path:
    """Write a crash report atomically to diagnostics/crashes/.

    Filename format: <timestamp>_<crash_id>.json
    Returns the path that was written.
    """
    crashes_dir = run_dir / "diagnostics" / "crashes"
    crashes_dir.mkdir(parents=True, exist_ok=True)

    # Use the timestamp from the payload if available, else derive one
    timestamp_raw = payload.get("timestamp", "")
    # Convert ISO timestamp to a safe filename component
    ts_safe = str(timestamp_raw).replace(":", "-").replace("+", "").replace(".", "-")[:23]
    filename = f"{ts_safe}_{crash_id}.json"
    path = crashes_dir / filename

    atomic_write_json(path, payload, fsync=fsync)
    return path
