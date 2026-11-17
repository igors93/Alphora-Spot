from dataclasses import dataclass, field
from typing import Any

from domain.models import FuturesPosition
from governor.defensive_mode import DefensiveModeEvaluation
from governor.request_budget import RequestBudget
from market_data.snapshot_provider.readiness import (
    SnapshotReadiness,
    SnapshotReadinessDiagnostic,
    not_evaluated_diagnostic,
)


@dataclass(frozen=True)
class PrefetchedHeavySnapshot:
    snapshot: Any
    captured_at: float
    private_state_generation: int


@dataclass
class CycleContext:
    now: float
    cycle_id: str = ""
    positions: dict[str, FuturesPosition] = field(default_factory=dict)
    account_info: dict[str, Any] | None = None
    account_info_loaded: bool = False
    daily_metrics: dict[str, Any] | None = None
    fast_lane_symbols: set[str] = field(default_factory=set)
    request_budget: RequestBudget | None = None
    defensive_mode: DefensiveModeEvaluation | None = None
    defensive_blocks: list[dict[str, Any]] = field(default_factory=list)
    operational_snapshot: Any = None
    snapshot_session: Any = None
    snapshot_baseline_ready: bool = False
    snapshot_selected_ready: bool = False
    snapshot_baseline_diagnostic: SnapshotReadinessDiagnostic = field(
        default_factory=lambda: not_evaluated_diagnostic("baseline")
    )
    snapshot_selected_diagnostic: SnapshotReadinessDiagnostic = field(
        default_factory=lambda: not_evaluated_diagnostic("evaluation")
    )
    snapshot_execution_diagnostics: dict[str, SnapshotReadinessDiagnostic] = field(
        default_factory=dict
    )
    prefetched_heavy_snapshots: dict[str, PrefetchedHeavySnapshot] = field(default_factory=dict)
    prefetched_heavy_errors: dict[str, str] = field(default_factory=dict)
    private_state_generation: int = 0
    private_state_loaded_at: float = 0.0
    force_isolated_heavy_reads: bool = False

    @property
    def snapshot_baseline_readiness(self) -> SnapshotReadiness:
        return self.snapshot_baseline_diagnostic.readiness

    @property
    def snapshot_selected_readiness(self) -> SnapshotReadiness:
        return self.snapshot_selected_diagnostic.readiness

    def shared_private_state_is_fresh(self, *, now: float, ttl_seconds: float) -> bool:
        if self.private_state_loaded_at <= 0:
            return False
        return max(0.0, float(now) - self.private_state_loaded_at) <= max(0.0, float(ttl_seconds))

    def store_prefetched_heavy_snapshot(
        self,
        symbol: str,
        *,
        snapshot: Any,
        captured_at: float,
    ) -> None:
        normalized = str(symbol or "").upper().strip()
        if not normalized:
            return
        self.prefetched_heavy_snapshots[normalized] = PrefetchedHeavySnapshot(
            snapshot=snapshot,
            captured_at=float(captured_at),
            private_state_generation=self.private_state_generation,
        )
        self.prefetched_heavy_errors.pop(normalized, None)

    def take_prefetched_heavy_snapshot(
        self,
        symbol: str,
        *,
        now: float,
        max_age_seconds: float,
    ) -> Any | None:
        normalized = str(symbol or "").upper().strip()
        entry = self.prefetched_heavy_snapshots.pop(normalized, None)
        if entry is None:
            return None
        if entry.private_state_generation != self.private_state_generation:
            return None

        snapshot = entry.snapshot
        elapsed_since_capture = max(0.0, float(now) - entry.captured_at)
        source_age = max(0.0, float(getattr(snapshot, "data_age_seconds", 0.0) or 0.0))
        effective_age = source_age + elapsed_since_capture
        if bool(getattr(snapshot, "stale", False)) or effective_age > max(
            0.0, float(max_age_seconds)
        ):
            return None
        return snapshot

    def invalidate_private_state(self) -> None:
        self.private_state_generation += 1
        self.prefetched_heavy_snapshots.clear()
        self.prefetched_heavy_errors.clear()
        self.positions.clear()
        self.account_info = None
        self.account_info_loaded = False
        self.private_state_loaded_at = 0.0
        self.operational_snapshot = None
        self.snapshot_baseline_ready = False
        self.snapshot_selected_ready = False
        self.snapshot_baseline_diagnostic = not_evaluated_diagnostic("baseline")
        self.snapshot_selected_diagnostic = not_evaluated_diagnostic("evaluation")
        self.snapshot_execution_diagnostics.clear()
        self.force_isolated_heavy_reads = True
