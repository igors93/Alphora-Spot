"""Settings, startup policy and result types of the operational snapshot provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from market_data.snapshot_engine import BufferOverflowPolicy
from market_data.snapshot_provider.wire_names import (
    REASON_PRODUCTION_REQUIRES,
    REASON_TESTNET_REQUIRES,
    REQUIRED_KEY,
)

# Every timeout must stay this far below the budget that contains it.
_TIMEOUT_MARGIN_SECONDS = 0.05

_POSITIVE_SETTINGS = (
    "submission_flush_timeout_seconds",
    "shutdown_timeout_seconds",
    "sync_builder_shutdown_timeout_seconds",
    "copy_shutdown_timeout_seconds",
    "observability_shutdown_timeout_seconds",
    "deadline_seconds",
    "cycle_deadline_seconds",
    "market_stream_timeout_seconds",
    "rest_timeout_seconds",
    "rest_connect_timeout_seconds",
    "rest_read_timeout_seconds",
    "rest_transport_timeout_seconds",
    "rest_min_attempt_timeout_seconds",
    "exchange_info_timeout_seconds",
    "exchange_info_connect_timeout_seconds",
    "exchange_info_read_timeout_seconds",
    "exchange_info_transport_timeout_seconds",
    "exchange_info_bootstrap_timeout_seconds",
    "exchange_info_bootstrap_connect_timeout_seconds",
    "exchange_info_bootstrap_read_timeout_seconds",
    "evaluation_max_age_seconds",
    "evaluation_max_skew_seconds",
    "execution_max_age_seconds",
    "execution_max_skew_seconds",
    "exit_max_age_seconds",
    "exit_max_skew_seconds",
    "source_transport_timeout_grace_seconds",
)


class SnapshotTransportBudgetExceeded(TimeoutError):
    """Raised when a REST source cannot finish inside its declared budget."""


@dataclass(frozen=True)
class SnapshotProviderSettings:
    light_ttl_seconds: float = 2.0
    heavy_ttl_seconds: float = 8.0
    max_data_age_seconds: float = 30.0
    exchange_info_ttl_seconds: float = 21_600.0
    account_ttl_seconds: float = 8.0
    deadline_seconds: float = 3.0
    cycle_deadline_seconds: float = 30.0
    market_stream_timeout_seconds: float = 0.1
    rest_timeout_seconds: float = 2.8
    rest_connect_timeout_seconds: float = 0.6
    rest_read_timeout_seconds: float = 1.8
    rest_transport_timeout_seconds: float = 2.5
    rest_max_attempts: int = 4
    rest_retry_delay_seconds: float = 0.05
    rest_min_attempt_timeout_seconds: float = 0.15

    exchange_info_timeout_seconds: float = 2.0
    exchange_info_connect_timeout_seconds: float = 0.5
    exchange_info_read_timeout_seconds: float = 0.75
    exchange_info_transport_timeout_seconds: float = 1.5
    exchange_info_bootstrap_timeout_seconds: float = 9.5
    exchange_info_bootstrap_connect_timeout_seconds: float = 3.0
    exchange_info_bootstrap_read_timeout_seconds: float = 6.0

    exchange_info_bootstrap_attempts: int = 4
    exchange_info_bootstrap_base_delay_seconds: float = 1.0
    exchange_info_bootstrap_max_delay_seconds: float = 5.0

    evaluation_max_age_seconds: float = 15.0
    # exchange:rules has a 6-hour TTL and is bootstrapped at startup; on cold start it will always
    # be older than freshly-fetched operational data. 30s matches max_data_age_seconds.
    evaluation_max_skew_seconds: float = 30.0
    evaluation_allow_stale: bool = True
    execution_max_age_seconds: float = 3.0
    execution_max_skew_seconds: float = 3.0
    execution_allow_stale: bool = False
    execution_force_refresh: bool = True
    exit_max_age_seconds: float = 8.0
    exit_max_skew_seconds: float = 5.0
    exit_allow_stale: bool = False
    exit_force_refresh: bool = True

    operational_degradation_enabled: bool = True
    evaluation_degraded_min_resources: int = 3
    baseline_degraded_min_resources: int = 1
    risk_reduction_degraded_min_resources: int = 2
    partitioned_symbol_request_concurrency: int = 4
    partitioned_symbol_deadline_seconds: float | None = None

    max_concurrency: int = 8
    max_pending_tasks: int | None = None
    max_pending_submissions: int = 1024
    submission_flush_timeout_seconds: float = 5.0
    shutdown_timeout_seconds: float = 5.0
    sync_builder_shutdown_timeout_seconds: float = 5.0
    copy_shutdown_timeout_seconds: float = 5.0
    observability_shutdown_timeout_seconds: float = 5.0
    observability_drain_on_shutdown: bool = True
    observability_max_pending: int = 10_000
    observability_overflow: str = BufferOverflowPolicy.DROP_OLDEST.value
    source_transport_timeout_grace_seconds: float = 0.05
    source_deadline_dispatch_grace_seconds: float = 2.0
    rest_concurrency: int = 4
    stream_concurrency: int = 16
    derived_concurrency: int = 4
    user_data_concurrency: int = 16
    price_window: int = 120
    source_concurrency: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_counts(self)
        for name in _POSITIVE_SETTINGS:
            _require_positive(name, float(getattr(self, name)))
        _validate_observability(self)
        _validate_timeout_margins(self)
        _validate_retries(self)
        if self.sync_builder_shutdown_timeout_seconds < max(
            self.copy_shutdown_timeout_seconds,
            self.observability_shutdown_timeout_seconds,
        ):
            raise ValueError(
                "sync_builder_shutdown_timeout_seconds must cover copy and observability shutdown"
            )

    @classmethod
    def from_runtime(
        cls,
        *,
        light_ttl_seconds: float,
        heavy_ttl_seconds: float,
        max_data_age_seconds: float,
        price_window: int,
    ) -> SnapshotProviderSettings:
        return cls(
            light_ttl_seconds=max(0.0, float(light_ttl_seconds)),
            heavy_ttl_seconds=max(0.0, float(heavy_ttl_seconds)),
            max_data_age_seconds=max(0.0, float(max_data_age_seconds)),
            account_ttl_seconds=max(0.0, float(heavy_ttl_seconds)),
            price_window=max(1, int(price_window)),
        )


def _require_positive(name: str, value: float | int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_non_negative(name: str, value: float | int) -> None:
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _require_margin(smaller_name: str, smaller: float, larger_name: str, larger: float) -> None:
    if float(smaller) + _TIMEOUT_MARGIN_SECONDS >= float(larger):
        raise ValueError(
            f"{smaller_name} must be at least {_TIMEOUT_MARGIN_SECONDS:.2f}s lower than "
            f"{larger_name}"
        )


def _validate_counts(settings: SnapshotProviderSettings) -> None:
    for name in (
        "evaluation_degraded_min_resources",
        "baseline_degraded_min_resources",
        "risk_reduction_degraded_min_resources",
        "partitioned_symbol_request_concurrency",
    ):
        if getattr(settings, name) < 1:
            raise ValueError(f"{name} must be at least 1")

    if (
        settings.partitioned_symbol_deadline_seconds is not None
        and settings.partitioned_symbol_deadline_seconds <= 0
    ):
        raise ValueError("partitioned_symbol_deadline_seconds must be positive or None")

    if settings.max_pending_tasks is not None and settings.max_pending_tasks < 1:
        raise ValueError("max_pending_tasks must be at least 1 or None")

    if settings.max_pending_submissions < 1:
        raise ValueError("max_pending_submissions must be at least 1")


def _validate_observability(settings: SnapshotProviderSettings) -> None:
    if settings.observability_max_pending < 1:
        raise ValueError("observability_max_pending must be at least 1")

    try:
        BufferOverflowPolicy(settings.observability_overflow)
    except ValueError:
        allowed = ", ".join(item.value for item in BufferOverflowPolicy)
        raise ValueError(f"observability_overflow must be one of: {allowed}") from None


def _validate_timeout_margins(settings: SnapshotProviderSettings) -> None:
    _require_margin(
        "deadline_seconds",
        settings.deadline_seconds,
        "cycle_deadline_seconds",
        settings.cycle_deadline_seconds,
    )
    _require_margin(
        "rest_timeout_seconds",
        settings.rest_timeout_seconds,
        "deadline_seconds",
        settings.deadline_seconds,
    )
    _require_margin(
        "rest_transport_timeout_seconds",
        settings.rest_transport_timeout_seconds,
        "rest_timeout_seconds",
        settings.rest_timeout_seconds,
    )
    _require_margin(
        "REST connect + read timeout",
        settings.rest_connect_timeout_seconds + settings.rest_read_timeout_seconds,
        "rest_transport_timeout_seconds",
        settings.rest_transport_timeout_seconds,
    )
    _require_margin(
        "exchange_info_timeout_seconds",
        settings.exchange_info_timeout_seconds,
        "deadline_seconds",
        settings.deadline_seconds,
    )
    _require_margin(
        "exchange_info_transport_timeout_seconds",
        settings.exchange_info_transport_timeout_seconds,
        "exchange_info_timeout_seconds",
        settings.exchange_info_timeout_seconds,
    )
    _require_margin(
        "exchangeInfo connect + read timeout",
        settings.exchange_info_connect_timeout_seconds
        + settings.exchange_info_read_timeout_seconds,
        "exchange_info_transport_timeout_seconds",
        settings.exchange_info_transport_timeout_seconds,
    )
    _require_margin(
        "exchangeInfo bootstrap connect + read timeout",
        settings.exchange_info_bootstrap_connect_timeout_seconds
        + settings.exchange_info_bootstrap_read_timeout_seconds,
        "exchange_info_bootstrap_timeout_seconds",
        settings.exchange_info_bootstrap_timeout_seconds,
    )


def _validate_retries(settings: SnapshotProviderSettings) -> None:
    if settings.rest_min_attempt_timeout_seconds >= settings.rest_transport_timeout_seconds:
        raise ValueError(
            "rest_min_attempt_timeout_seconds must be lower than rest_transport_timeout_seconds"
        )

    if settings.rest_max_attempts < 1:
        raise ValueError("rest_max_attempts must be at least 1")

    _require_non_negative("rest_retry_delay_seconds", settings.rest_retry_delay_seconds)
    _require_non_negative(
        "source_deadline_dispatch_grace_seconds",
        settings.source_deadline_dispatch_grace_seconds,
    )

    if settings.exchange_info_bootstrap_attempts < 1:
        raise ValueError("exchange_info_bootstrap_attempts must be at least 1")

    _require_non_negative(
        "exchange_info_bootstrap_base_delay_seconds",
        settings.exchange_info_bootstrap_base_delay_seconds,
    )
    _require_non_negative(
        "exchange_info_bootstrap_max_delay_seconds",
        settings.exchange_info_bootstrap_max_delay_seconds,
    )

    if (
        settings.exchange_info_bootstrap_base_delay_seconds
        > settings.exchange_info_bootstrap_max_delay_seconds
    ):
        raise ValueError(
            "exchange_info_bootstrap_base_delay_seconds must not exceed "
            "exchange_info_bootstrap_max_delay_seconds"
        )


@dataclass(frozen=True)
class ExchangeInfoBootstrapResult:
    attempts: int
    duration_ms: float
    symbol_count: int
    from_cache: bool
    stale: bool


@dataclass(frozen=True)
class SubmissionReconciliationResult:
    attempted: int = 0
    reconciled: int = 0
    revalidated: int = 0
    invalidated: int = 0
    failed: int = 0
    pending_after: int = 0
    success: bool = True
    reason: str = ""
    error_type: str = ""


@dataclass(frozen=True)
class SnapshotStartupPolicy:
    """Whether a run may start without the snapshot provider, decided by execution mode."""

    execution_mode: str
    environment: str
    required: bool
    fallback_allowed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        # ``REQUIRED_KEY`` is the persisted name of ``required`` (ADR-002).
        return {
            "execution_mode": self.execution_mode,
            "environment": self.environment,
            REQUIRED_KEY: self.required,
            "fallback_allowed": self.fallback_allowed,
            "reason": self.reason,
        }


def resolve_startup_policy(
    *,
    execution_mode: Any = None,
    environment: Any = None,
    fallback_allowed: bool | None = None,
) -> SnapshotStartupPolicy:
    """Live modes require the provider; paper needs an explicit opt-in to run without it."""

    mode = _enum_value_for_policy(execution_mode, default="DRY_RUN")
    env = _enum_value_for_policy(environment, default="DEVELOPMENT")
    if mode == "REAL_LIVE":
        return SnapshotStartupPolicy(
            execution_mode=mode,
            environment=env or "REAL",
            required=True,
            fallback_allowed=False,
            reason=REASON_PRODUCTION_REQUIRES,
        )
    if mode == "TESTNET_LIVE":
        return SnapshotStartupPolicy(
            execution_mode=mode,
            environment=env or "TESTNET",
            required=True,
            fallback_allowed=False,
            reason=REASON_TESTNET_REQUIRES,
        )
    if mode == "PAPER_LIVE":
        explicit_fallback = bool(fallback_allowed) if fallback_allowed is not None else False
        return SnapshotStartupPolicy(
            execution_mode=mode,
            environment=env,
            required=not explicit_fallback,
            fallback_allowed=explicit_fallback,
            reason=(
                "paper_explicit_fallback_allowed"
                if explicit_fallback
                else "paper_requires_explicit_fallback"
            ),
        )
    development_fallback = True if fallback_allowed is None else bool(fallback_allowed)
    return SnapshotStartupPolicy(
        execution_mode=mode,
        environment=env,
        required=not development_fallback,
        fallback_allowed=development_fallback,
        reason=(
            "development_fallback_allowed"
            if development_fallback
            else "development_fallback_disabled"
        ),
    )


def _enum_value_for_policy(value: Any, *, default: str) -> str:
    if value is None:
        return default
    raw = getattr(value, "value", value)
    return str(raw or default).upper().strip()


__all__ = [
    "ExchangeInfoBootstrapResult",
    "SnapshotProviderSettings",
    "SnapshotStartupPolicy",
    "SnapshotTransportBudgetExceeded",
    "SubmissionReconciliationResult",
    "resolve_startup_policy",
]
