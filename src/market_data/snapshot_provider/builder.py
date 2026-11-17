"""Assemble the snapshot engine builder: sources, freshness, resilience and authority."""

from __future__ import annotations

from typing import Any

from market_data.snapshot_engine import (
    BufferOverflowPolicy,
    CallableBatchSource,
    CallableDerivedSource,
    CallableSource,
    CircuitBreakerPolicy,
    CircuitScope,
    FreshnessPolicy,
    ObservationPolicy,
    PolicyResolver,
    RefreshMode,
    ResourceKey,
    SnapshotBuilder,
    SourceResiliencePolicy,
)
from market_data.snapshot_engine import RetryPolicy as EngineRetryPolicy
from market_data.snapshot_provider.fetchers import SourceFetchers
from market_data.snapshot_provider.keys import ACCOUNT, ALL_POSITIONS, EXCHANGE_INFO, is_key
from market_data.snapshot_provider.settings import SnapshotProviderSettings
from market_data.snapshot_provider.sources import (
    SOURCE_MARKET_STREAM,
    SOURCE_POSITION_DERIVED,
    SOURCE_REST_EXCHANGE,
    SOURCE_REST_MARKET,
    SOURCE_REST_PRIVATE,
    SOURCE_RULES_DERIVED,
    SOURCE_USER_DATA_CACHE,
    build_authority_resolver,
)


def freshness(
    ttl_seconds: float,
    max_stale_seconds: float,
    *,
    refresh_mode: RefreshMode = RefreshMode.BLOCKING,
    refresh_ahead_seconds: float = 0.0,
) -> FreshnessPolicy:
    ttl = max(0.0, float(ttl_seconds))
    max_stale = max(ttl, float(max_stale_seconds))
    ahead = max(0.0, float(refresh_ahead_seconds))
    if refresh_mode is not RefreshMode.REFRESH_AHEAD:
        ahead = 0.0
    if refresh_mode is RefreshMode.REFRESH_AHEAD and ttl <= 0:
        refresh_mode = RefreshMode.BLOCKING
        ahead = 0.0
    return FreshnessPolicy(
        ttl_seconds=ttl,
        max_stale_seconds=max_stale,
        refresh_mode=refresh_mode,
        refresh_ahead_seconds=min(ahead, ttl) if ttl > 0 else 0.0,
    )


def _policy_resolver(settings: SnapshotProviderSettings) -> PolicyResolver:
    """Freshness per resource family; operational TTLs never undercut execution freshness."""

    operational_ttl_floor = max(settings.execution_max_age_seconds, settings.exit_max_age_seconds)
    market_ttl_seconds = max(
        settings.light_ttl_seconds if settings.light_ttl_seconds > 0 else 1.0,
        settings.execution_max_age_seconds,
    )
    heavy_ttl_seconds = max(settings.heavy_ttl_seconds, operational_ttl_floor)
    account_ttl_seconds = max(settings.account_ttl_seconds, operational_ttl_floor)
    market_policy = freshness(
        market_ttl_seconds,
        max(market_ttl_seconds, settings.max_data_age_seconds),
    )
    heavy_policy = freshness(
        heavy_ttl_seconds,
        max(heavy_ttl_seconds, settings.max_data_age_seconds),
    )
    exchange_policy = freshness(
        settings.exchange_info_ttl_seconds,
        max(settings.exchange_info_ttl_seconds, 300.0),
        refresh_mode=RefreshMode.REFRESH_AHEAD,
        refresh_ahead_seconds=min(30.0, settings.exchange_info_ttl_seconds),
    )
    account_policy = freshness(
        account_ttl_seconds,
        max(account_ttl_seconds, settings.max_data_age_seconds),
    )

    def dynamic(key: ResourceKey) -> FreshnessPolicy:
        if is_key(key, "market", "state"):
            return market_policy
        if key == EXCHANGE_INFO or is_key(key, "exchange", "rules"):
            return exchange_policy
        if key == ACCOUNT:
            return account_policy
        return heavy_policy

    return PolicyResolver(default=heavy_policy, dynamic=dynamic)


def _rest_policy(*, scope: CircuitScope, failure_threshold: int, recovery: float) -> Any:
    return SourceResiliencePolicy(
        retry=EngineRetryPolicy(
            max_attempts=1,
            base_delay_seconds=0.0,
            max_delay_seconds=0.0,
            jitter_ratio=0.0,
        ),
        circuit=CircuitBreakerPolicy(
            scope=scope,
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery,
        ),
    )


