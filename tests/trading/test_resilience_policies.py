"""Tests for resilience.policies and related integration points."""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from binance.futures_client import BinanceFuturesAPIError, BinanceFuturesNetworkError
from binance.retry_policies import (
    build_binance_timestamp_resync_policy,
    is_timestamp_out_of_sync,
    is_transient_binance_read_error,
)
from resilience.events import make_giveup_logger
from resilience.predicates import is_expected_position, is_open_position

# ---------------------------------------------------------------------------
# Predicate tests
# ---------------------------------------------------------------------------


def test_is_timestamp_out_of_sync_matches_code():
    err = BinanceFuturesAPIError(400, -1021, "timestamp")
    assert is_timestamp_out_of_sync(err, None) is True


def test_is_timestamp_out_of_sync_wrong_code():
    err = BinanceFuturesAPIError(400, -1001, "other")
    assert is_timestamp_out_of_sync(err, None) is False


def test_is_timestamp_out_of_sync_no_exception():
    assert is_timestamp_out_of_sync(None, "some_result") is False


def test_is_timestamp_out_of_sync_wrong_exception_type():
    assert is_timestamp_out_of_sync(ValueError("x"), None) is False


def test_is_transient_read_error_network():
    err = BinanceFuturesNetworkError("timeout")
    assert is_transient_binance_read_error(err, None) is True


def test_is_transient_read_error_429():
    err = BinanceFuturesAPIError(429, -1003, "rate limit")
    assert is_transient_binance_read_error(err, None) is True


def test_is_transient_read_error_500():
    err = BinanceFuturesAPIError(500, -1, "server error")
    assert is_transient_binance_read_error(err, None) is True


def test_is_transient_read_error_503():
    err = BinanceFuturesAPIError(503, -1, "server error")
    assert is_transient_binance_read_error(err, None) is True


def test_is_transient_read_error_fatal_code():
    err = BinanceFuturesAPIError(400, -2015, "auth error")
    assert is_transient_binance_read_error(err, None) is False


def test_is_transient_read_error_no_exception():
    assert is_transient_binance_read_error(None, "result") is False


def _make_position(is_open, direction=None):
    from unittest.mock import MagicMock

    pos = MagicMock()
    pos.is_open = is_open
    pos.direction = direction
    return pos


def test_is_open_position_true():
    assert is_open_position(_make_position(True)) is True


def test_is_open_position_false():
    assert is_open_position(_make_position(False)) is False


def test_is_expected_position_match():
    from domain.models import PositionDirection

    pos = _make_position(True, PositionDirection.LONG)
    assert is_expected_position(pos, PositionDirection.LONG) is True


def test_is_expected_position_wrong_direction():
    from domain.models import PositionDirection

    pos = _make_position(True, PositionDirection.SHORT)
    assert is_expected_position(pos, PositionDirection.LONG) is False


def test_is_expected_position_not_open():
    from domain.models import PositionDirection

    pos = _make_position(False, PositionDirection.LONG)
    assert is_expected_position(pos, PositionDirection.LONG) is False


# ---------------------------------------------------------------------------
# build_binance_timestamp_resync_policy tests
# ---------------------------------------------------------------------------


class _TimestampClient:
    def __init__(self, sync_raises=None):
        self._time_sync_lock = threading.Lock()
        self._time_sync_generation = 0
        self.sync_time = MagicMock(return_value=0)
        if sync_raises:
            self.sync_time.side_effect = sync_raises

    def _time_sync_generation_snapshot(self):
        with self._time_sync_lock:
            return self._time_sync_generation

    def _sync_time_if_generation_current(self, expected_generation, *, timeout=None):
        with self._time_sync_lock:
            current_generation = self._time_sync_generation
            if current_generation != expected_generation:
                return False, None, current_generation

            new_offset_ms = self.sync_time(timeout=timeout)
            self._time_sync_generation += 1
            return True, new_offset_ms, self._time_sync_generation


def _make_client(sync_raises=None):
    return _TimestampClient(sync_raises=sync_raises)


