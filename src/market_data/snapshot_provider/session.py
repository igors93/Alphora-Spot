"""One cumulative snapshot session for an entire Governor cycle."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from market_data.market_snapshots import normalize_symbol
from market_data.snapshot_engine import (
    ResourceKey,
    ResourceResolutionError,
    Snapshot,
    SnapshotBuildError,
    SnapshotRequest,
    SnapshotResult,
    SyncSnapshotSession,
)
from market_data.snapshot_provider import wire_names
from market_data.snapshot_provider.keys import (
    ACCOUNT,
    ALL_POSITIONS,
    exchange_rules,
    mark_price,
    market_state,
    normalized_symbols,
    open_algo_orders,
    open_orders,
    position,
    symbol_evaluation_keys,
)
from utils.events import EventChannel, observe_event

if TYPE_CHECKING:
    from market_data.snapshot_provider.provider import SnapshotProvider

logger = logging.getLogger("market_data.snapshot_provider")


class SnapshotCycleSession:
    """One cumulative snapshot session for an entire Governor cycle."""

    def __init__(
        self,
        *,
        provider: SnapshotProvider,
        session: SyncSnapshotSession,
        cycle_id: str,
        now: float,
        symbols: Iterable[str],
    ) -> None:
        self.provider = provider
        self.session = session
        self.cycle_id = str(cycle_id)
        self.now = float(now)
        self.symbols = normalized_symbols(symbols)
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self.session.closed

    def resolve_baseline(self) -> Snapshot:
        """Resolve account and all effective positions once for the cycle.

        Resources are optional at this orchestration layer so the existing Alphora fallback path can
        still run when a component is unavailable. Errors remain attached to the cumulative snapshot.
        """
        policies = self.provider.policies
        result = self.session.try_resolve_request(
            SnapshotRequest(
                optional=[
                    ACCOUNT,
                    *(position(symbol) for symbol in self.symbols),
                ],
                acceptance_policy=policies.baseline_acceptance(self.symbols),
            ),
            degradation_policy=policies.baseline_degradation(),
        )
        self._record(result)
        observe_event(
            wire_names.BASELINE_TRY_RESOLVED,
            component=__name__,
            category="market_data",
            channel=EventChannel.AUDIT,
            cycle_id=self.cycle_id,
            state=result.state.value,
            reason=result.reason,
            allowed_actions=list(result.allowed_actions),
            diagnostics=result.to_dict(),
        )
        if result.rejected:
            result.raise_for_rejected()
        return self._remember(result.snapshot)

    def resolve_selected(self, symbols: Iterable[str]) -> Snapshot:
        """Prefetch every read needed by the selected symbols before evaluation starts."""

        normalized = normalized_symbols(symbols)
        keys: list[ResourceKey] = [ACCOUNT]
        for symbol in normalized:
            keys.extend(
                [
                    market_state(symbol),
                    mark_price(symbol),
                    position(symbol),
                    open_orders(symbol),
                    open_algo_orders(symbol),
                    exchange_rules(symbol),
                ]
            )
        if len(keys) == 1:
            return self.snapshot()
        policies = self.provider.policies
        result = self.session.try_resolve_request(
            SnapshotRequest(
                optional=keys,
                acceptance_policy=policies.evaluation_acceptance(normalized),
                consistency_policy=policies.evaluation_consistency(),
            ),
            degradation_policy=policies.evaluation_degradation(),
        )
        self._record(result)
        observe_event(
            wire_names.SELECTED_TRY_RESOLVED,
            component=__name__,
            category="market_data",
            channel=EventChannel.AUDIT,
            cycle_id=self.cycle_id,
            symbols=list(normalized),
            state=result.state.value,
            reason=result.reason,
            allowed_actions=list(result.allowed_actions),
            diagnostics=result.to_dict(),
        )
        if result.rejected and not result.snapshot.resources:
            result.raise_for_rejected()
        return self._remember(result.snapshot)

    def resolve_light_selected(self, symbols: Iterable[str]) -> Snapshot:
        """Prefetch only light market data for the symbols selected in this cycle.

        The Governor does not know which symbols need an expensive heavy evaluation until
        after strategy scoring. Fetching account, orders, mark price, and exchange rules for
        every dirty symbol up front made multi-symbol cycles unnecessarily slow and allowed a
        snapshot captured at the beginning of the cycle to become stale before later symbols
        were evaluated.

        Missing light resources remain attached to the cumulative snapshot as optional errors;
        the router can then perform an isolated per-symbol fallback without one quiet symbol
        invalidating the whole selected-symbol batch.
        """

        normalized = normalized_symbols(symbols)
        keys = [market_state(symbol) for symbol in normalized]
        if not keys:
            return self.snapshot()
        result = self.session.try_resolve_request(
            SnapshotRequest(optional=keys),
            degradation_policy=self.provider.policies.evaluation_degradation(),
        )
        self._record(result)
        if result.rejected and not result.snapshot.resources:
            observe_event(
                wire_names.LIGHT_SELECTED_REJECTED,
                component=__name__,
                category="market_data",
                channel=EventChannel.AUDIT,
                cycle_id=self.cycle_id,
                symbols=list(normalized),
                state=result.state.value,
                reason=result.reason,
                diagnostics=result.to_dict(),
            )
            result.raise_for_rejected()
        return self._remember(result.snapshot)

    def resolve_symbol_evaluation(self, symbol: str) -> Snapshot:
        """Resolve heavy evaluation data just in time for one symbol.

        This isolates acceptance from unrelated symbols already present in the cumulative
        session and ensures later symbols do not reuse a heavy snapshot captured before an
        earlier symbol spent several seconds in live execution.
        """

        return self._remember(self.session.resolve_request(self._symbol_evaluation_request(symbol)))

    def try_resolve_symbol_evaluation(self, symbol: str) -> SnapshotResult:
        """Resolve heavy evaluation data for one symbol without throwing on partial availability."""

        result = self.session.try_resolve_request(
            self._symbol_evaluation_request(symbol),
            degradation_policy=self.provider.policies.evaluation_degradation(),
        )
        self._record(result)
        return result

    def revalidate_for_execution(self, symbol: str, *, exit_only: bool = False) -> Snapshot:
        """Revalidate critical execution resources for a symbol before sending an order.

        Opens a fresh independent session with its own full deadline budget so that earlier
        symbols in a multi-symbol cycle cannot exhaust the cycle session's time budget and
        cause SnapshotDeadlineExceededError for later symbols.

        Raises SnapshotBuildError or ResourceResolutionError on failure (fail-closed).
        """
        if self.closed:
            raise RuntimeError("SnapshotCycleSession is closed")
        provider = self.provider
        policies = provider.policies
        normalized = normalize_symbol(symbol)
        material_keys = (
            self.exit_keys(normalized) if exit_only else self.entry_consistency_keys(normalized)
        )
        dependency_keys = self.execution_dependency_keys(normalized)
        refresh_keys = tuple(dict.fromkeys((*dependency_keys, *material_keys)))
        force_refresh = (
            provider.settings.exit_force_refresh
            if exit_only
            else provider.settings.execution_force_refresh
        )
        # Use invalidation mode (session=None): pending keys are evicted from the shared cache
        # so the subsequent force_refresh resolve fetches them live. Avoids consuming any session
        # budget at this step, which matters when the cycle session is nearly spent.
        reconciliation = provider.reconcile_pending_submissions(
            session=None,
            keys=refresh_keys,
            reason="pre_execution_revalidation",
        )
        if not reconciliation.success:
            provider.collect_health("reconciliation_failed")
            raise RuntimeError(
                "pending snapshot submissions could not be reconciled before execution: "
                f"{reconciliation.reason}"
            )
        # _prepare_cycle_position_views publishes position(symbol) via SOURCE_LOCAL_RECONCILED
        # (authority=300) so the cycle evaluation snapshot has the governor's local position view.
        # During force_refresh revalidation, SOURCE_POSITION_DERIVED (authority=100) derives
        # fresh position from REST-fetched ALL_POSITIONS, but the write is rejected by the
        # higher-authority cache entry — position stays at cycle-start observed_at, causing
        # a skew vs ACCOUNT (which has no high-authority entry and refreshes freely).
        # Invalidating position here removes the LOCAL_RECONCILED entry so POSITION_DERIVED
        # can write the fresh value with its real observed_at.
        provider.publisher.invalidate(
            position(normalized),
            reason="alphora-pre-execution-position-invalidation",
        )
        # Fresh session with its own full deadline_seconds budget. force_refresh=True is used
        # throughout, so no accumulated cycle state is needed from self.session.
        exec_session = provider.open_session(
            snapshot_id=f"exec:{normalized}:{self.cycle_id}",
            now=self.now,
            cycle_id=self.cycle_id,
        )
        try:
            # Always resolve the full execution_keys set so the returned snapshot contains
            # market_state, mark_price, and exchange_rules — needed by heavy_snapshot_from_snapshot.
            # For exit_only, these extra keys are populated from cache (no force_refresh) and the
            # revalidate step below only force-refreshes the subset that actually needs to be fresh.
            base_resolve_keys = tuple(
                dict.fromkeys((*self.execution_keys(normalized), *dependency_keys, *material_keys))
            )
            exec_session.resolve(base_resolve_keys, strict=True)
            self._log_pre_revalidation_timestamps(
                normalized, exit_only=exit_only, session=exec_session
            )
            if dependency_keys:
                exec_session.revalidate(
                    dependency_keys,
                    strict=True,
                    force_refresh=force_refresh,
                )
            snapshot = exec_session.revalidate(
                material_keys,
                strict=True,
                force_refresh=force_refresh,
                acceptance_policy=(
                    policies.exit_acceptance(normalized)
                    if exit_only
                    else policies.entry_acceptance(normalized)
                ),
                consistency_policy=(
                    policies.exit_consistency() if exit_only else policies.entry_consistency()
                ),
            )
        except (SnapshotBuildError, ResourceResolutionError) as revalidate_error:
            partial = getattr(revalidate_error, "snapshot", None)
            if partial is not None:
                provider.last_snapshot = partial
            provider.collect_health("revalidation_failed")
            raise
        finally:
            exec_session.close()
        provider.collect_health("revalidation_succeeded")
        return self._remember(snapshot)

    def _log_pre_revalidation_timestamps(
        self,
        symbol: str,
        *,
        exit_only: bool,
        session: SyncSnapshotSession | None = None,
    ) -> None:
        """Log observed_at timestamps of critical resources before the material revalidate step.

        Helps diagnose authority/skew issues: if position or ALL_POSITIONS timestamps
        are old (near cycle_start) here, check whether ALL_POSITIONS is being published
        with SOURCE_LOCAL_RECONCILED somewhere (would block REST writes via authority).
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            snap = (session or self.session).snapshot()
            now = time.time()
            phase = "exit" if exit_only else "entry"
            diag: dict[str, str] = {}
            for key in (ALL_POSITIONS, position(symbol)):
                try:
                    item = snap[key]
                    age = now - item.observed_at
                    diag[str(key)] = f"age={age:.2f}s src={item.source}"
                except Exception:
                    diag[str(key)] = "missing"
            logger.debug(
                "Pre-revalidação timestamps | symbol=%s | phase=%s | %s",
                symbol,
                phase,
                " | ".join(f"{k}:{v}" for k, v in diag.items()),
            )
        except Exception:
            pass

    def execution_keys(self, symbol: str) -> tuple[ResourceKey, ...]:
        normalized = normalize_symbol(symbol)
        return (
            market_state(normalized),
            mark_price(normalized),
            exchange_rules(normalized),
            position(normalized),
            open_orders(normalized),
            open_algo_orders(normalized),
            ACCOUNT,
        )

    def entry_consistency_keys(self, symbol: str) -> tuple[ResourceKey, ...]:
        normalized = normalize_symbol(symbol)
        # ALL_POSITIONS must be in the material keys (force_refresh_keys) so that the
        # builder bypasses the cache READ and fetches it fresh from REST. The root cause
        # of the position/open_algo_orders skew was that _prepare_cycle_position_views
        # previously published ALL_POSITIONS via SOURCE_LOCAL_RECONCILED (authority=300).
        # This wrote a high-authority entry to the builder cache. force_refresh=True
        # bypasses cache READ but NOT the authority check on WRITE — so when REST_PRIVATE
        # (authority=30) tried to write a fresh value, it was rejected (IGNORED_LOWER_AUTHORITY)
        # and the stale LOCAL baseline was retained. Now _prepare_cycle_position_views only
        # invalidates ALL_POSITIONS (no publish), so the builder cache has no high-authority
        # entry and REST can write freely. The engine's mark_refreshed() ensures only one
        # REST fetch happens: SOURCE_POSITION_DERIVED resolves ALL_POSITIONS first (which
        # marks it refreshed), then REST_PRIVATE finds it in staging_runtime.memo and reuses it.
        return (
            market_state(normalized),
            mark_price(normalized),
            ALL_POSITIONS,
            position(normalized),
            open_orders(normalized),
            open_algo_orders(normalized),
            ACCOUNT,
        )

    def execution_dependency_keys(self, _symbol: str) -> tuple[ResourceKey, ...]:
        # ALL_POSITIONS is handled directly in entry_consistency_keys.
        return ()

    def exit_keys(self, symbol: str) -> tuple[ResourceKey, ...]:
        normalized = normalize_symbol(symbol)
        return (
            position(normalized),
            open_orders(normalized),
            open_algo_orders(normalized),
            ACCOUNT,
        )

    def snapshot(self) -> Snapshot:
        return self._remember(self.session.snapshot())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.session.close()

    def _symbol_evaluation_request(self, symbol: str) -> SnapshotRequest:
        normalized = normalize_symbol(symbol)
        policies = self.provider.policies
        return SnapshotRequest(
            required=symbol_evaluation_keys(normalized),
            acceptance_policy=policies.evaluation_acceptance(
                [normalized],
                include_optional_resources=False,
            ),
            consistency_policy=policies.evaluation_consistency(),
        )

    def _record(self, result: SnapshotResult) -> None:
        self.provider.last_snapshot_result = result
        self.provider.last_snapshot = result.snapshot

    def _remember(self, snapshot: Snapshot) -> Snapshot:
        self.provider.last_snapshot = snapshot
        return snapshot


__all__ = ["SnapshotCycleSession"]
