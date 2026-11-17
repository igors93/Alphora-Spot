"""Atomic file write utilities."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, *, fsync: bool = True) -> None:
    """Write text atomically using a temp file + rename.

    On POSIX systems, rename is atomic within the same filesystem.
    The temp file is written to the same directory as the target to ensure
    they are on the same filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any, *, fsync: bool = True) -> None:
    """Write JSON atomically."""
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, content, fsync=fsync)
