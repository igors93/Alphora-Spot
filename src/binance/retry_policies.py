"""Retry policies and predicates specific to the Binance Futures REST API.

They build on the generic retry engine in ``resilience``. Keeping them here means
``resilience`` never imports the exchange adapter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import requests

from binance.futures_client import (
    TRANSIENT_STATUS_CODES,
    BinanceFuturesAPIError,
    BinanceFuturesNetworkError,
)
from resilience.budgets import BINANCE_READ_RETRY_BUDGET
from resilience.events import make_giveup_logger, make_retry_logger
from resilience.retry_engine import RetryPolicy
from resilience.retry_engine.http import http_retry_policy

if TYPE_CHECKING:
    from binance.futures_client import BinanceFuturesClient, RequestTimeout

_LOG = logging.getLogger("resilience")

# Minimum policy delay after sync_time before the retry fires. The delay is
# intentionally owned by Relinker so it appears in preview(), events and
# max_time() calculations.
_POST_SYNC_PAUSE_SECONDS = 0.05

# Maximum wall-clock skew (ms) where a sync attempt is expected to help.
# Above this the host clock is so far out that even a corrected timestamp may
# still fall outside recv_window on the next attempt; we log a clear warning.
_SYNC_HOPEFUL_THRESHOLD_MS = 55_000


def is_timestamp_out_of_sync(exc: BaseException | None, value: object) -> bool:
    return isinstance(exc, BinanceFuturesAPIError) and exc.code == -1021


def is_transient_binance_read_error(exc: BaseException | None, value: object) -> bool:
    if isinstance(exc, BinanceFuturesNetworkError):
        return True
    if isinstance(exc, BinanceFuturesAPIError):
        return exc.status_code in TRANSIENT_STATUS_CODES
    return False


# 418 is an IP ban: repeating the request extends it, so it is never retried. 429 is retried
# after its Retry-After (and the shared REST ledger refuses to send earlier).
BINANCE_PUBLIC_READ_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BINANCE_PUBLIC_READ_DEFAULT_DELAY_SECONDS = 1.0
BINANCE_PUBLIC_READ_MAX_DELAY_SECONDS = 30.0


def build_binance_public_read_policy(
    *,
    policy_name: str,
    attempts: int = 3,
    budget_key: str | None = None,
) -> RetryPolicy:
    """Build the shared retry policy for safe Binance public GET reads."""
    policy = (
        http_retry_policy(
            attempts=attempts,
            statuses=BINANCE_PUBLIC_READ_RETRY_STATUSES,
            transport_exceptions=(requests.RequestException,),
            default_delay=BINANCE_PUBLIC_READ_DEFAULT_DELAY_SECONDS,
            maximum_delay=BINANCE_PUBLIC_READ_MAX_DELAY_SECONDS,
            respect_retry_after=True,
        )
        .named(policy_name)
        .raise_on_result_exhausted()
    )
    if budget_key:
        policy = policy.with_retry_budget(BINANCE_READ_RETRY_BUDGET, key=budget_key)
    return policy


def _event_policy_name(event: Any) -> str:
    return (
        getattr(event, "policy_name", None)
        or getattr(getattr(event, "state", None), "policy_name", None)
        or "binance_timestamp_resync"
    )


def _event_attempt_number(event: Any) -> Any:
    return (
        getattr(event, "attempt_number", None)
        or getattr(getattr(event, "state", None), "attempt_number", None)
        or "?"
    )


def _make_sync_handler(
    client: BinanceFuturesClient,
    initial_generation: int,
    timeout: RequestTimeout | None,
) -> Any:
    """Return an on_retry handler that syncs time and coalesces concurrent retries."""

    expected_generation = initial_generation

    def _handler(event: Any) -> None:
        nonlocal expected_generation
        try:
            synced, new_offset_ms, generation = client._sync_time_if_generation_current(
                expected_generation,
                timeout=timeout,
            )
        except Exception as exc:
            # sync_time itself failed. Log sanitized metadata and let Relinker
            # continue with the current offset if another attempt remains.
            _LOG.warning(
                "sync_time failed during -1021 retry; "
                "retry will continue with current offset | "
                "policy=%s | attempt=%s | error_type=%s | "
                "status_code=%s | binance_code=%s",
                _event_policy_name(event),
                _event_attempt_number(event),
                type(exc).__name__,
                getattr(exc, "status_code", None),
                getattr(exc, "code", None),
            )
            return

        expected_generation = generation
        if not synced:
            return

        try:
            skew_ms = abs(int(new_offset_ms or 0))
        except (TypeError, ValueError):
            skew_ms = 0
        if skew_ms > _SYNC_HOPEFUL_THRESHOLD_MS:
            _LOG.warning(
                "Clock skew (>55 s) acima do recv_window; "
                "sync_time pode nao resolver o -1021 | "
                "offset_ms=%d | acao=corrija o NTP/chrony do host",
                new_offset_ms,
            )

    return _handler


# Exported for tests that inspect the base policy structure.
# The actual policy used at runtime is always built by
# build_binance_timestamp_resync_policy, not this bare template.
_BASE_TIMESTAMP_RESYNC: RetryPolicy = (
    RetryPolicy()
    .named("binance_timestamp_resync")
    .attempts(4)
    .fixed_delay(_POST_SYNC_PAUSE_SECONDS)
    .retry_if(is_timestamp_out_of_sync)
    .raise_last()
)


def build_binance_timestamp_resync_policy(
    client: BinanceFuturesClient,
    *,
    timeout: RequestTimeout | None = None,
    logger: logging.Logger | None = None,
) -> RetryPolicy:
    """Return a per-call policy that syncs time on -1021 before retrying.

    Behaviour:
    - Attempt 1: execute the request normally.
    - On -1021: sync through the client's generation guard, so concurrent
      -1021 retries can reuse a sync completed by another thread.
    - Relinker applies the 50 ms policy delay before the next attempt.
    - Attempt 4 failure: giveup logger fires; caller receives the original error.
    """
    sync_handler = _make_sync_handler(
        client,
        client._time_sync_generation_snapshot(),
        timeout,
    )

    return (
        RetryPolicy()
        .named("binance_timestamp_resync")
        .attempts(4)
        .fixed_delay(_POST_SYNC_PAUSE_SECONDS)
        .retry_if(is_timestamp_out_of_sync)
        .raise_last()
        .on_retry(sync_handler)
        .on_retry(make_retry_logger(logger))
        .on_event("after_giveup", make_giveup_logger(logger), failure_mode="isolate")
    )
