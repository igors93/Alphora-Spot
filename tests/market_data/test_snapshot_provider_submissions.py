"""Tests for Etapa 2: async submission tracking and deduplication."""

from __future__ import annotations

import time
from concurrent.futures import Future
from typing import Any

from market_data.snapshot_engine import ResourceKey, ResourceUpdate, SubmissionBacklogFullError
from market_data.snapshot_provider.submissions import SubmissionCoordinator

_ACCOUNT = ResourceKey("account", "summary")
_POSITION = ResourceKey("account", "position", "BTCUSDT")
_ORDERS = ResourceKey("orders", "open", "BTCUSDT")


def _make_update(key: ResourceKey) -> ResourceUpdate[Any]:
    return ResourceUpdate(
        key=key,
        value={"test": True},
        source="alphora-test",
        observed_at=time.time(),
    )


def _resolved_future(value: Any) -> Future[Any]:
    f: Future[Any] = Future()
    f.set_result(value)
    return f


def _failed_future(exc: Exception) -> Future[Any]:
    f: Future[Any] = Future()
    f.set_exception(exc)
    return f


def _cancelled_future() -> Future[Any]:
    f: Future[Any] = Future()
    f.cancel()
    return f


class FakePublisher:
    def __init__(self) -> None:
        self.publish_calls: list[list[ResourceUpdate[Any]]] = []
        self.invalidate_calls: list[tuple[list[ResourceKey], str]] = []
        self._publish_future: Future[Any] = _resolved_future({})
        self._invalidate_future: Future[Any] = _resolved_future(None)
        self.raise_backlog_on_submit = False

    def submit_publish_many(self, updates: Any) -> Future[Any]:
        if self.raise_backlog_on_submit:
            raise SubmissionBacklogFullError(limit=1024, pending=1025)
        self.publish_calls.append(list(updates))
        return self._publish_future

    def submit_invalidate_many(self, keys: Any, *, reason: str = "") -> Future[Any]:
        if self.raise_backlog_on_submit:
            raise SubmissionBacklogFullError(limit=1024, pending=1025)
        self.invalidate_calls.append((list(keys), reason))
        return self._invalidate_future

    def flush(self, *, timeout_seconds: Any = None) -> None:
        pass


def test_submit_calls_publish_and_invalidate_correctly():
    pub = FakePublisher()
    coord = SubmissionCoordinator(pub)

    update = _make_update(_POSITION)
    coord.submit([update], [_ACCOUNT], reason="test-reason")

    assert len(pub.publish_calls) == 1
    assert pub.publish_calls[0][0].key == _POSITION
    assert len(pub.invalidate_calls) == 1
    assert _ACCOUNT in pub.invalidate_calls[0][0]
    assert pub.invalidate_calls[0][1] == "test-reason"


def test_deduplication_removes_updates_when_key_also_invalidated():
    pub = FakePublisher()
    coord = SubmissionCoordinator(pub)

    update = _make_update(_POSITION)
    coord.submit([update], [_POSITION, _ACCOUNT], reason="test")

    assert len(pub.publish_calls) == 0, "POSITION should have been removed (also invalidated)"
    assert len(pub.invalidate_calls) == 1
    assert _POSITION in pub.invalidate_calls[0][0]
    assert _ACCOUNT in pub.invalidate_calls[0][0]


def test_empty_updates_and_invalidations_is_noop():
    pub = FakePublisher()
    coord = SubmissionCoordinator(pub)

    coord.submit([], [], reason="empty")

    assert pub.publish_calls == []
    assert pub.invalidate_calls == []


def test_invalidations_are_deduplicated():
    pub = FakePublisher()
    coord = SubmissionCoordinator(pub)

    coord.submit([], [_ACCOUNT, _ACCOUNT, _POSITION], reason="test")

    assert len(pub.invalidate_calls) == 1
    keys = pub.invalidate_calls[0][0]
    assert keys.count(_ACCOUNT) == 1, "Duplicate invalidation keys must be removed"


