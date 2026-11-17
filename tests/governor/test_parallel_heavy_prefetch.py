import threading
import time
from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection
from domain.symbol_rules import SymbolRules
from governor.cycle.prefetch import HeavyPrefetcher
from governor.cycle.snapshots import CycleSnapshots
from governor.cycle_context import (
    CycleContext,
    SnapshotReadiness,
    SnapshotReadinessDiagnostic,
)
from governor.market_data_router import HeavyMarketSnapshot
from governor.symbol_evaluation_pipeline import (
    SymbolEvaluationContext,
    SymbolEvaluationInput,
)
from scanner.signal_score import SignalScore


class _Journal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **payload):
        self.events.append((event_type, payload))


class _ParallelRouter:
    def __init__(self, *, delay_seconds=0.08):
        self.delay_seconds = delay_seconds
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.invalidations = []

    def prefetch_heavy_snapshot(
        self,
        symbol,
        *,
        now=None,
        cached_position=None,
        cached_account_info=None,
    ):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay_seconds)
            position = cached_position or FuturesPosition(
                symbol=symbol,
                direction=PositionDirection.NONE,
                quantity=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )
            return HeavyMarketSnapshot(
                symbol=symbol,
                market=MarketSnapshot(
                    symbol=symbol,
                    price=Decimal("100"),
                    prices=[Decimal("100")],
                ),
                mark_price=Decimal("100"),
                exchange_rules=SymbolRules(
                    symbol=symbol,
                    quantity_step=Decimal("0.001"),
                    min_quantity=Decimal("0.001"),
                    min_notional=Decimal("5"),
                    price_tick=Decimal("0.1"),
                ),
                position=position,
                open_orders=[],
                open_algo_orders=[],
                account_info=cached_account_info or {},
                timestamp=float(now or time.time()),
                data_age_seconds=0.0,
                stale=False,
            )
        finally:
            with self.lock:
                self.active -= 1

    def invalidate_private_state_after_mutation(self, symbol, *, reason):
        self.invalidations.append((symbol, reason))
        return True


def _evaluation(symbol: str) -> SymbolEvaluationContext:
    evaluation = SymbolEvaluationContext(
        evaluation_input=SymbolEvaluationInput(
            symbol_config=SimpleNamespace(symbol=symbol),
            now=time.time(),
            cycle_id="cycle-1",
            symbol_eval_id=f"cycle-1:{symbol}:1",
        )
    )
    evaluation.score = SignalScore(
        symbol=symbol,
        score=75,
        action="NO_ACTION",
        reason="hot",
    )
    return evaluation


def _prefetcher(router):
    return HeavyPrefetcher(
        config=SimpleNamespace(
            governor_parallel_heavy_prefetch_enabled=True,
            governor_max_concurrent_symbol_snapshots=3,
            governor_shared_private_cycle_ttl_seconds=2.0,
        ),
        provider=None,
        router=router,
        journal=_Journal(),
    )


def _snapshots(router):
    return CycleSnapshots(
        provider=None,
        execution_engine=None,
        router=router,
        symbol_configs=[],
        journal=_Journal(),
        logger=None,
    )


def test_parallel_prefetch_resolves_heavy_snapshots_concurrently():
    router = _ParallelRouter(delay_seconds=0.30)
    prefetcher = _prefetcher(router)
    now = time.time()
    context = CycleContext(
        now=now,
        cycle_id="cycle-1",
        account_info={"availableBalance": "100"},
        account_info_loaded=True,
        private_state_loaded_at=now,
    )
    evaluations = [_evaluation(symbol) for symbol in ("BTCUSDT", "ETHUSDT", "LTCUSDT")]

    prefetcher.prefetch(context, evaluations)

    # Overlapping router calls prove that prefetch did not run serially. An elapsed
    # time bound also measures thread startup and is unreliable on a loaded CI host.
    assert router.max_active >= 2
    assert set(context.prefetched_heavy_snapshots) == {
        "BTCUSDT",
        "ETHUSDT",
        "LTCUSDT",
    }