def _sources(settings: SnapshotProviderSettings, fetchers: SourceFetchers) -> list[Any]:
    local_policy = SourceResiliencePolicy(
        retry=EngineRetryPolicy(max_attempts=1),
        circuit=CircuitBreakerPolicy(
            scope=CircuitScope.SUBJECT,
            failure_threshold=2,
            recovery_timeout_seconds=2.0,
        ),
    )
    rest_policy = _rest_policy(scope=CircuitScope.NAMESPACE, failure_threshold=3, recovery=5.0)
    # Bootstrap publishes exchangeInfo before the operational cycle. The REST
    # exchange source remains as a short-budget fallback with an isolated circuit.
    exchange_rest_policy = _rest_policy(
        scope=CircuitScope.SOURCE,
        failure_threshold=settings.exchange_info_bootstrap_attempts + 2,
        recovery=30.0,
    )
    return [
        CallableBatchSource(
            name=SOURCE_USER_DATA_CACHE,
            priority=130,
            supports=fetchers.user_data_cache_supports,
            fetcher=fetchers.user_data_cache_batch,
            cache_supports=False,
            timeout_seconds=settings.market_stream_timeout_seconds,
            max_batch_size=100,
            resilience_policy=local_policy,
            run_sync_in_thread=False,
        ),
        CallableDerivedSource(
            name=SOURCE_POSITION_DERIVED,
            priority=120,
            supports=lambda key: is_key(key, "account", "position"),
            dependencies=lambda _key: (ALL_POSITIONS,),
            deriver=fetchers.derive_position,
            max_concurrency=settings.derived_concurrency,
            run_sync_in_thread=False,
        ),
        CallableDerivedSource(
            name=SOURCE_RULES_DERIVED,
            priority=110,
            supports=lambda key: is_key(key, "exchange", "rules"),
            dependencies=lambda _key: (EXCHANGE_INFO,),
            deriver=fetchers.derive_exchange_rules,
            max_concurrency=settings.derived_concurrency,
            run_sync_in_thread=False,
        ),
        CallableBatchSource(
            name=SOURCE_MARKET_STREAM,
            priority=100,
            supports=lambda key: is_key(key, "market", "state"),
            fetcher=fetchers.market_stream_batch,
            timeout_seconds=settings.market_stream_timeout_seconds,
            max_batch_size=100,
            resilience_policy=local_policy,
            run_sync_in_thread=False,
        ),
        CallableSource(
            name=SOURCE_REST_MARKET,
            priority=40,
            supports=lambda key: (
                is_key(key, "market", "state") or is_key(key, "market", "mark-price")
            ),
            fetcher=fetchers.rest_market,
            timeout_seconds=settings.rest_timeout_seconds,
            resilience_policy=rest_policy,
            blocking_io=True,
            transport_timeout_seconds=settings.rest_transport_timeout_seconds,
        ),
        CallableSource(
            name=SOURCE_REST_PRIVATE,
            priority=30,
            supports=lambda key: (
                key in {ACCOUNT, ALL_POSITIONS}
                or is_key(key, "account", "position")
                or is_key(key, "orders", "open")
                or is_key(key, "orders", "open-algo")
            ),
            fetcher=fetchers.rest_private,
            timeout_seconds=settings.rest_timeout_seconds,
            resilience_policy=rest_policy,
            blocking_io=True,
            transport_timeout_seconds=settings.rest_transport_timeout_seconds,
        ),
        CallableSource(
            name=SOURCE_REST_EXCHANGE,
            priority=20,
            supports=lambda key: key == EXCHANGE_INFO,
            fetcher=fetchers.rest_exchange,
            timeout_seconds=settings.exchange_info_timeout_seconds,
            resilience_policy=exchange_rest_policy,
            blocking_io=True,
            transport_timeout_seconds=settings.exchange_info_transport_timeout_seconds,
        ),
    ]


def build_snapshot_builder(
    settings: SnapshotProviderSettings,
    fetchers: SourceFetchers,
    *,
    metrics: Any,
    events: Any,
) -> SnapshotBuilder:
    source_concurrency = {
        SOURCE_USER_DATA_CACHE: settings.user_data_concurrency,
        SOURCE_POSITION_DERIVED: settings.derived_concurrency,
        SOURCE_MARKET_STREAM: settings.stream_concurrency,
        SOURCE_REST_MARKET: settings.rest_concurrency,
        SOURCE_REST_PRIVATE: settings.rest_concurrency,
        SOURCE_REST_EXCHANGE: settings.rest_concurrency,
        SOURCE_RULES_DERIVED: settings.derived_concurrency,
        **dict(settings.source_concurrency),
    }
    return SnapshotBuilder(
        sources=_sources(settings, fetchers),
        policy_resolver=_policy_resolver(settings),
        authority_resolver=build_authority_resolver(),
        source_concurrency=source_concurrency,
        max_concurrency=settings.max_concurrency,
        max_pending_tasks=settings.max_pending_tasks,
        observation_policy=ObservationPolicy(),
        metrics=metrics,
        events=events,
        manage_lifecycle=True,
        copy_shutdown_timeout_seconds=settings.copy_shutdown_timeout_seconds,
        buffer_observability=True,
        observability_max_pending=settings.observability_max_pending,
        observability_overflow=BufferOverflowPolicy(settings.observability_overflow),
        observability_shutdown_timeout_seconds=settings.observability_shutdown_timeout_seconds,
        observability_drain_on_shutdown=settings.observability_drain_on_shutdown,
        source_transport_timeout_grace_seconds=settings.source_transport_timeout_grace_seconds,
        source_deadline_dispatch_grace_seconds=settings.source_deadline_dispatch_grace_seconds,
    )


__all__ = ["build_snapshot_builder", "freshness"]
