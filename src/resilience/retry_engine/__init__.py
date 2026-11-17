"""
Public API for Alphora's native retry engine.

This module exposes the stable imports users should rely on. Internal modules can
change, but these names should remain stable whenever possible.
"""

from __future__ import annotations

from resilience.retry_engine.budget import RetryBudget, RetryBudgetSnapshot
from resilience.retry_engine.context import AsyncRetryAttemptContext, RetryAttemptContext
from resilience.retry_engine.diagnostics import (
    PolicyHealthReport,
    PolicyWarning,
    RetryLoadEstimate,
    RetrySimulation,
    RetrySimulationAttempt,
)
from resilience.retry_engine.exceptions import (
    InvalidRetryConfigError,
    RelinkerError,
    RetryExhaustedError,
    TryAgain,
)
from resilience.retry_engine.http import (
    DEFAULT_RETRYABLE_STATUSES,
    DEFAULT_RETRYABLE_TRANSPORT_EXCEPTIONS,
    MAX_RETRY_AFTER_SECONDS,
    http_retry_policy,
    parse_retry_after,
    retry_after_delay,
    retry_if_status,
    should_retry_http_status,
)
from resilience.retry_engine.policy import RetryPolicy
from resilience.retry_engine.presets import background_job, database, fast, network, patient
from resilience.retry_engine.result import RetryResult
from resilience.retry_engine.retry import retry
from resilience.retry_engine.state import RetryState
from resilience.retry_engine.stats import RetryStats, RetryStatsSnapshot
from resilience.retry_engine.typing import RetryWrappedFunction

__all__ = [
    "AsyncRetryAttemptContext",
    "DEFAULT_RETRYABLE_STATUSES",
    "DEFAULT_RETRYABLE_TRANSPORT_EXCEPTIONS",
    "MAX_RETRY_AFTER_SECONDS",
    "InvalidRetryConfigError",
    "PolicyHealthReport",
    "PolicyWarning",
    "RetryAttemptContext",
    "RetryBudget",
    "RetryBudgetSnapshot",
    "RetryExhaustedError",
    "RetryLoadEstimate",
    "RelinkerError",
    "RetryPolicy",
    "RetryResult",
    "RetrySimulation",
    "RetrySimulationAttempt",
    "RetryState",
    "RetryStats",
    "RetryStatsSnapshot",
    "RetryWrappedFunction",
    "TryAgain",
    "background_job",
    "database",
    "fast",
    "http_retry_policy",
    "network",
    "parse_retry_after",
    "patient",
    "retry",
    "retry_after_delay",
    "retry_if_status",
    "should_retry_http_status",
]

__version__ = "1.3.2"
