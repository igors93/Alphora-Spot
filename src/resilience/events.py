from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from resilience.retry_engine.event import RetryEvent

_LOGGER = logging.getLogger("resilience")


def make_retry_logger(
    logger: logging.Logger | None = None,
) -> Callable[[RetryEvent], None]:
    log = logger or _LOGGER

    def _handler(event: RetryEvent) -> None:
        state = event.state
        error = state.last_error if state is not None else event.error
        if not _should_log_retry_event(event, error):
            return
        log.warning(
            "Retry | policy=%s | fn=%s | attempt=%d | "
            "error_type=%s | status_code=%s | binance_code=%s | elapsed=%.3fs",
            (state.policy_name if state is not None else event.policy_name) or "unknown",
            state.function_name if state is not None else event.function_name,
            state.attempt_number if state is not None else event.attempt_number,
            type(error).__name__ if error else None,
            getattr(error, "status_code", None),
            getattr(error, "code", None),
            state.elapsed if state is not None else 0.0,
        )

    return _handler


def make_giveup_logger(
    logger: logging.Logger | None = None,
) -> Callable[[RetryEvent], None]:
    log = logger or _LOGGER

    def _handler(event: RetryEvent) -> None:
        state = event.state
        error = state.last_error if state is not None else event.error
        if not _should_log_retry_event(event, error):
            return
        log.error(
            "Retry exhausted | policy=%s | fn=%s | total_attempts=%d | "
            "error_type=%s | status_code=%s | binance_code=%s | elapsed=%.3fs",
            (state.policy_name if state is not None else event.policy_name) or "unknown",
            state.function_name if state is not None else event.function_name,
            state.attempt_number if state is not None else event.attempt_number,
            type(error).__name__ if error else None,
            getattr(error, "status_code", None),
            getattr(error, "code", None),
            state.elapsed if state is not None else 0.0,
        )

    return _handler


def _should_log_retry_event(event: RetryEvent, error: BaseException | None) -> bool:
    state = event.state
    policy_name = (state.policy_name if state is not None else event.policy_name) or ""
    if policy_name == "binance_timestamp_resync":
        return getattr(error, "code", None) == -1021
    return True