def test_resync_policy_two_calls_on_1021(tmp_path):
    """Exactly two signed-request calls and one sync_time on -1021."""
    client = _make_client()
    call_count = 0

    def _signed_request_once():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise BinanceFuturesAPIError(400, -1021, "timestamp out of sync")
        return {"ok": True}

    policy = build_binance_timestamp_resync_policy(client, timeout=5.0)
    result = policy.run(_signed_request_once)

    assert result == {"ok": True}
    assert call_count == 2
    client.sync_time.assert_called_once_with(timeout=5.0)


def test_resync_policy_syncs_before_relinker_delay():
    client = _make_client()
    sequence = []
    call_count = 0

    def _sync_time(*, timeout=None):
        sequence.append(("sync", timeout))
        return 0

    client.sync_time.side_effect = _sync_time

    def _signed_request_once():
        nonlocal call_count
        call_count += 1
        sequence.append(("request", call_count))
        if call_count == 1:
            raise BinanceFuturesAPIError(400, -1021, "timestamp out of sync")
        return {"ok": True}

    sleeps = []
    policy = build_binance_timestamp_resync_policy(client, timeout=5.0).with_sleep(
        lambda delay: (sequence.append(("sleep", delay)), sleeps.append(delay))
    )

    assert policy.run(_signed_request_once) == {"ok": True}
    assert sequence == [
        ("request", 1),
        ("sync", 5.0),
        ("sleep", 0.05),
        ("request", 2),
    ]
    assert sleeps == [0.05]


def test_resync_policy_non_1021_propagates_immediately():
    """A non-1021 API error is NOT retried."""
    client = _make_client()
    call_count = 0

    def _signed_request_once():
        nonlocal call_count
        call_count += 1
        raise BinanceFuturesAPIError(400, -9999, "other error")

    policy = build_binance_timestamp_resync_policy(client, timeout=None)
    with pytest.raises(BinanceFuturesAPIError) as exc_info:
        policy.run(_signed_request_once)

    assert exc_info.value.code == -9999
    assert call_count == 1
    client.sync_time.assert_not_called()


def test_timestamp_resync_giveup_logger_suppresses_non_timestamp_errors(caplog):
    logger = logging.getLogger("tests.resilience.timestamp")
    handler = make_giveup_logger(logger)
    error = BinanceFuturesAPIError(400, -4046, "No need to change margin type.")
    event = SimpleNamespace(
        state=SimpleNamespace(
            policy_name="binance_timestamp_resync",
            function_name="_signed_request_once",
            attempt_number=1,
            last_error=error,
            elapsed=0.0,
        ),
        error=error,
        policy_name="binance_timestamp_resync",
        function_name="_signed_request_once",
        attempt_number=1,
    )

    with caplog.at_level(logging.ERROR, logger="tests.resilience.timestamp"):
        handler(event)

    assert not caplog.records


def test_resync_policy_network_error_not_retried():
    """Network errors are not caught by the timestamp policy."""
    client = _make_client()
    call_count = 0

    def _signed_request_once():
        nonlocal call_count
        call_count += 1
        raise BinanceFuturesNetworkError("timeout")

    policy = build_binance_timestamp_resync_policy(client, timeout=None)
    with pytest.raises(BinanceFuturesNetworkError):
        policy.run(_signed_request_once)

    assert call_count == 1
    client.sync_time.assert_not_called()


def test_resync_policy_preserves_timeout():
    """The timeout passed to the policy factory is forwarded to sync_time on every retry."""
    client = _make_client()

    def _signed_request_once():
        raise BinanceFuturesAPIError(400, -1021, "ts")

    policy = build_binance_timestamp_resync_policy(client, timeout=(3.0, 10.0))
    with pytest.raises(BinanceFuturesAPIError):
        policy.run(_signed_request_once)

    # Policy has 4 attempts → sync_time called on retries 2, 3, 4 = 3 times total.
    assert client.sync_time.call_count == 3
    client.sync_time.assert_called_with(timeout=(3.0, 10.0))


