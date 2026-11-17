"""Thread-safe JSONL writer with rotation, fsync, and periodic flush."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Guard for the global writers dict
_WRITERS_LOCK = threading.Lock()
_ACTIVE_WRITERS: dict[Path, _FileWriter] = {}


class _FileWriter:
    """One writer per resolved file path."""

    def __init__(
        self,
        path: Path,
        *,
        max_file_bytes: int,
        backup_count: int,
        flush_interval_seconds: float,
    ) -> None:
        self._path = path
        self._max_file_bytes = max_file_bytes
        self._backup_count = backup_count
        self._flush_interval = flush_interval_seconds
        self._lock = threading.Lock()
        self._fh: Any = None
        self._last_flush = time.monotonic()

    def _open(self) -> None:
        if self._fh is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")

    def _maybe_rotate(self) -> None:
        try:
            if self._path.exists() and self._path.stat().st_size >= self._max_file_bytes:
                self._rotate()
        except OSError:
            pass

    def _rotate(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        # Shift backups: .1 -> .2, ... (N-1) -> N; current -> .1
        for n in range(self._backup_count - 1, 0, -1):
            src = self._path.parent / f"{self._path.name}.{n}"
            dst = self._path.parent / f"{self._path.name}.{n + 1}"
            if src.exists():
                try:
                    src.rename(dst)
                except OSError:
                    pass
        bak = self._path.parent / f"{self._path.name}.1"
        try:
            self._path.rename(bak)
        except OSError:
            pass

    def write(self, event_dict: dict[str, Any], *, fsync: bool = False) -> None:
        line = json.dumps(event_dict, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._maybe_rotate()
            self._open()
            try:
                self._fh.write(line)
                self._fh.write("\n")
                now = time.monotonic()
                if fsync or now - self._last_flush >= self._flush_interval:
                    self._fh.flush()
                    if fsync:
                        try:
                            os.fsync(self._fh.fileno())
                        except (OSError, AttributeError):
                            pass
                    self._last_flush = now
            except Exception as exc:
                _log.warning("JSONL write failed | path=%s | error=%s", self._path, exc)
                raise

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None


class JsonlWriter:
    """Manages one _FileWriter per file path, with rotation support."""

    def __init__(
        self,
        *,
        max_file_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 10,
        flush_interval_seconds: float = 0.25,
        fsync_errors: bool = True,
    ) -> None:
        self._max_file_bytes = max_file_bytes
        self._backup_count = backup_count
        self._flush_interval = flush_interval_seconds
        self._fsync_errors = fsync_errors
        self._writers: dict[Path, _FileWriter] = {}
        self._lock = threading.Lock()

    def _get_writer(self, path: Path) -> _FileWriter:
        resolved = path.resolve()
        with self._lock:
            writer = self._writers.get(resolved)
            if writer is None:
                writer = _FileWriter(
                    path,
                    max_file_bytes=self._max_file_bytes,
                    backup_count=self._backup_count,
                    flush_interval_seconds=self._flush_interval,
                )
                self._writers[resolved] = writer
            return writer

    def write(self, path: Path, event_dict: dict[str, Any], *, fsync: bool | None = None) -> None:
        use_fsync = self._fsync_errors if fsync is None else fsync
        writer = self._get_writer(path)
        writer.write(event_dict, fsync=use_fsync)

    def flush_all(self) -> None:
        with self._lock:
            writers = list(self._writers.values())
        for writer in writers:
            writer.flush()

    def close(self) -> None:
        with self._lock:
            writers = list(self._writers.values())
        for writer in writers:
            writer.close()
