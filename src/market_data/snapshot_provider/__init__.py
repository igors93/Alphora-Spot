"""The Governor's operational snapshot provider over the embedded snapshot engine.

Modules by responsibility:

- ``provider``: :class:`SnapshotProvider`, the facade the Governor holds for a run;
- ``session``: :class:`SnapshotCycleSession`, one cumulative session per Governor cycle;
- ``factory``: start the provider under the startup policy of the execution mode;
- ``settings``: provider settings, startup policy and result types;
- ``keys``: the resource keys the Governor reads;
- ``sources`` / ``builder`` / ``fetchers`` / ``transport``: where each key comes from;
- ``policies`` / ``readiness``: acceptance, consistency and degradation per usage profile;
- ``views``: light and heavy market snapshots read out of an engine snapshot;
- ``user_data`` / ``submissions``: User Data Stream writes into the engine cache;
- ``health`` / ``diagnostics`` / ``event_sink`` / ``metrics_sink``: observability;
- ``wire_names``: persisted names kept for compatibility (ADR-002).
"""

from __future__ import annotations

from market_data.snapshot_engine import (
    RequestDegradationPolicy,
    SnapshotEngineError,
    SnapshotResult,
    SnapshotResultState,
)
from market_data.snapshot_provider import wire_names
from market_data.snapshot_provider.diagnostics import (
    snapshot_diagnostics_payload,
    snapshot_error_diagnostics_payload,
    snapshot_error_failure_summary,
    snapshot_error_failure_summary_text,
)
from market_data.snapshot_provider.engine_errors import extract_engine_error_diagnostics
from market_data.snapshot_provider.factory import build_snapshot_provider
from market_data.snapshot_provider.health import normalize_builder_health
from market_data.snapshot_provider.keys import (
    ACCOUNT,
    ALL_POSITIONS,
    EXCHANGE_INFO,
    exchange_rules,
    mark_price,
    market_state,
    open_algo_orders,
    open_orders,
    position,
)
from market_data.snapshot_provider.provider import (
    SnapshotProvider,
    close_live_snapshot_providers_for_testing,
)
from market_data.snapshot_provider.readiness import (
    SnapshotReadiness,
    SnapshotReadinessDiagnostic,
)
from market_data.snapshot_provider.session import SnapshotCycleSession
from market_data.snapshot_provider.settings import (
    ExchangeInfoBootstrapResult,
    SnapshotProviderSettings,
    SnapshotStartupPolicy,
    SnapshotTransportBudgetExceeded,
    SubmissionReconciliationResult,
    resolve_startup_policy,
)
from observability import register_error_diagnostics

# Observability error events carry the engine diagnostics of snapshot errors under this key.
register_error_diagnostics(wire_names.PAYLOAD_KEY, extract_engine_error_diagnostics)

__all__ = [
    "ACCOUNT",
    "ALL_POSITIONS",
    "EXCHANGE_INFO",
    "ExchangeInfoBootstrapResult",
    "RequestDegradationPolicy",
    "SnapshotCycleSession",
    "SnapshotEngineError",
    "SnapshotProvider",
    "SnapshotProviderSettings",
    "SnapshotReadiness",
    "SnapshotReadinessDiagnostic",
    "SnapshotResult",
    "SnapshotResultState",
    "SnapshotStartupPolicy",
    "SnapshotTransportBudgetExceeded",
    "SubmissionReconciliationResult",
    "build_snapshot_provider",
    "extract_engine_error_diagnostics",
    "close_live_snapshot_providers_for_testing",
    "exchange_rules",
    "mark_price",
    "market_state",
    "normalize_builder_health",
    "open_algo_orders",
    "open_orders",
    "position",
    "resolve_startup_policy",
    "snapshot_diagnostics_payload",
    "snapshot_error_diagnostics_payload",
    "snapshot_error_failure_summary",
    "snapshot_error_failure_summary_text",
]
