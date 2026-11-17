"""Persistent JSONL appenders with rotation detection.

Opening and closing the journal on every event costs three syscalls plus
file-object construction; with many symbols the engine appends hundreds of
events per cycle. This module keeps handles open, detects rotation (journals
are archived with shutil.move and recreated empty) by comparing inodes, and
bounds open handles with a small LRU.
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

_MAX_OPEN_HANDLES = 128
_WRITE_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.05


class _OpenHandle:
    __slots__ = ("file", "ino", "dev")

    def __init__(self, file, ino: int, dev: int) -> None:
        self.file = file
        self.ino = ino
        self.dev = dev


class JsonlAppendWriter:
    def __init__(
        self,
        *,
        max_open_handles: int = _MAX_OPEN_HANDLES,
        persistent_handles: bool | None = None,
    ) -> None:
        self._max_open = max(1, int(max_open_handles))
        self._handles: OrderedDict[str, _OpenHandle] = OrderedDict()
        self._lock = threading.Lock()
        # Windows refuses to rename or delete a file while any handle is open,
        # so a cached handle would break journal rotation done by another
        # process (the desktop UI). There, open-append-close per event.
        self._persistent_handles = (
            os.name != "nt" if persistent_handles is None else bool(persistent_handles)
        )

    def append_line(self, path: str | Path, line: str) -> None:
        """Append `line` to `path` (newline added when missing), flushing per call.

        Reopens the handle when the file was rotated or removed; retries
        transient failures (e.g. PermissionError on Windows) like the
        previous open-per-append implementation did.
        """
        target = Path(path)
        key = str(target)
        payload = line if line.endswith("\n") else f"{line}\n"

        for attempt in range(_WRITE_ATTEMPTS):
            try:
                with self._lock:
                    if not self._persistent_handles:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open("a", encoding="utf-8") as file:
                            file.write(payload)
                        return
                    handle = self._handle_locked(target, key)
                    handle.file.write(payload)
                    handle.file.flush()
                return
            except (OSError, ValueError):
                with self._lock:
                    self._close_locked(key)
                if attempt == _WRITE_ATTEMPTS - 1:
                    raise
                time.sleep(_RETRY_BASE_DELAY_SECONDS * (attempt + 1))

    def close_all(self) -> None:
        """Close every cached handle (required before archiving journals)."""
        with self._lock:
            for key in list(self._handles):
                self._close_locked(key)

    def close_path(self, path: str | Path) -> None:
        with self._lock:
            self._close_locked(str(Path(path)))

    def _handle_locked(self, target: Path, key: str) -> _OpenHandle:
        cached = self._handles.get(key)
        if cached is not None:
            try:
                stat = os.stat(target)
            except OSError:
                stat = None
            if stat is not None and (stat.st_ino, stat.st_dev) == (cached.ino, cached.dev):
                self._handles.move_to_end(key)
                return cached
            self._close_locked(key)

        target.parent.mkdir(parents=True, exist_ok=True)
        file = target.open("a", encoding="utf-8")
        info = os.fstat(file.fileno())
        handle = _OpenHandle(file, info.st_ino, info.st_dev)
        self._handles[key] = handle
        while len(self._handles) > self._max_open:
            oldest = next(iter(self._handles))
            self._close_locked(oldest)
        return handle

    def _close_locked(self, key: str) -> None:
        handle = self._handles.pop(key, None)
        if handle is None:
            return
        try:
            handle.file.close()
        except Exception:
            pass


_SHARED_WRITER = JsonlAppendWriter()
atexit.register(_SHARED_WRITER.close_all)


def shared_jsonl_writer() -> JsonlAppendWriter:
    return _SHARED_WRITER