def test_backlog_full_adds_to_reconciliation_needed():
    pub = FakePublisher()
    pub.raise_backlog_on_submit = True
    coord = SubmissionCoordinator(pub)

    update = _make_update(_POSITION)
    coord.submit([update], [_ACCOUNT], reason="test")

    needed = coord.reconciliation_needed
    assert _POSITION in needed or _ACCOUNT in needed


def test_failed_future_adds_to_reconciliation_needed():
    pub = FakePublisher()
    pub._publish_future = _failed_future(RuntimeError("publish failed"))
    coord = SubmissionCoordinator(pub)

    update = _make_update(_POSITION)
    coord.submit([update], [], reason="test")

    assert _POSITION in coord.reconciliation_needed


def test_cancelled_future_adds_to_reconciliation():
    pub = FakePublisher()
    pub._publish_future = _cancelled_future()
    coord = SubmissionCoordinator(pub)

    update = _make_update(_POSITION)
    coord.submit([update], [], reason="test")

    assert _POSITION in coord.reconciliation_needed


def test_clear_reconciliation_needed_removes_keys():
    pub = FakePublisher()
    pub._publish_future = _failed_future(RuntimeError("err"))
    coord = SubmissionCoordinator(pub)

    update = _make_update(_POSITION)
    coord.submit([update], [], reason="test")

    assert _POSITION in coord.reconciliation_needed

    coord.clear_reconciliation_needed([_POSITION])
    assert _POSITION not in coord.reconciliation_needed


def test_pending_count_tracks_in_flight_futures():
    pub = FakePublisher()
    # Use an unresolved future to check pending count
    pending_future: Future[Any] = Future()
    pub._publish_future = pending_future
    coord = SubmissionCoordinator(pub)

    update = _make_update(_POSITION)
    coord.submit([update], [], reason="test")

    assert coord.pending_count == 1

    pending_future.set_result({})
    assert coord.pending_count == 0


def test_flush_delegates_to_publisher():
    pub = FakePublisher()
    flush_called = []

    def tracking_flush(*, timeout_seconds=None):
        flush_called.append(timeout_seconds)

    pub.flush = tracking_flush
    coord = SubmissionCoordinator(pub)

    coord.flush(timeout_seconds=5.0)
    assert flush_called == [5.0]


def test_flush_timeout_marks_pending_keys_for_reconciliation():
    pub = FakePublisher()
    pending_future: Future[Any] = Future()
    pub._publish_future = pending_future

    def timeout_flush(*, timeout_seconds=None):
        raise TimeoutError("synthetic timeout")

    pub.flush = timeout_flush
    coord = SubmissionCoordinator(pub)

    coord.submit([_make_update(_POSITION)], [], reason="test")
    summary = coord.flush(timeout_seconds=0.01)

    assert summary.timed_out is True
    assert summary.pending == 1
    assert _POSITION in coord.reconciliation_needed


def test_reconciliation_checkpoint_does_not_clear_new_failure():
    pub = FakePublisher()
    coord = SubmissionCoordinator(pub)

    coord.mark_reconciliation_needed([_POSITION])
    checkpoint = coord.reconciliation_checkpoint()
    coord.mark_reconciliation_needed([_POSITION])

    coord.clear_reconciled_checkpoint(checkpoint)

    assert _POSITION in coord.reconciliation_needed


def test_only_updates_submitted_when_no_invalidations():
    pub = FakePublisher()
    coord = SubmissionCoordinator(pub)

    update = _make_update(_POSITION)
    coord.submit([update], [], reason="test")

    assert len(pub.publish_calls) == 1
    assert pub.invalidate_calls == []


def test_only_invalidations_submitted_when_no_updates():
    pub = FakePublisher()
    coord = SubmissionCoordinator(pub)

    coord.submit([], [_ACCOUNT, _ORDERS], reason="inv-only")

    assert pub.publish_calls == []
    assert len(pub.invalidate_calls) == 1
    assert _ACCOUNT in pub.invalidate_calls[0][0]
    assert _ORDERS in pub.invalidate_calls[0][0]
