"""The Governor's operational snapshot provider, built on the embedded snapshot engine.

The provider owns one engine builder per process and composes the pieces around it: the
sources (``fetchers``, ``transport``), the policies of each usage profile (``policies``),
readiness classification (``readiness``), async user-data submissions (``submissions``) and
builder health (``health``). The Governor opens one :class:`SnapshotCycleSession` per cycle.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from typing import Any

from binance.futures_client import BinanceFuturesClient
from domain.models import FuturesPosition
from market_data.market_snapshots import (
    HeavyMarketSnapshot,
    LightMarketSnapshot,
    empty_position,
    normalize_symbol,
)
from market_data.price_window import MarketPriceWindow
from market_data.snapshot_engine import (
    BuilderHealth,
    ResourceKey,
    ResourceUpdate,
    Snapshot,
    SnapshotBuildError,
    SnapshotRequest,
    SnapshotResult,
    SyncResourcePublisher,
    SyncSnapshotBuilder,
    SyncSnapshotSession,
    require_capabilities,
)
from market_data.snapshot_provider import wire_names
from market_data.snapshot_provider.builder import build_snapshot_builder
from market_data.snapshot_provider.diagnostics import snapshot_error_diagnostics_payload
from market_data.snapshot_provider.event_sink import SnapshotEventSink
from market_data.snapshot_provider.exchange_info import bootstrap_exchange_info
from market_data.snapshot_provider.fetchers import SourceFetchers
from market_data.snapshot_provider.health import COMPONENT, HealthMonitor
from market_data.snapshot_provider.keys import (
    ACCOUNT,
    ALL_POSITIONS,
    EXCHANGE_INFO,
    exchange_rules,
    mark_price,
    market_state,
    normalized_symbols,
    open_algo_orders,
    open_orders,
    position,
    symbol_evaluation_keys,
)
from market_data.snapshot_provider.metrics_sink import SnapshotMetricsSink
from market_data.snapshot_provider.observed_at import (
    CONTEXT_NOW_KEY,
    coerce_now,
)
from market_data.snapshot_provider.policies import SnapshotPolicies
from market_data.snapshot_provider.readiness import (
    ReadinessDiagnostics,
    SnapshotReadinessDiagnostic,
)
from market_data.snapshot_provider.reconciliation import reconcile_pending_submissions
from market_data.snapshot_provider.session import SnapshotCycleSession
from market_data.snapshot_provider.settings import (
    ExchangeInfoBootstrapResult,
    SnapshotProviderSettings,
    SubmissionReconciliationResult,
)
from market_data.snapshot_provider.sources import SOURCE_LOCAL_RECONCILED
from market_data.snapshot_provider.submissions import SubmissionCoordinator
from market_data.snapshot_provider.transport import RestTransport
from market_data.snapshot_provider.user_data import user_data_writes
from market_data.snapshot_provider.views import (
    heavy_snapshot_from_snapshot,
    light_snapshot_from_snapshot,
    missing_heavy_snapshot_keys,
)
from utils.events import EventChannel, observe_error, observe_event

# What the provider relies on from the embedded engine; checked when it starts.
REQUIRED_FEATURES: tuple[str, ...] = (
    "automatic_observability_buffering",
    "blocking_source_timeout_guarantees",
    "builder_health_assessment",
    "builder_health_serialization",
    "controlled_payload_copy_shutdown",
    "degraded_snapshot_results",
    "partitioned_request_builds",
    "snapshot_acceptance",
    "snapshot_consistency",
    "transactional_revalidation",
    "versioned_error_diagnostics",
)
REQUIRED_SCHEMAS: Mapping[str, int] = {
    "builder_health": 1,
    "builder_health_assessment": 1,
    "capabilities": 1,
    "error_diagnostics": 1,
}

_LIVE_PROVIDERS_FOR_TESTING: weakref.WeakSet[SnapshotProvider] = weakref.WeakSet()


class SnapshotProvider:
    def __init__(
        self,
        *,
        client: BinanceFuturesClient,
        market_data_hub: Any = None,
        candle_data_hub: Any = None,
        user_data_cache: Any = None,
        settings: SnapshotProviderSettings | None = None,
        wall_clock: Callable[[], float] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        require_capabilities(features=list(REQUIRED_FEATURES), schemas=dict(REQUIRED_SCHEMAS))
        self.client = client
        self.market_data_hub = market_data_hub
        self.candle_data_hub = candle_data_hub
        self.user_data_cache = user_data_cache
        self.settings = settings or SnapshotProviderSettings()
        self._wall_clock = wall_clock or time.time
        self.sleep: Callable[[float], None] = sleeper or time.sleep
        shared_window = getattr(market_data_hub, "price_window_store", None)
        self.price_window: MarketPriceWindow = shared_window or MarketPriceWindow(
            maxlen=self.settings.price_window
        )
        self.last_snapshot: Snapshot | None = None
        self.last_snapshot_result: SnapshotResult | None = None
        self.transport = RestTransport(
            client=client,
            settings=self.settings,
            monotonic=monotonic_clock or time.monotonic,
            sleep=self.sleep,
        )
        self.fetchers = SourceFetchers(
            client=client,
            transport=self.transport,
            settings=self.settings,
            market_data_hub=market_data_hub,
            candle_data_hub=candle_data_hub,
            user_data_cache=user_data_cache,
            price_window=self.price_window,
            wall_clock=self._wall_clock,
        )
        self.policies = SnapshotPolicies(self.settings)
        self.readiness = ReadinessDiagnostics(self.policies)
        self.metrics_sink = SnapshotMetricsSink()
        self.event_sink = SnapshotEventSink()
        self._sync_builder = SyncSnapshotBuilder(
            build_snapshot_builder(
                self.settings,
                self.fetchers,
                metrics=self.metrics_sink,
                events=self.event_sink,
            ),
            shutdown_timeout_seconds=self.settings.sync_builder_shutdown_timeout_seconds,
            max_pending_submissions=self.settings.max_pending_submissions,
        )
        self.submissions = SubmissionCoordinator(self.publisher, component=COMPONENT)
        self.health = HealthMonitor(self.health_snapshot)
        self._close_lock = threading.Lock()
        self._closing = False
        self._closed = False
        _LIVE_PROVIDERS_FOR_TESTING.add(self)

    # --- lifecycle ------------------------------------------------------------------------------

    @property
    def publisher(self) -> SyncResourcePublisher:
        return self._sync_builder.publisher

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closing = True

        self._flush_submissions()
        self.collect_health("pre_shutdown")
        close_error: BaseException | None = None
        try:
            self._sync_builder.close()
        except BaseException as error:
            close_error = error
            observe_error(
                "snapshot_provider_close_failed",
                error=error,
                component=COMPONENT,
                category="market_data",
                error_type=type(error).__name__,
                message=str(error),
            )
        finally:
            with self._close_lock:
                self._closed = True
                self._closing = False
            _LIVE_PROVIDERS_FOR_TESTING.discard(self)

        if close_error is not None:
            raise close_error

    def _flush_submissions(self) -> None:
        timeout_seconds = self.settings.submission_flush_timeout_seconds
        try:
            summary = self.submissions.flush(timeout_seconds=timeout_seconds)
        except Exception as flush_error:
            self.submissions.mark_pending_for_reconciliation()
            observe_error(
                "submission_flush_failed",
                error=flush_error,
                component=COMPONENT,
                category="market_data",
                error_type=type(flush_error).__name__,
                message=str(flush_error),
            )
            return
        observe_event(
            wire_names.SUBMISSION_FLUSH,
            component=COMPONENT,
            category="market_data",
            channel=EventChannel.AUDIT,
            **asdict(summary),
            timeout_seconds=timeout_seconds,
        )
        if summary.timed_out or summary.pending:
            self.submissions.mark_pending_for_reconciliation()
            self.submissions.cancel_pending()

    def health_snapshot(self) -> BuilderHealth:
        return self._sync_builder.health_snapshot()

    def collect_health(self, label: str) -> dict[str, Any]:
        """Sample, assess and record builder health; see :class:`HealthMonitor`."""
        return self.health.collect(label)

    def _accepts_submissions(self) -> bool:
        with self._close_lock:
            return not self._closing and not self._closed

    # --- cycle sessions -------------------------------------------------------------------------

    def open_cycle_session(
        self,
        *,
        cycle_id: str,
        now: float,
        symbols: Iterable[str],
        local_positions: Mapping[str, FuturesPosition] | None = None,
    ) -> SnapshotCycleSession:
        self.reconcile_pending_submissions(reason="cycle_start")
        symbols = normalized_symbols(symbols)
        self._prepare_cycle_position_views(
            symbols=symbols,
            local_positions=local_positions,
            observed_at=now,
        )
        session = self.open_session(
            snapshot_id=cycle_id,
            now=now,
            cycle_id=cycle_id,
            deadline_seconds=self.settings.cycle_deadline_seconds,
        )
        return SnapshotCycleSession(
            provider=self,
            session=session,
            cycle_id=cycle_id,
            now=now,
            symbols=symbols,
        )

    def open_session(
        self,
        *,
        snapshot_id: str,
        now: float,
        cycle_id: str,
        deadline_seconds: float | None = None,
    ) -> SyncSnapshotSession:
        """A cumulative engine session with its own deadline (``deadline_seconds`` by default)."""
        return self._sync_builder.session(
            deadline_seconds=(
                self.settings.deadline_seconds if deadline_seconds is None else deadline_seconds
            ),
            snapshot_id=snapshot_id,
            metadata={CONTEXT_NOW_KEY: float(now), "cycle_id": str(cycle_id)},
        )

    def _prepare_cycle_position_views(
        self,
        *,
        symbols: Iterable[str],
        local_positions: Mapping[str, FuturesPosition] | None,
        observed_at: float,
    ) -> None:
        normalized = normalized_symbols(symbols)
        self.publisher.invalidate_many(
            [ALL_POSITIONS, *(position(symbol) for symbol in normalized)],
            reason="alphora-cycle-position-baseline",
        )
        if local_positions is None:
            return

        values = {normalize_symbol(symbol): value for symbol, value in local_positions.items()}
        # ALL_POSITIONS is intentionally NOT published here. Publishing it via
        # SOURCE_LOCAL_RECONCILED (authority=300) would write it to the builder cache with
        # high authority, blocking subsequent REST refreshes from overwriting it
        # (SOURCE_REST_PRIVATE has authority=30). Even force_refresh=True bypasses cache
        # READ but not the authority check on WRITE — so the REST value would be rejected
        # and the stale LOCAL baseline would persist, causing a consistency skew between
        # position(symbol) (derived from the stale ALL_POSITIONS) and open_algo_orders
        # (fetched fresh via REST). Keeping ALL_POSITIONS invalidated-only ensures the
        # builder cache has no high-authority entry, so REST can write it freely during
        # pre-execution revalidation.
        updates = [
            ResourceUpdate(
                position(symbol),
                values.get(symbol, empty_position(symbol)),
                source=SOURCE_LOCAL_RECONCILED,
                observed_at=observed_at,
            )
            for symbol in normalized
        ]
        self.publisher.publish_many(updates, force=True, replace_equal=True)

    # --- async user-data submissions ------------------------------------------------------------

    def reconcile_pending_submissions(
        self,
        *,
        session: SnapshotCycleSession | None = None,
        keys: Iterable[ResourceKey] | None = None,
        reason: str = "manual",
    ) -> SubmissionReconciliationResult:
        """Invalidate or revalidate keys whose async submission did not land (``reconciliation``)."""
        return reconcile_pending_submissions(self, session=session, keys=keys, reason=reason)

    def publish_user_data_cache_update(
        self,
        *,
        cache: Any,
        update: Any,
        event: Mapping[str, Any],
    ) -> None:
        """Publish normalized User Data Stream state into the snapshot cache (see ``user_data``)."""

        if not self._accepts_submissions():
            return
        writes = user_data_writes(cache=cache, update=update, event=event)
        result = self.submissions.submit(
            writes.updates,
            writes.invalidations,
            reason="alphora-user-data-stream-update",
        )
        if result.backlog_full:
            self.collect_health("submission_backlog_full")

    # --- one-off snapshots ----------------------------------------------------------------------

    def get_light_snapshot(
        self,
        symbol: str,
        *,
        now: float | None = None,
        operational_snapshot: Snapshot | None = None,
    ) -> LightMarketSnapshot:
        current_time = coerce_now(now)
        if operational_snapshot is not None:
            return self.light_snapshot_from_snapshot(
                operational_snapshot,
                symbol,
                now=current_time,
            )

        key = market_state(symbol)
        self._invalidate_market_when_ttl_is_zero(key)
        snapshot = self._build_request(
            SnapshotRequest(required=[key]),
            now=current_time,
            snapshot_id=f"light:{key.subject}",
        )
        return self.light_snapshot_from_snapshot(snapshot, symbol, now=current_time)

    def get_heavy_snapshot(
        self,
        symbol: str,
        *,
        now: float | None = None,
        cached_position: FuturesPosition | None = None,
        cached_account_info: dict[str, Any] | None = None,
        cached_open_orders: list[dict[str, Any]] | None = None,
        operational_snapshot: Snapshot | None = None,
    ) -> HeavyMarketSnapshot:
        current_time = coerce_now(now)
        normalized = normalize_symbol(symbol)
        if operational_snapshot is None:
            required = [
                market_state(normalized),
                mark_price(normalized),
                exchange_rules(normalized),
                open_algo_orders(normalized),
            ]
            self._invalidate_market_when_ttl_is_zero(market_state(normalized))
            if cached_position is None:
                required.append(position(normalized))
            if cached_open_orders is None:
                required.append(open_orders(normalized))
            if cached_account_info is None:
                required.append(ACCOUNT)
            operational_snapshot = self._build_request(
                SnapshotRequest(required=required),
                now=current_time,
                snapshot_id=f"heavy:{normalized}",
                deadline_seconds=self.settings.cycle_deadline_seconds,
            )
        return self.heavy_snapshot_from_snapshot(
            operational_snapshot,
            normalized,
            now=current_time,
            cached_position=cached_position,
            cached_account_info=cached_account_info,
            cached_open_orders=cached_open_orders,
        )

    def light_snapshot_from_snapshot(
        self,
        snapshot: Snapshot,
        symbol: str,
        *,
        now: float,
    ) -> LightMarketSnapshot:
        return light_snapshot_from_snapshot(
            snapshot,
            symbol,
            now=now,
            max_data_age_seconds=self.settings.max_data_age_seconds,
        )

    def heavy_snapshot_from_snapshot(
        self,
        snapshot: Snapshot,
        symbol: str,
        *,
        now: float,
        cached_position: FuturesPosition | None = None,
        cached_account_info: dict[str, Any] | None = None,
        cached_open_orders: list[dict[str, Any]] | None = None,
    ) -> HeavyMarketSnapshot:
        return heavy_snapshot_from_snapshot(
            snapshot,
            symbol,
            now=now,
            max_data_age_seconds=self.settings.max_data_age_seconds,
            cached_position=cached_position,
            cached_account_info=cached_account_info,
            cached_open_orders=cached_open_orders,
        )

    def missing_heavy_snapshot_keys(
        self,
        snapshot: Snapshot,
        symbol: str,
    ) -> tuple[ResourceKey, ...]:
        """The keys a heavy snapshot of ``symbol`` needs that ``snapshot`` does not hold."""
        return missing_heavy_snapshot_keys(snapshot, symbol)

    def get_account_info(
        self,
        *,
        now: float | None = None,
        operational_snapshot: Snapshot | None = None,
    ) -> dict[str, Any]:
        if operational_snapshot is not None:
            return dict(operational_snapshot.value(ACCOUNT, dict))
        snapshot = self._build_request(
            SnapshotRequest(required=[ACCOUNT]),
            now=coerce_now(now),
            snapshot_id="account:summary",
        )
        return dict(snapshot.value(ACCOUNT, dict))

    def get_exchange_info(
        self,
        *,
        now: float | None = None,
        operational_snapshot: Snapshot | None = None,
    ) -> dict[str, Any]:
        if operational_snapshot is not None and EXCHANGE_INFO in operational_snapshot:
            return dict(operational_snapshot.value(EXCHANGE_INFO, dict))
        snapshot = self._build_request(
            SnapshotRequest(required=[EXCHANGE_INFO]),
            now=coerce_now(now),
            snapshot_id="exchange:info",
            deadline_seconds=self.settings.cycle_deadline_seconds,
        )
        return dict(snapshot.value(EXCHANGE_INFO, dict))

    def bootstrap_exchange_info(
        self,
        *,
        now: float | None = None,
    ) -> ExchangeInfoBootstrapResult:
        """Fetch a guaranteed-fresh exchangeInfo before the first cycle (``exchange_info``)."""
        return bootstrap_exchange_info(self, now=now)

    def _build_request(
        self,
        request: SnapshotRequest,
        *,
        now: float,
        snapshot_id: str,
        deadline_seconds: float | None = None,
    ) -> Snapshot:
        effective_deadline = (
            self.settings.deadline_seconds if deadline_seconds is None else float(deadline_seconds)
        )

        try:
            snapshot = self._sync_builder.build_request(
                request,
                deadline_seconds=effective_deadline,
                snapshot_id=snapshot_id,
                metadata={CONTEXT_NOW_KEY: now},
            )
        except SnapshotBuildError as error:
            if error.snapshot is not None:
                self.last_snapshot = error.snapshot
            observe_error(
                "snapshot_build_failed",
                error=error,
                component=COMPONENT,
                category="market_data",
                resource=snapshot_id,
                source=wire_names.ERROR_SOURCE_SNAPSHOT_BUILDER,
                error_type=type(error).__name__,
                message=str(error),
                partial_snapshot_available=bool(error.snapshot is not None),
                diagnostics=snapshot_error_diagnostics_payload(error),
            )
            self.collect_health("snapshot_failed")
            raise

        self.last_snapshot = snapshot
        return snapshot

    def _invalidate_market_when_ttl_is_zero(self, key: ResourceKey) -> None:
        if self.settings.light_ttl_seconds > 0:
            return
        self.publisher.invalidate(key, reason="alphora-light-ttl-zero")

    # --- partitioned and risk-reduction builds --------------------------------------------------

    def try_prefetch_symbol_evaluation_snapshots(
        self,
        *,
        symbols: Iterable[str],
        cycle_id: str,
        now: float,
    ) -> Mapping[str, SnapshotResult]:
        symbols = normalized_symbols(symbols)
        requests = [
            SnapshotRequest(
                required=symbol_evaluation_keys(symbol),
                acceptance_policy=self.policies.evaluation_acceptance(
                    [symbol],
                    include_optional_resources=False,
                ),
                consistency_policy=self.policies.evaluation_consistency(),
            )
            for symbol in symbols
        ]

        deadline = (
            self.settings.partitioned_symbol_deadline_seconds or self.settings.deadline_seconds
        )

        results = self._sync_builder.try_build_requests(
            requests,
            deadline_seconds=deadline,
            metadata={CONTEXT_NOW_KEY: float(now), "cycle_id": str(cycle_id)},
            snapshot_id_prefix=f"prefetch:{cycle_id}",
            degradation_policy=self.policies.evaluation_degradation(),
            max_concurrent=self.settings.partitioned_symbol_request_concurrency,
        )

        return dict(zip(symbols, results, strict=True))

    def try_risk_reduction_snapshot(
        self,
        symbol: str,
        *,
        cycle_id: str,
        now: float,
    ) -> SnapshotResult:
        normalized = normalize_symbol(symbol)
        request = SnapshotRequest(
            required=[
                position(normalized),
                open_orders(normalized),
                open_algo_orders(normalized),
                ACCOUNT,
            ],
            acceptance_policy=self.policies.exit_acceptance(normalized),
            consistency_policy=self.policies.exit_consistency(),
        )
        return self._sync_builder.try_build_request(
            request,
            deadline_seconds=self.settings.deadline_seconds,
            snapshot_id=f"exit-risk-reduction:{normalized}:{cycle_id}",
            metadata={CONTEXT_NOW_KEY: float(now), "cycle_id": str(cycle_id)},
            degradation_policy=self.policies.risk_reduction_degradation(),
        )

    # --- readiness ------------------------------------------------------------------------------

    def readiness_diagnostic_from_result(
        self,
        result: SnapshotResult,
        *,
        profile: str,
        symbol: str | None = None,
        symbols: Iterable[str] = (),
    ) -> SnapshotReadinessDiagnostic:
        return self.readiness.from_result(result, profile=profile, symbol=symbol, symbols=symbols)

    def readiness_diagnostic(
        self,
        snapshot: Snapshot | None,
        *,
        profile: str,
        symbol: str | None = None,
        symbols: Iterable[str] = (),
        error: BaseException | None = None,
    ) -> SnapshotReadinessDiagnostic:
        return self.readiness.from_snapshot(
            snapshot,
            profile=profile,
            symbol=symbol,
            symbols=symbols,
            error=error,
        )


def close_live_snapshot_providers_for_testing() -> None:
    """Close snapshot providers left alive by tests."""

    for provider in tuple(_LIVE_PROVIDERS_FOR_TESTING):
        try:
            provider.close()
        except Exception:
            pass


__all__ = [
    "REQUIRED_FEATURES",
    "REQUIRED_SCHEMAS",
    "SnapshotProvider",
    "close_live_snapshot_providers_for_testing",
]
