import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from governor.governor_helpers import _json_default
from observability import observe_error, observe_event

# On Windows, os.replace() can fail with PermissionError (WinError 5) when the
# destination file is concurrently open by another process (e.g. the desktop GUI
# reading governor_state.json) without FILE_SHARE_DELETE. The reader holds the
# file for only a few milliseconds, so a short retry resolves the contention.
_WINDOWS_REPLACE_RETRIES = 8
_WINDOWS_REPLACE_RETRY_SLEEP = 0.025  # 25 ms per retry → up to 200 ms total


class GovernorStateWriter:
    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)
        self._write_lock = threading.Lock()

    def write(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            observe_event(
                "state_write_started",
                component="governor.state_writer",
                category="governor",
                state_path=str(self.state_path),
            )
            try:
                serialized = json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                )
                content = f"{serialized}\n"

                self.state_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                temp_path: Path | None = None

                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        newline="\n",
                        dir=self.state_path.parent,
                        prefix=f".{self.state_path.name}.",
                        suffix=".tmp",
                        delete=False,
                    ) as temp_file:
                        temp_path = Path(temp_file.name)
                        temp_file.write(content)
                        temp_file.flush()
                        os.fsync(temp_file.fileno())

                    self._replace_with_retry(temp_path)
                    temp_path = None

                    self._sync_parent_directory()
                finally:
                    if temp_path is not None:
                        try:
                            temp_path.unlink(missing_ok=True)
                        except OSError:
                            pass
            except Exception as error:
                observe_error(
                    "state_write_failed",
                    error=error,
                    component="governor.state_writer",
                    category="governor",
                    state_path=str(self.state_path),
                )
                raise
            else:
                observe_event(
                    "state_write_completed",
                    component="governor.state_writer",
                    category="governor",
                    state_path=str(self.state_path),
                )

    def _replace_with_retry(self, temp_path: Path) -> None:
        """Rename temp_path over self.state_path, retrying on Windows PermissionError.

        On Windows, os.replace() fails with WinError 5 when the destination is
        open without FILE_SHARE_DELETE (e.g. the GUI reading the state file).
        """
        last_error: Exception | None = None
        retries = _WINDOWS_REPLACE_RETRIES if os.name == "nt" else 1
        for attempt in range(retries):
            try:
                os.replace(temp_path, self.state_path)
                return
            except PermissionError as error:
                last_error = error
                if attempt < retries - 1:
                    time.sleep(_WINDOWS_REPLACE_RETRY_SLEEP)

        if last_error is None:
            raise RuntimeError("retry loop exited without exception")

        observe_error(
            "atomic_replace_failed",
            error=last_error,
            component="governor.state_writer",
            category="governor",
            state_path=str(self.state_path),
            temp_path=str(temp_path),
            attempts=retries,
        )
        raise last_error

    def _sync_parent_directory(self) -> None:
        if os.name == "nt":
            return

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)

        try:
            directory_fd = os.open(
                self.state_path.parent,
                flags,
            )
        except OSError:
            return

        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)
