import os
from unittest.mock import patch

import pytest

from futures.process_lock import ProcessLock, ProcessLockError, coerce_pid, is_pid_running


def test_coerce_pid_accepts_only_positive_integers():
    assert coerce_pid("123") == 123
    assert coerce_pid(0) is None
    assert coerce_pid("-1") is None
    assert coerce_pid("invalid") is None
    assert coerce_pid(None) is None


def test_is_pid_running_recognizes_current_process():
    assert is_pid_running(os.getpid()) is True


def test_process_lock_acquire_and_release(tmp_path):
    lock = ProcessLock(str(tmp_path), "BTCUSDT", "testnet")
    lock.acquire()
    assert lock.path.exists()
    lock.release()
    assert not lock.path.exists()


def test_process_lock_context_manager(tmp_path):
    lock = ProcessLock(str(tmp_path), "BTCUSDT", "testnet")
    with lock:
        assert lock.path.exists()
    assert not lock.path.exists()


def test_process_lock_double_acquire_raises(tmp_path):
    lock1 = ProcessLock(str(tmp_path), "BTCUSDT", "testnet")
    lock2 = ProcessLock(str(tmp_path), "BTCUSDT", "testnet")
    lock1.acquire()
    try:
        with pytest.raises(ProcessLockError):
            lock2.acquire()
    finally:
        lock1.release()


def test_process_lock_no_fd_leak_on_write_failure(tmp_path):
    """If os.write fails after os.open, fd must be closed and lock file removed."""
    lock = ProcessLock(str(tmp_path), "BTCUSDT", "testnet")

    with patch("os.write", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            lock.acquire()

    # No leaked fd — self._fd must not be set and the lock file must not remain
    assert lock._fd is None
    assert not lock.path.exists()


def test_process_lock_stale_pid_recovered(tmp_path):
    """A lock file referencing a non-running PID is cleaned up automatically."""
    import json

    lock = ProcessLock(str(tmp_path), "ETHUSDT", "testnet")
    # Write a stale lock file with a dead PID (pid=1 won't match current process,
    # but we mock is_pid_running to return False)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(json.dumps({"pid": 99999999, "created_at": 0}), encoding="utf-8")

    with patch("futures.process_lock.is_pid_running", return_value=False):
        lock.acquire()

    assert lock.path.exists()
    lock.release()
    assert not lock.path.exists()
