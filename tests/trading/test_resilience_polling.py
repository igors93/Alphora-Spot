"""Tests for resilience.polling.run_polling."""

from __future__ import annotations

import pytest

from resilience.polling import run_polling
from resilience.retry_engine import RetryBudget


class _TransientError(Exception):
    pass


class _FatalError(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_counter(values):
    """Return a callable that yields successive values, raising if it's an Exception."""
    seq = iter(values)

    def _fn():
        v = next(seq)
        if isinstance(v, BaseException):
            raise v
        return v

    return _fn


def _no_sleep(seconds):
    pass  # deterministic: no real delays


# ---------------------------------------------------------------------------
# Basic validation
# ---------------------------------------------------------------------------


def test_attempts_zero_raises():
    with pytest.raises(ValueError, match="attempts must be greater than zero"):
        run_polling(
            lambda: True,
            attempts=0,
            interval_seconds=0.1,
            retry_if_result=lambda x: not x,
            fallback=False,
            policy_name="test",
            _sleep=_no_sleep,
        )


def test_attempts_negative_raises():
    with pytest.raises(ValueError, match="attempts must be greater than zero"):
        run_polling(
            lambda: True,
            attempts=-1,
            interval_seconds=0.1,
            retry_if_result=lambda x: not x,
            fallback=False,
            policy_name="test",
            _sleep=_no_sleep,
        )


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------


def test_success_on_first_attempt():
    calls = []

    def _op():
        calls.append(1)
        return "good"

    result = run_polling(
        _op,
        attempts=5,
        interval_seconds=0.1,
        retry_if_result=lambda x: x != "good",
        fallback="FALLBACK",
        delay_before_first_attempt=False,
        policy_name="test",
        _sleep=_no_sleep,
    )

    assert result == "good"
    assert len(calls) == 1


def test_success_on_last_attempt():
    values = ["bad", "bad", "good"]
    fn = _make_counter(values)
    calls = []

    def _op():
        v = fn()
        calls.append(v)
        return v

    result = run_polling(
        _op,
        attempts=3,
        interval_seconds=0.0,
        retry_if_result=lambda x: x != "good",
        fallback="FALLBACK",
        delay_before_first_attempt=False,
        policy_name="test",
        _sleep=_no_sleep,
    )

    assert result == "good"
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Exhaustion cases
# ---------------------------------------------------------------------------


def test_result_exhaustion_returns_fallback():
    calls = []

    def _op():
        calls.append(1)
        return "bad"

    result = run_polling(
        _op,
        attempts=3,
        interval_seconds=0.0,
        retry_if_result=lambda x: x == "bad",
        fallback="FALLBACK",
        delay_before_first_attempt=False,
        policy_name="test",
        _sleep=_no_sleep,
    )

    assert result == "FALLBACK"
    assert len(calls) == 3


def test_result_exhaustion_with_none_fallback():
    result = run_polling(
        lambda: "bad",
        attempts=2,
        interval_seconds=0.0,
        retry_if_result=lambda x: True,
        fallback=None,
        delay_before_first_attempt=False,
        policy_name="test",
        _sleep=_no_sleep,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Exception cases
# ---------------------------------------------------------------------------


def test_fatal_exception_propagates_immediately():
    calls = []

    def _op():
        calls.append(1)
        raise _FatalError("fatal")

    with pytest.raises(_FatalError):
        run_polling(
            _op,
            attempts=5,
            interval_seconds=0.0,
            retry_if_result=lambda x: True,
            fallback=None,
            retry_exceptions=(_TransientError,),
            delay_before_first_attempt=False,
            policy_name="test",
            _sleep=_no_sleep,
        )

    assert len(calls) == 1


def test_transient_exception_retried():
    invoke_count = 0
    values = [_TransientError("net"), _TransientError("net"), "good"]
    fn = _make_counter(values)

    def _op():
        nonlocal invoke_count
        invoke_count += 1
        return fn()

    result = run_polling(
        _op,
        attempts=3,
        interval_seconds=0.0,
        retry_if_result=lambda x: x != "good",
        fallback=None,
        retry_exceptions=(_TransientError,),
        delay_before_first_attempt=False,
        policy_name="test",
        _sleep=_no_sleep,
    )

    assert result == "good"
    assert invoke_count == 3


def test_transient_exception_exhaustion_raises():
    calls = []

    def _op():
        calls.append(1)
        raise _TransientError("net")

    with pytest.raises(_TransientError):
        run_polling(
            _op,
            attempts=3,
            interval_seconds=0.0,
            retry_if_result=lambda x: True,
            fallback=None,
            retry_exceptions=(_TransientError,),
            delay_before_first_attempt=False,
            policy_name="test",
            _sleep=_no_sleep,
        )

    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Delay semantics
# ---------------------------------------------------------------------------


def test_delay_before_first_attempt_true_initial_sleep_added():
    sleeps = []

    def _op():
        return "done"

    run_polling(
        _op,
        attempts=1,
        interval_seconds=0.5,
        retry_if_result=lambda x: False,
        fallback=None,
        delay_before_first_attempt=True,
        policy_name="test",
        _sleep=sleeps.append,
    )

    # One sleep before the first (and only) attempt
    assert sleeps == [0.5]


def test_delay_before_first_attempt_false_no_initial_sleep():
    sleeps = []

    def _op():
        return "done"

    run_polling(
        _op,
        attempts=1,
        interval_seconds=0.5,
        retry_if_result=lambda x: False,
        fallback=None,
        delay_before_first_attempt=False,
        policy_name="test",
        _sleep=sleeps.append,
    )

    # No sleeps: first attempt runs immediately with no result retry
    assert sleeps == []


def test_delay_count_with_three_attempts_delay_before():
    """attempts=3, delay_before=True -> 1 initial + 2 between = 3 sleeps, 3 calls."""
    sleeps = []
    calls = []

    def _op():
        calls.append(1)
        return "bad"

    run_polling(
        _op,
        attempts=3,
        interval_seconds=0.3,
        retry_if_result=lambda x: True,
        fallback=None,
        delay_before_first_attempt=True,
        policy_name="test",
        _sleep=sleeps.append,
    )

    assert len(calls) == 3
    assert len(sleeps) == 3  # 1 initial + 2 between-attempt
    assert all(s == 0.3 for s in sleeps)


def test_delay_count_with_three_attempts_no_delay_before():
    """attempts=3, delay_before=False -> 0 initial + 2 between = 2 sleeps, 3 calls."""
    sleeps = []
    calls = []

    def _op():
        calls.append(1)
        return "bad"

    run_polling(
        _op,
        attempts=3,
        interval_seconds=0.3,
        retry_if_result=lambda x: True,
        fallback=None,
        delay_before_first_attempt=False,
        policy_name="test",
        _sleep=sleeps.append,
    )

    assert len(calls) == 3
    assert len(sleeps) == 2
    assert all(s == 0.3 for s in sleeps)


def test_attempts_one_with_delay_before():
    """attempts=1: exactly 1 initial sleep, 1 call."""
    sleeps = []
    calls = []

    def _op():
        calls.append(1)
        return "bad"

    result = run_polling(
        _op,
        attempts=1,
        interval_seconds=0.5,
        retry_if_result=lambda x: True,
        fallback="FB",
        delay_before_first_attempt=True,
        policy_name="test",
        _sleep=sleeps.append,
    )

    assert result == "FB"
    assert len(calls) == 1
    assert sleeps == [0.5]


# ---------------------------------------------------------------------------
# Zero interval
# ---------------------------------------------------------------------------


def test_zero_interval_no_sleep_even_with_delay_before():
    sleeps = []
    calls = []

    def _op():
        calls.append(1)
        return "bad"

    run_polling(
        _op,
        attempts=2,
        interval_seconds=0.0,
        retry_if_result=lambda x: True,
        fallback=None,
        delay_before_first_attempt=True,
        policy_name="test",
        _sleep=sleeps.append,
    )

    # interval_seconds=0 -> guard skips initial sleep
    assert len(calls) == 2
    assert sleeps == []


# ---------------------------------------------------------------------------
# RetryBudget and max_time integration
# ---------------------------------------------------------------------------


def test_budget_key_required_when_budget_is_provided():
    budget = RetryBudget(max_retries=1, per=60)

    with pytest.raises(ValueError, match="budget_key is required"):
        run_polling(
            lambda: "bad",
            attempts=2,
            interval_seconds=0.0,
            retry_if_result=lambda x: True,
            fallback=None,
            delay_before_first_attempt=False,
            policy_name="test",
            budget=budget,
            _sleep=_no_sleep,
        )


def test_budget_applied_when_configured():
    budget = RetryBudget(max_retries=1, per=60)
    sleeps = []
    calls = []

    def _op():
        calls.append(1)
        return "bad"

    result = run_polling(
        _op,
        attempts=3,
        interval_seconds=0.0,
        retry_if_result=lambda x: True,
        fallback="FB",
        delay_before_first_attempt=False,
        policy_name="test",
        budget=budget,
        budget_key="polling:test",
        _sleep=sleeps.append,
    )

    assert result == "FB"
    assert len(calls) == 3
    assert any(delay > 0 for delay in sleeps)
    assert budget.snapshot("polling:test").queued >= 1


def test_no_budget_preserves_zero_interval_no_sleep():
    sleeps = []
    calls = []

    def _op():
        calls.append(1)
        return "bad"

    run_polling(
        _op,
        attempts=3,
        interval_seconds=0.0,
        retry_if_result=lambda x: True,
        fallback=None,
        delay_before_first_attempt=False,
        policy_name="test",
        _sleep=sleeps.append,
    )

    assert len(calls) == 3
    assert sleeps == []


def test_max_time_returns_fallback_before_next_delayed_attempt():
    sleeps = []
    calls = []

    def _op():
        calls.append(1)
        return "bad"

    result = run_polling(
        _op,
        attempts=3,
        interval_seconds=1.0,
        retry_if_result=lambda x: True,
        fallback="FB",
        delay_before_first_attempt=False,
        policy_name="test",
        max_time_seconds=0.5,
        _sleep=sleeps.append,
    )

    assert result == "FB"
    assert len(calls) == 1
    assert sleeps == []
