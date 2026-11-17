from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from resilience.retry_engine import RetryBudget, RetryPolicy

T = TypeVar("T")


def run_polling(
    operation: Callable[[], T],
    *,
    attempts: int,
    interval_seconds: float,
    retry_if_result: Callable[[T], bool],
    fallback: Any,
    delay_before_first_attempt: bool = True,
    retry_exceptions: tuple[type[Exception], ...] = (),
    policy_name: str,
    budget: RetryBudget | None = None,
    budget_key: str | None = None,
    max_time_seconds: float | None = None,
    _sleep: Callable[[float], None] | None = None,
) -> Any:
    """Run a polling loop using a RetryPolicy.

    Parameters
    ----------
    operation:
        Callable invoked on each attempt. Must be idempotent (read-only).
    attempts:
        Total number of attempts. Must be >= 1.
    interval_seconds:
        Delay between attempts (and before the first attempt when
        ``delay_before_first_attempt`` is True).
    retry_if_result:
        Predicate receiving the return value of ``operation``. Return True to
        retry, False to stop and return the value.
    fallback:
        Value returned when all attempts are exhausted via result condition.
    delay_before_first_attempt:
        When True (default), sleep ``interval_seconds`` before the first call,
        preserving the original "sleep then check" loop semantics.
        When False, the first call runs immediately (check then sleep).
    retry_exceptions:
        Tuple of exception types to treat as transient. They are retried up to
        ``attempts`` times. All other exceptions propagate immediately. If all
        attempts fail with a transient exception, the last one is re-raised.
    policy_name:
        Label used in logs and RetryResult for observability.
    budget:
        Optional process-local retry budget applied only when provided.
    budget_key:
        Required stable key when ``budget`` is provided.
    max_time_seconds:
        Optional total policy time limit. This complements, but does not
        replace, per-request timeouts used by the operation.
    _sleep:
        Injectable sleep function for testing. Defaults to ``time.sleep``.
    """
    if attempts < 1:
        raise ValueError("attempts must be greater than zero")
    if budget is not None and not budget_key:
        raise ValueError("budget_key is required when budget is provided")

    sleep_fn = _sleep if _sleep is not None else time.sleep

    # Relinker calls the sleep function even for zero-delay policies. Wrap it
    # so only positive delays reach the underlying sleep (and test spy).
    def _effective_sleep(t: float) -> None:
        if t > 0:
            sleep_fn(t)

    if delay_before_first_attempt and interval_seconds > 0:
        _effective_sleep(interval_seconds)

    def _should_retry(exc: BaseException | None, result: Any) -> bool:
        if exc is not None:
            return bool(retry_exceptions) and isinstance(exc, retry_exceptions)
        return retry_if_result(result)

    policy: RetryPolicy = (
        RetryPolicy()
        .named(policy_name)
        .attempts(attempts)
        .fixed_delay(interval_seconds)
        .retry_if(_should_retry)
        .return_result()
        .with_sleep(_effective_sleep)
    )

    if budget is not None:
        policy = policy.with_retry_budget(budget, key=budget_key or "")

    if max_time_seconds is not None:
        policy = policy.max_time(max_time_seconds)

    outcome = policy.run(operation)

    if not outcome.exhausted:
        # retry_if returned False for a non-retried exception (succeeded=False):
        # re-raise it. When succeeded=True, last_error is from a prior attempt.
        if not outcome.succeeded and outcome.last_error is not None:
            raise outcome.last_error
        return outcome.last_value

    if outcome.exhausted_by_exception and outcome.last_error is not None:
        raise outcome.last_error

    return fallback
