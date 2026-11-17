"""Phase B of a cycle: read the private (heavy) snapshots of the selected symbols in parallel."""

from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from governor.cycle_context import CycleContext
from governor.evaluation.outcomes import EventJournal
from governor.governor_observability import elapsed_ms, monotonic_now
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext
from market_data.snapshot_provider import wire_names
from utils.events import EventChannel, observe_event


class HeavyPrefetcher:
    """Prefetch heavy snapshots so phase C only re-reads what an order invalidated.

    With the snapshot provider the reads are one partitioned request; without it a
    small thread pool calls the market data router. A failed symbol is recorded in
    ``context.prefetched_heavy_errors`` and later falls back to its own heavy read.
    """

    def __init__(
        self,
        *,
        config: Any,
        provider: Any,
        router: Any,
        journal: EventJournal,
    ) -> None:
        self.config = config
        self.provider = provider
        self.router = router
        self.journal = journal

    def enabled(self) -> bool:
        return bool(getattr(self.config, "governor_parallel_heavy_prefetch_enabled", True))

    def max_concurrent_snapshots(self) -> int:
        return max(
            1,
            int(getattr(self.config, "governor_max_concurrent_symbol_snapshots", 3) or 1),
        )

    def shared_private_cycle_ttl_seconds(self) -> float:
        return max(
            0.0,
            float(getattr(self.config, "governor_shared_private_cycle_ttl_seconds", 2.0) or 0.0),
        )

    def prefetch(
        self,
        context: CycleContext,
        evaluations: Sequence[SymbolEvaluationContext],
    ) -> None:
        if not self.enabled() or len(evaluations) < 2:
            return

        candidates = [
            evaluation
            for evaluation in evaluations
            if evaluation.terminal_result is None and evaluation.score is not None
        ]
        if len(candidates) < 2:
            return

        started_at = monotonic_now()
        self.journal.append(
            "parallel_heavy_prefetch_started",
            cycle_id=context.cycle_id,
            symbols=[_symbol(evaluation) for evaluation in candidates],
            private_state_generation=context.private_state_generation,
        )

        try_prefetch = getattr(self.provider, "try_prefetch_symbol_evaluation_snapshots", None)
        if callable(try_prefetch):
            self._prefetch_partitioned(context, candidates, try_prefetch)
            mode = wire_names.PARTITIONED_PREFETCH_MODE
        else:
            self._prefetch_thread_pool(context, candidates)
            mode = "thread_pool"

        self.journal.append(
            "parallel_heavy_prefetch_finished",
            cycle_id=context.cycle_id,
            duration_ms=elapsed_ms(started_at),
            completed_symbols=sorted(context.prefetched_heavy_snapshots),
            failed_symbols=sorted(context.prefetched_heavy_errors),
            mode=mode,
        )

    def _prefetch_partitioned(
        self,
        context: CycleContext,
        candidates: list[SymbolEvaluationContext],
        try_prefetch: Any,
    ) -> None:
        provider = self.provider
        try:
            results = try_prefetch(
                symbols=[_symbol(evaluation) for evaluation in candidates],
                cycle_id=context.cycle_id,
                now=time.time(),
            )
            for evaluation in candidates:
                symbol = _symbol(evaluation)
                result = results.get(str(symbol or "").upper().strip())
                if result is None:
                    continue

                observe_event(
                    wire_names.PARALLEL_PREFETCH_RESULT,
                    component=__name__,
                    category="governor",
                    channel=EventChannel.AUDIT,
                    cycle_id=context.cycle_id,
                    symbol=symbol,
                    state=result.state.value,
                    reason=result.reason,
                    allowed_actions=list(result.allowed_actions),
                    diagnostics=result.to_dict(),
                )

                if not result.accepted:
                    context.prefetched_heavy_errors[str(symbol).upper()] = result.reason
                    continue

                missing = provider.missing_heavy_snapshot_keys(result.snapshot, symbol)
                if missing:
                    context.prefetched_heavy_errors[str(symbol).upper()] = (
                        f"missing_keys:{','.join(str(k) for k in missing)}"
                    )
                    continue

                captured_at = time.time()
                snapshot = provider.heavy_snapshot_from_snapshot(
                    result.snapshot,
                    symbol,
                    now=captured_at,
                )
                self._store(context, symbol, snapshot, captured_at)
        except Exception as error:
            observe_event(
                wire_names.PARALLEL_PREFETCH_FAILED,
                component=__name__,
                category="governor",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                cycle_id=context.cycle_id,
                reason=str(error),
                error_type=type(error).__name__,
            )

    def _prefetch_thread_pool(
        self,
        context: CycleContext,
        candidates: list[SymbolEvaluationContext],
    ) -> None:
        max_workers = min(self.max_concurrent_snapshots(), len(candidates))
        self.journal.append(
            "parallel_heavy_prefetch_mode",
            cycle_id=context.cycle_id,
            mode="thread_pool",
            max_workers=max_workers,
        )

        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="alphora-heavy-prefetch",
        ) as executor:
            futures = {
                executor.submit(self.prefetch_one, context, evaluation): evaluation
                for evaluation in candidates
            }
            for future in as_completed(futures):
                symbol = _symbol(futures[future])
                try:
                    snapshot, captured_at = future.result()
                except Exception as error:
                    normalized_err_symbol = str(symbol or "").upper().strip() or symbol
                    context.prefetched_heavy_errors[normalized_err_symbol] = (
                        f"{type(error).__name__}: {error}"
                    )
                    observe_event(
                        "parallel_heavy_prefetch_failed",
                        component=__name__,
                        category="governor",
                        channel=EventChannel.AUDIT,
                        severity="WARNING",
                        cycle_id=context.cycle_id,
                        symbol=symbol,
                        reason=str(error),
                        error_type=type(error).__name__,
                    )
                    continue

                self._store(context, symbol, snapshot, captured_at)

    def _store(self, context: CycleContext, symbol: str, snapshot: Any, captured_at: float) -> None:
        context.store_prefetched_heavy_snapshot(symbol, snapshot=snapshot, captured_at=captured_at)
        observe_event(
            "parallel_heavy_prefetch_completed",
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            cycle_id=context.cycle_id,
            symbol=symbol,
            captured_at=captured_at,
            data_age_seconds=getattr(snapshot, "data_age_seconds", 0.0),
            private_state_generation=context.private_state_generation,
        )

    def prefetch_one(
        self,
        context: CycleContext,
        evaluation: SymbolEvaluationContext,
    ) -> tuple[Any, float]:
        """Read one symbol's heavy snapshot, reusing the cycle's fresh private state."""

        symbol = _symbol(evaluation)
        captured_at = time.time()
        shared_state_fresh = context.shared_private_state_is_fresh(
            now=captured_at,
            ttl_seconds=self.shared_private_cycle_ttl_seconds(),
        )
        cached_position = context.positions.get(symbol) if shared_state_fresh else None
        cached_account_info = context.account_info if shared_state_fresh else None

        prefetch = getattr(self.router, "prefetch_heavy_snapshot", None)
        if callable(prefetch):
            snapshot = prefetch(
                symbol,
                now=captured_at,
                cached_position=cached_position,
                cached_account_info=cached_account_info,
            )
        else:
            snapshot = self.router.get_heavy_snapshot(
                symbol,
                now=captured_at,
                cached_position=cached_position,
                cached_account_info=cached_account_info,
            )
        return snapshot, captured_at


def _symbol(evaluation: SymbolEvaluationContext) -> str:
    return evaluation.evaluation_input.symbol_config.symbol
