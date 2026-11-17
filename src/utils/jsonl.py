import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_TAIL_CHUNK_SIZE = 64 * 1024


def iter_lines(path: str | Path) -> Iterable[str]:
    with Path(path).open("r", encoding="utf-8") as file:
        for line in file:
            yield line.rstrip("\n")


def tail_lines(path: str | Path, limit: int) -> list[str]:
    """Return the last `limit` lines reading only the file suffix.

    Journals grow without bound during a run; scanning them from the start
    on every refresh becomes O(file size). This reads fixed-size blocks
    backwards from the end until enough newlines are collected.
    """
    if limit < 1:
        return []
    target = Path(path)
    try:
        with target.open("rb") as file:
            file.seek(0, os.SEEK_END)
            position = file.tell()
            buffer = b""
            # limit + 1 newlines guarantee `limit` complete lines after the
            # (possibly partial) first chunk boundary line is sliced away.
            while position > 0 and buffer.count(b"\n") <= limit:
                read_size = min(_TAIL_CHUNK_SIZE, position)
                position -= read_size
                file.seek(position)
                buffer = file.read(read_size) + buffer
    except FileNotFoundError:
        return []

    if not buffer:
        return []

    text = buffer.decode("utf-8", errors="replace")
    # Split on "\n" only (str.splitlines would also break on U+2028 inside JSON
    # strings) and drop the "\r" that Windows text-mode writers leave behind.
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines[-limit:]


def parse_json_object_line(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None

    return payload if isinstance(payload, dict) else None