def test_resync_policy_concurrent_sync_coalesced():
    """Concurrent -1021 errors reuse one sync per retry generation."""
    client = _make_client()
    barrier = threading.Barrier(3)
    results = []

    def _signed_request_once():
        barrier.wait()
        raise BinanceFuturesAPIError(400, -1021, "ts")

    def _run():
        policy = build_binance_timestamp_resync_policy(client, timeout=None)
        try:
            policy.run(_signed_request_once)
        except BinanceFuturesAPIError:
            results.append("exhausted")

    threads = [threading.Thread(target=_run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 3
    # 3 threads × 3 syncs each (attempts 2,3,4) = 9 total, all serialized by the lock.
    assert client.sync_time.call_count == 3


def test_resync_policy_named():
    """Policy must carry the expected name for observability."""
    from binance.retry_policies import _BASE_TIMESTAMP_RESYNC

    assert _BASE_TIMESTAMP_RESYNC.explain().startswith("Relinker policy")


# ---------------------------------------------------------------------------
# BinanceFuturesClient integration: _time_sync_lock and _sync_time_serialized
# ---------------------------------------------------------------------------


def test_client_has_time_sync_lock():
    from binance.futures_client import BinanceFuturesClient

    client = BinanceFuturesClient(
        base_url="https://fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    assert hasattr(client, "_time_sync_lock")
    assert isinstance(client._time_sync_lock, type(threading.Lock()))


def test_client_sync_time_serialized_calls_sync_time():
    from binance.futures_client import BinanceFuturesClient

    client = BinanceFuturesClient(
        base_url="https://fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    with patch.object(client, "sync_time") as mock_sync:
        client._sync_time_serialized(timeout=5.0)
        mock_sync.assert_called_once_with(timeout=5.0)


# ---------------------------------------------------------------------------
# execution.errors.classify_error and TRANSIENT_STATUS_CODES
# ---------------------------------------------------------------------------


def test_transient_status_codes_includes_503():
    """503 must be in TRANSIENT_STATUS_CODES so both classify_error and
    is_transient_binance_read_error agree (regression for split-set inconsistency)."""
    from binance.futures_client import TRANSIENT_STATUS_CODES

    assert 503 in TRANSIENT_STATUS_CODES


def test_classify_error_503_non_unknown_is_transient():
    from execution.errors import ErrorKind, classify_error

    err = BinanceFuturesAPIError(503, -1, "generic 503")
    assert classify_error(err) == ErrorKind.TRANSIENT


def test_classify_error_503_unknown_execution_is_unknown_order_status():
    from execution.errors import ErrorKind, classify_error

    err = BinanceFuturesAPIError(503, -1, "execution status is unknown")
    assert classify_error(err) == ErrorKind.UNKNOWN_ORDER_STATUS


def test_classify_error_network_is_transient():
    from execution.errors import ErrorKind, classify_error

    err = BinanceFuturesNetworkError("timeout")
    assert classify_error(err) == ErrorKind.TRANSIENT


def test_classify_error_fatal_code_is_fatal():
    from execution.errors import ErrorKind, classify_error

    err = BinanceFuturesAPIError(400, -2015, "auth")
    assert classify_error(err) == ErrorKind.FATAL


# ---------------------------------------------------------------------------
# Budget smoke tests
# ---------------------------------------------------------------------------


def test_budgets_importable():
    from resilience.budgets import (
        BINANCE_READ_RETRY_BUDGET,
        EMERGENCY_FLATTEN_BUDGET,
        POSITION_CONFIRMATION_BUDGET,
    )

    # Snapshots must be callable
    snap = BINANCE_READ_RETRY_BUDGET.snapshot("test-key")
    assert snap.available >= 0

    snap2 = POSITION_CONFIRMATION_BUDGET.snapshot("other-key")
    assert snap2.available >= 0

    snap3 = EMERGENCY_FLATTEN_BUDGET.snapshot("flat-key")
    assert snap3.available >= 0
