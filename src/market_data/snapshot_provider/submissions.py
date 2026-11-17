"""Tracking and deduplication for async snapshot submission futures."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from market_data.snapshot_engine import (
    ResourceKey,
    ResourceUpdate,
    SubmissionBacklogFullError,
    SyncResourcePublisher,
)
from market_data.snapshot_provider import wire_names
from observability import observe_error


@dataclass(frozen=True)
class SubmissionSubmitResult:
    submitted_publish: bool = False
    submitted_invalidate: bool = False
    backlog_full: bool = False
    affected_keys: int = 0


@dataclass(frozen=True)
class SubmissionFlushSummary:
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    pending: int = 0
    timed_out: bool = False


@dataclass(frozen=True)
class _TrackedSubmission:
    keys: frozenset[ResourceKey]
    operation: str


class SubmissionCoordinator:
    """Manages async snapshot submissions with deduplication, tracking, and error observability.

    Wraps SyncResourcePublisher's non-blocking submit_publish_many / submit_invalidate_many
    to add:
    - Key deduplication: keys present in invalidations are removed from the updates batch
      because an invalidation takes precedence over a potentially stale concurrent publish.
    - Future tracking with done callbacks for per-outcome observability.
    - Reconciliation tracking: keys whose futures failed are flagged for re-fetch.
    - Flush support for orderly shutdown.
    """

    def __init__(
        self,
        publisher: SyncResourcePublisher,
        *,
        component: str = "market_data.snapshot_submissions",
    ) -> None:
        self._publisher = publisher
        self._component = component
        self._lock = threading.Lock()
        self._pending: dict[Future[Any], _TrackedSubmission] = {}
        self._reconciliation_needed: dict[ResourceKey, int] = {}

    @property
    def pending_count(self) -> int:
        """Number of in-flight async submission futures."""
        with self._lock:
            return len(self._pending)

    @property
    def reconciliation_needed(self) -> frozenset[ResourceKey]:
        """Keys whose submission failed and may need manual reconciliation."""
        with self._lock:
            return frozenset(self._reconciliation_needed)

    def reconciliation_checkpoint(
        self,
        keys: Iterable[ResourceKey] | None = None,
    ) -> dict[ResourceKey, int]:
        """Return an atomic reconciliation checkpoint keyed by ResourceKey.

        The integer token changes every time the same key is marked again. Callers can clear a
        checkpoint only when the token still matches, which prevents a fresh failure from being
        erased by an older reconciliation attempt.
        """
        key_filter = frozenset(keys) if keys is not None else None
        with self._lock:
            if key_filter is None:
                return dict(self._reconciliation_needed)
            return {
                key: generation
                for key, generation in self._reconciliation_needed.items()
                if key in key_filter
            }

    def clear_reconciled_checkpoint(self, checkpoint: Mapping[ResourceKey, int]) -> None:
        """Clear only keys whose generation still matches the processed checkpoint."""
        with self._lock:
            for key, generation in checkpoint.items():
                if self._reconciliation_needed.get(key) == generation:
                    self._reconciliation_needed.pop(key, None)

    def clear_reconciliation_needed(self, keys: Iterable[ResourceKey]) -> None:
        """Remove keys from the reconciliation-needed set after successful revalidation."""
        with self._lock:
            for key in keys:
                self._reconciliation_needed.pop(key, None)

    def mark_reconciliation_needed(self, keys: Iterable[ResourceKey]) -> None:
        """Mark keys whose asynchronous result is unknown or failed."""
        with self._lock:
            self._mark_reconciliation_needed_locked(frozenset(keys))

    def track_future(
        self,
        future: Future[Any],
        *,
        keys: Iterable[ResourceKey],
        operation: str,
    ) -> None:
        """Track a Future created by direct publisher APIs."""
        self._track(future, keys=frozenset(keys), operation=operation)

    def submit(
        self,
        updates: list[ResourceUpdate[Any]],
        invalidations: list[ResourceKey],
        *,
        reason: str,
    ) -> SubmissionSubmitResult:
        """Deduplicate and submit updates and invalidations as non-blocking calls.

        Raises SubmissionBacklogFullError only if the publisher queue is full; that error
        is caught internally, logged, and affected keys are added to reconciliation_needed.
        """
        invalidation_set = frozenset(invalidations)
        deduped_updates = [u for u in updates if u.key not in invalidation_set]
        deduped_invalidations = list(dict.fromkeys(invalidations))
        submitted_publish = False
        submitted_invalidate = False

        try:
            if deduped_updates:
                pub_future: Future[Any] = self._publisher.submit_publish_many(deduped_updates)
                submitted_publish = True
                self._track(
                    pub_future,
                    keys=frozenset(u.key for u in deduped_updates),
                    operation="publish",
                )
            if deduped_invalidations:
                inv_future: Future[Any] = self._publisher.submit_invalidate_many(
                    deduped_invalidations, reason=reason
                )
                submitted_invalidate = True
                self._track(
                    inv_future,
                    keys=frozenset(deduped_invalidations),
                    operation="invalidate",
                )
        except SubmissionBacklogFullError as backlog_error:
            affected = frozenset(u.key for u in updates) | frozenset(invalidations)
            with self._lock:
                self._mark_reconciliation_needed_locked(affected)
            observe_error(
                "submission_backlog_full",
                error=backlog_error,
                component=self._component,
                category="market_data",
                resource=wire_names.ERROR_SOURCE_PUBLISHER,
                operation="submit",
                error_type="SubmissionBacklogFullError",
                message="submission backlog is full; event dropped and marked for reconciliation",
                affected_keys=len(affected),
            )
            return SubmissionSubmitResult(
                submitted_publish=submitted_publish,
                submitted_invalidate=submitted_invalidate,
                backlog_full=True,
                affected_keys=len(affected),
            )

        return SubmissionSubmitResult(
            submitted_publish=submitted_publish,
            submitted_invalidate=submitted_invalidate,
            affected_keys=len(
                frozenset(u.key for u in deduped_updates) | frozenset(deduped_invalidations)
            ),
        )

    def flush(self, timeout_seconds: float | None = None) -> SubmissionFlushSummary:
        """Flush all pending submissions through the underlying publisher."""
        with self._lock:
            checkpoint = dict(self._pending)
        timed_out = False
        try:
            self._publisher.flush(timeout_seconds=timeout_seconds)
        except TimeoutError as timeout_error:
            timed_out = True
            with self._lock:
                for future, tracked in self._pending.items():
                    if future in checkpoint and not future.done():
                        self._mark_reconciliation_needed_locked(tracked.keys)
            observe_error(
                "submission_flush_timeout",
                error=timeout_error,
                component=self._component,
                category="market_data",
                source=wire_names.ERROR_SOURCE_PUBLISHER,
                operation="flush",
                timeout_seconds=timeout_seconds,
                pending=len(self.pending_snapshot()),
            )
        return self._summarize_futures(checkpoint, timed_out=timed_out)

    def pending_snapshot(self) -> dict[Future[Any], tuple[frozenset[ResourceKey], str]]:
        """Return an atomic snapshot of pending futures and their affected keys."""
        with self._lock:
            return {
                future: (tracked.keys, tracked.operation)
                for future, tracked in self._pending.items()
            }

    def mark_pending_for_reconciliation(self) -> int:
        """Mark all currently pending submissions for reconciliation."""
        with self._lock:
            affected: set[ResourceKey] = set()
            for tracked in self._pending.values():
                affected.update(tracked.keys)
            self._mark_reconciliation_needed_locked(frozenset(affected))
            return len(affected)

    def cancel_pending(self) -> int:
        """Cancel all currently pending futures and return how many were requested."""
        with self._lock:
            futures = tuple(self._pending)
        cancelled = 0
        for future in futures:
            if future.cancel():
                cancelled += 1
        return cancelled

    def _track(
        self,
        future: Future[Any],
        *,
        keys: frozenset[ResourceKey],
        operation: str,
    ) -> None:
        with self._lock:
            self._pending[future] = _TrackedSubmission(keys=keys, operation=operation)

        def _on_done(f: Future[Any]) -> None:
            with self._lock:
                tracked = self._pending.pop(f, _TrackedSubmission(keys=keys, operation=operation))

            if f.cancelled():
                with self._lock:
                    self._mark_reconciliation_needed_locked(tracked.keys)
                observe_error(
                    "submission_cancelled",
                    error=RuntimeError(f"snapshot {operation} future was cancelled"),
                    component=self._component,
                    category="market_data",
                    source=wire_names.ERROR_SOURCE_PUBLISHER,
                    operation=operation,
                    affected_keys=len(keys),
                )
                return

            exc = f.exception()
            if exc is not None:
                with self._lock:
                    self._mark_reconciliation_needed_locked(tracked.keys)
                observe_error(
                    "submission_failed",
                    error=exc,
                    component=self._component,
                    category="market_data",
                    source=wire_names.ERROR_SOURCE_PUBLISHER,
                    operation=operation,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    affected_keys=len(keys),
                )

        future.add_done_callback(_on_done)

    def _mark_reconciliation_needed_locked(self, keys: frozenset[ResourceKey]) -> None:
        for key in keys:
            self._reconciliation_needed[key] = self._reconciliation_needed.get(key, 0) + 1

    @staticmethod
    def _summarize_futures(
        futures: Mapping[Future[Any], _TrackedSubmission],
        *,
        timed_out: bool,
    ) -> SubmissionFlushSummary:
        completed = 0
        failed = 0
        cancelled = 0
        pending = 0
        for future in futures:
            if future.cancelled():
                cancelled += 1
            elif not future.done():
                pending += 1
            elif future.exception() is not None:
                failed += 1
            else:
                completed += 1
        return SubmissionFlushSummary(
            completed=completed,
            failed=failed,
            cancelled=cancelled,
            pending=pending,
            timed_out=timed_out,
        )


__all__ = ["SubmissionCoordinator", "SubmissionFlushSummary", "SubmissionSubmitResult"]