def test_prefetched_snapshot_is_rejected_after_private_state_generation_changes():
    router = _ParallelRouter(delay_seconds=0)
    snapshots = _snapshots(router)
    now = time.time()
    context = CycleContext(now=now, cycle_id="cycle-1")
    snapshot = router.prefetch_heavy_snapshot("BTCUSDT", now=now)
    context.store_prefetched_heavy_snapshot(
        "BTCUSDT",
        snapshot=snapshot,
        captured_at=now,
    )

    snapshots.invalidate_private_state(
        context,
        symbol="ETHUSDT",
        reason="POSITION_CHANGED",
    )

    assert context.private_state_generation == 1
    assert context.prefetched_heavy_snapshots == {}
    assert context.operational_snapshot is None
    assert context.force_isolated_heavy_reads is True
    assert router.invalidations == [("ETHUSDT", "POSITION_CHANGED")]
    event_type, payload = snapshots.journal.events[-1]
    assert event_type == "cycle_private_state_invalidated"
    assert payload["severity"] == "INFO"


def test_prefetched_snapshot_expires_before_serial_execution():
    router = _ParallelRouter(delay_seconds=0)
    now = time.time()
    context = CycleContext(now=now, cycle_id="cycle-1")
    snapshot = router.prefetch_heavy_snapshot("BTCUSDT", now=now)
    context.store_prefetched_heavy_snapshot(
        "BTCUSDT",
        snapshot=snapshot,
        captured_at=now - 5,
    )

    result = context.take_prefetched_heavy_snapshot(
        "BTCUSDT",
        now=now,
        max_age_seconds=3,
    )

    assert result is None


def test_private_state_invalidation_resets_snapshot_readiness():
    ready_diagnostic = SnapshotReadinessDiagnostic(
        readiness=SnapshotReadiness.ENTRY_SAFE,
        reason="READY",
        policy_profile="evaluation",
    )
    context = CycleContext(
        now=time.time(),
        cycle_id="cycle-1",
        snapshot_baseline_ready=True,
        snapshot_selected_ready=True,
        snapshot_baseline_diagnostic=ready_diagnostic,
        snapshot_selected_diagnostic=ready_diagnostic,
        snapshot_execution_diagnostics={"BTCUSDT": ready_diagnostic},
    )

    context.invalidate_private_state()

    assert context.snapshot_baseline_ready is False
    assert context.snapshot_selected_ready is False
    assert context.snapshot_baseline_readiness is SnapshotReadiness.UNAVAILABLE
    assert context.snapshot_selected_readiness is SnapshotReadiness.UNAVAILABLE
    assert context.snapshot_baseline_diagnostic.reason == "NOT_EVALUATED"
    assert context.snapshot_selected_diagnostic.reason == "NOT_EVALUATED"
    assert context.snapshot_execution_diagnostics == {}


def test_prefetch_completed_event_tolerates_snapshot_without_data_age_seconds():
    """data_age_seconds must use getattr fallback — routers may omit the attribute."""

    class _NoAgeRouter(_ParallelRouter):
        def prefetch_heavy_snapshot(self, symbol, *, now=None, **kwargs):
            snap = super().prefetch_heavy_snapshot(symbol, now=now, **kwargs)
            # Simulate a router that returns a snapshot-like object without data_age_seconds.
            from types import SimpleNamespace

            return SimpleNamespace(
                symbol=snap.symbol,
                stale=False,
            )

    router = _NoAgeRouter(delay_seconds=0)
    prefetcher = _prefetcher(router)
    now = time.time()
    context = CycleContext(
        now=now,
        cycle_id="cycle-1",
        account_info={"availableBalance": "100"},
        account_info_loaded=True,
        private_state_loaded_at=now,
    )
    evaluations = [_evaluation(symbol) for symbol in ("BTCUSDT", "ETHUSDT", "LTCUSDT")]

    # Must not raise AttributeError even though the snapshot has no data_age_seconds.
    prefetcher.prefetch(context, evaluations)

    # All three snapshots stored successfully — the audit_event call with getattr fallback
    # must not have crashed and left symbols missing from prefetched_heavy_snapshots.
    assert set(context.prefetched_heavy_snapshots) == {"BTCUSDT", "ETHUSDT", "LTCUSDT"}
    assert context.prefetched_heavy_errors == {}


