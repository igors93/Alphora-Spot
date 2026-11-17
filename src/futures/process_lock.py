import ctypes
import json
import os
from pathlib import Path
from typing import Any


class ProcessLockError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, runtime_dir: str, symbol: str, environment: str):
        safe_symbol = _safe_lock_part(symbol)
        safe_environment = _safe_lock_part(environment)
        self.path = Path(runtime_dir) / f"alphora-{safe_symbol}-{safe_environment}.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        while True:
            try:
                fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                try:
                    payload = json.dumps(
                        {"pid": os.getpid(), "created_at": _now_epoch_seconds()},
                        sort_keys=True,
                    )
                    os.write(fd, payload.encode("utf-8"))
                except Exception:
                    os.close(fd)
                    self.path.unlink(missing_ok=True)
                    raise
                self._fd = fd
                return
            except FileExistsError:
                lock_data = self._read_lock_data()
                pid = coerce_pid(lock_data.get("pid"))

                if pid is None or not is_pid_running(pid):
                    # Stale lock: process is dead or file has no valid PID
                    # (e.g. the previous process crashed between open() and write())
                    self.path.unlink(missing_ok=True)
                    continue

                raise ProcessLockError(
                    f"Ja existe uma instancia Alphora ativa para este simbolo/ambiente: {self.path}"
                ) from None

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

        self.path.unlink(missing_ok=True)

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def _read_lock_data(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}

        return data if isinstance(data, dict) else {}


def _safe_lock_part(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value.lower()).strip("-")


def coerce_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None

    return pid if pid > 0 else None


def is_pid_running(pid: int) -> bool:
    if pid == os.getpid():
        return True

    if os.name == "nt":
        return _pid_is_running_windows(pid)

    try:
        os.kill(pid, 0)
    except OSError:
        return False

    return True


def _pid_is_running_windows(pid: int) -> bool:
    process_query_limited_information = 0x1000
    still_active = 259

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False

    exit_code = ctypes.c_ulong()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False

        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _now_epoch_seconds() -> float:
    import time

    return time.time()
