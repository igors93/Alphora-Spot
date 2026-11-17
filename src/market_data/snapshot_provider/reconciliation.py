"""Reconcile cache keys whose async User Data submission failed, timed out or was cancelled."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from market_data.snapshot_engine import ResourceKey
from market_data.snapshot_provider import wire_names
from market_data.snapshot_provider.diagnostics import snapshot_error_diagnostics_payload
from market_data.snapshot_provider.health import COMPONENT
from market_data.snapshot_provider.settings import SubmissionReconciliationResult
from utils.events import EventChannel, observe_error, observe_event

if TYPE_CHECKING:
    from market_data.snapshot_provider.provider import SnapshotProvider
    from market_data.snapshot_provider.session import SnapshotCycleSession


def reconcile_pending_submissions(
    provider: SnapshotProvider,
    *,
    session: SnapshotCycleSession | None = None,
    keys: Iterable[ResourceKey] | None = None,
    reason: str = "manual",
) -> SubmissionReconciliationResult:
    """Consume pending async-submission reconciliation work.

    Without a cycle session, pending keys are invalidated so the next resolution cannot reuse a
    possibly stale cache entry. With a session, keys are resolved if needed and then revalidated
    with force_refresh=True so critical execution resources come from live sources instead of
    the shared cache. Checkpoints are cleared only when their generation still matches.
    """
    submissions = provider.submissions
    checkpoint = submissions.reconciliation_checkpoint(keys)
    if not checkpoint:
        return SubmissionReconciliationResult(pending_after=len(submissions.reconciliation_needed))

    pending_keys = tuple(checkpoint)
    revalidated = 0
    invalidated = 0
    success = True
    failure_reason = ""
    error_type = ""

    try:
        if session is None:
            provider.publisher.invalidate_many(
                pending_keys,
                reason=f"alphora-reconcile-pending-submission:{reason}",
            )
            invalidated = len(pending_keys)
        else:
            session.session.resolve(pending_keys, strict=True)
            provider.last_snapshot = session.session.revalidate(
                pending_keys,
                strict=True,
                force_refresh=True,
            )
            revalidated = len(pending_keys)
        submissions.clear_reconciled_checkpoint(checkpoint)
    except Exception as error:
        success = False
        failure_reason = str(error)
        error_type = type(error).__name__
        observe_error(
            wire_names.SUBMISSION_RECONCILIATION_FAILED,
            error=error,
            component=COMPONENT,
            category="market_data",
            operation="reconcile_pending_submissions",
            reason=reason,
            attempted=len(pending_keys),
            error_type=error_type,
            message=failure_reason,
            diagnostics=snapshot_error_diagnostics_payload(error),
        )
        provider.collect_health("reconciliation_failed")

    result = SubmissionReconciliationResult(
        attempted=len(pending_keys),
        reconciled=revalidated + invalidated if success else 0,
        revalidated=revalidated,
        invalidated=invalidated,
        failed=0 if success else len(pending_keys),
        pending_after=len(submissions.reconciliation_needed),
        success=success,
        reason=failure_reason,
        error_type=error_type,
    )
    observe_event(
        wire_names.SUBMISSION_RECONCILIATION,
        component=__name__,
        category="market_data",
        channel=EventChannel.AUDIT,
        reason=reason,
        attempted=result.attempted,
        reconciled=result.reconciled,
        revalidated=result.revalidated,
        invalidated=result.invalidated,
        failed=result.failed,
        pending_after=result.pending_after,
        success=result.success,
        error_type=result.error_type,
    )
    if success:
        provider.collect_health("reconciliation_succeeded")
    return result


__all__ = ["reconcile_pending_submissions"]