def test_prefetch_error_key_is_normalized_to_uppercase():
    """prefetched_heavy_errors must use the same normalized key as prefetched_heavy_snapshots."""

    class _FailingRouter(_ParallelRouter):
        def prefetch_heavy_snapshot(self, symbol, *, now=None, **kwargs):
            raise RuntimeError("network error")

    router = _FailingRouter(delay_seconds=0)
    prefetcher = _prefetcher(router)
    now = time.time()
    # Construct an evaluation with a lowercase symbol to prove normalization happens.
    eval_lower = SymbolEvaluationContext(
        evaluation_input=SymbolEvaluationInput(
            symbol_config=__import__("types").SimpleNamespace(symbol="btcusdt"),
            now=now,
            cycle_id="cycle-1",
            symbol_eval_id="cycle-1:btcusdt:1",
        )
    )
    eval_lower.score = SignalScore(symbol="btcusdt", score=75, action="NO_ACTION", reason="hot")
    eval_eth = _evaluation("ETHUSDT")
    context = CycleContext(
        now=now,
        cycle_id="cycle-1",
        private_state_loaded_at=now,
    )

    prefetcher.prefetch(context, [eval_lower, eval_eth])

    assert "BTCUSDT" in context.prefetched_heavy_errors, (
        "Error key must be uppercased to match store_prefetched_heavy_snapshot normalization"
    )
    assert "btcusdt" not in context.prefetched_heavy_errors


def test_stage_metrics_emitted_on_heavy_evaluation_exception():
    """SymbolEvaluator.record_stage_metrics must fire even when the heavy phase raises."""
    from types import SimpleNamespace

    from governor.evaluation import StageRunner
    from governor.evaluation.evaluator import SymbolEvaluator
    from governor.symbol_evaluation_pipeline import (
        SymbolEvaluationContext,
        SymbolEvaluationInput,
    )

    class _TrackingJournal(_Journal):
        def __init__(self):
            super().__init__()
            self.stage_metrics_calls = []

        def append(self, event_type, **payload):
            super().append(event_type, **payload)
            if event_type == "symbol_evaluation_stage_metrics":
                self.stage_metrics_calls.append(payload)

    def _failing_stage(evaluation):
        raise RuntimeError("heavy stage failure")

    journal = _TrackingJournal()
    evaluator = SymbolEvaluator(
        runner=StageRunner(),
        light_stages=lambda: (),
        heavy_stages=lambda: (_failing_stage,),
        journal=journal,
        trace_payload=lambda: {"cycle_id": "cycle-1", "symbol_eval_id": "cycle-1:BTCUSDT:1"},
        strategy_name=lambda: "test",
        cycle_id_for=lambda _context: "cycle-1",
        next_symbol_eval_id=lambda **_kwargs: "cycle-1:BTCUSDT:1",
    )

    evaluation = SymbolEvaluationContext(
        evaluation_input=SymbolEvaluationInput(
            symbol_config=SimpleNamespace(symbol="BTCUSDT"),
            now=time.time(),
            cycle_id="cycle-1",
            symbol_eval_id="cycle-1:BTCUSDT:1",
        )
    )
    evaluation.diagnostics["stage_timings"] = []
    evaluation.lifecycle_started_at = time.monotonic()

    with __import__("pytest").raises(RuntimeError, match="heavy stage failure"):
        evaluator.resume(evaluation)

    assert journal.stage_metrics_calls, (
        "record_stage_metrics must be called even when heavy phase raises"
    )
    assert journal.stage_metrics_calls[0]["terminal_stage"] == "ERROR"
