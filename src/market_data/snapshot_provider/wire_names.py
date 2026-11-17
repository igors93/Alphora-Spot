"""Snapshot provider names that leave the process: journal events, reason codes and payload keys.

legacy-wire-names (ADR-002): the operational snapshot provider was first integrated under the name
"Coalestra". These values are part of the persisted contract read by the desktop, by operators and by
the history of earlier runs: journal and audit event types, reason and stage codes shown in
``governor_state.json``, payload keys and source labels. They keep their spelling until the event
schema of migration M7 versions them. Code may not spell the legacy name anywhere else;
``tools/architecture_metrics.py`` counts it outside the modules marked like this one.
"""

from __future__ import annotations

# --- provider lifecycle and health (audit log) -------------------------------------------------
PROVIDER_STARTED = "coalestra_snapshot_provider_started"
PROVIDER_UNAVAILABLE = "coalestra_snapshot_provider_unavailable"
BUILDER_HEALTH = "coalestra_builder_health"
BUILDER_HEALTH_FINDING = "coalestra_builder_health_finding"
BACKLOG_HIGH = "coalestra_backlog_high"
WAITING_FOR_CAPACITY = "coalestra_waiting_for_capacity"
SUBMISSION_FLUSH = "coalestra_submission_flush"
SUBMISSION_RECONCILIATION = "coalestra_submission_reconciliation"
SUBMISSION_RECONCILIATION_FAILED = "coalestra_submission_reconciliation_failed"
# ``source``/``resource`` labels of the error records the provider sends to observability.
ERROR_SOURCE_BUILDER = "coalestra:builder"
ERROR_SOURCE_SNAPSHOT_BUILDER = "coalestra_snapshot_builder"
ERROR_SOURCE_PROVIDER = "coalestra_snapshot_provider"
ERROR_RESOURCE_PROVIDER = "coalestra:provider"
ERROR_SOURCE_PUBLISHER = "coalestra:publisher"

# --- engine events and metrics forwarded to the audit log --------------------------------------
ENGINE_EVENT_PREFIX = "coalestra_"
UNKNOWN_ENGINE_EVENT = "coalestra_unknown_event"
UNKNOWN_ENGINE_EVENT_TYPE_KEY = "unknown_coalestra_event_type"
ENGINE_METRIC = "coalestra_metric"

# --- cycle session (audit log and journal) -----------------------------------------------------
BASELINE_TRY_RESOLVED = "coalestra_baseline_try_resolved"
SELECTED_TRY_RESOLVED = "coalestra_selected_try_resolved"
LIGHT_SELECTED_REJECTED = "coalestra_light_selected_rejected"
CYCLE_BASELINE_FAILED = "coalestra_cycle_baseline_failed"
CYCLE_SELECTED_FAILED = "coalestra_cycle_selected_failed"
SYMBOL_EVALUATION_TRY_RESOLVED = "coalestra_symbol_evaluation_try_resolved"
SYMBOL_EVALUATION_SNAPSHOT_FAILED = "coalestra_symbol_evaluation_snapshot_failed"
EXCHANGE_INFO_BOOTSTRAPPED = "coalestra_exchange_info_bootstrapped"
EXCHANGE_INFO_BOOTSTRAP_FAILED = "coalestra_exchange_info_bootstrap_failed"
EXCHANGE_INFO_BOOTSTRAP_ERROR_SOURCE = "coalestra_exchange_info_bootstrap"

# --- parallel heavy prefetch (journal) ---------------------------------------------------------
PARALLEL_PREFETCH_RESULT = "parallel_heavy_prefetch_coalestra_result"
PARALLEL_PREFETCH_FAILED = "parallel_heavy_prefetch_coalestra_failed"
PARTITIONED_PREFETCH_MODE = "coalestra_partitioned"

# --- market data router (audit log) and snapshot source labels ---------------------------------
CYCLE_LIGHT_FALLBACK = "coalestra_cycle_light_fallback"
CYCLE_LIGHT_REFRESH_FAILED = "coalestra_cycle_light_refresh_failed"
CYCLE_LIGHT_REFRESHED = "coalestra_cycle_light_refreshed"
CYCLE_HEAVY_FALLBACK = "coalestra_cycle_heavy_fallback"
CYCLE_HEAVY_REFRESH_FAILED = "coalestra_cycle_heavy_refresh_failed"
CYCLE_HEAVY_REFRESHED = "coalestra_cycle_heavy_refreshed"
SOURCE_LABEL_PROVIDER = "coalestra"
SOURCE_LABEL_CYCLE = "coalestra_cycle"
SOURCE_LABEL_PARALLEL_PREFETCH = "coalestra_parallel_prefetch"

# --- stage 22: pre-execution revalidation (journal, audit log, runtime board) ------------------
EXIT_DEGRADED_WITHOUT_SESSION = "coalestra_exit_degraded_without_session"
PRE_EXECUTION_REVALIDATED = "coalestra_pre_execution_revalidated"
PRE_EXECUTION_REVALIDATION_FAILED = "coalestra_pre_execution_revalidation_failed"
EXIT_POSITION_VIEW_PRESERVED = "coalestra_exit_position_view_preserved"
TRADE_BLOCKED_AFTER_REVALIDATION = "trade_blocked_by_governor_after_coalestra_revalidation"
DEGRADED_SNAPSHOT_REJECTED_FOR_ENTRY = "coalestra_degraded_snapshot_rejected_for_entry"
DEGRADED_SNAPSHOT_USED = "coalestra_degraded_snapshot_used"
RISK_REDUCTION_TRY_RESOLVED = "coalestra_risk_reduction_try_resolved"
RISK_REDUCTION_FALLBACK_FAILED = "coalestra_risk_reduction_fallback_failed"

REASON_SESSION_UNAVAILABLE = "COALESTRA_SESSION_UNAVAILABLE"
REASON_SESSION_UNAVAILABLE_EXIT_DEGRADED = "COALESTRA_SESSION_UNAVAILABLE_EXIT_DEGRADED"
REASON_ENTRY_NOT_SAFE = "COALESTRA_ENTRY_NOT_SAFE"
REASON_EXIT_DATA_UNAVAILABLE = "COALESTRA_EXIT_DATA_UNAVAILABLE"
REASON_EXIT_REVALIDATION_FAILED = "COALESTRA_EXIT_REVALIDATION_FAILED"
REASON_PRE_EXECUTION_REVALIDATION_FAILED = "COALESTRA_PRE_EXECUTION_REVALIDATION_FAILED"
REASON_ENTRY_NOT_SAFE_AT_DISPATCH = "COALESTRA_ENTRY_NOT_SAFE_AT_DISPATCH"
REASON_EXIT_NOT_SAFE_AT_DISPATCH = "COALESTRA_EXIT_NOT_SAFE_AT_DISPATCH"

STAGE_ENTRY_GUARD = "COALESTRA_ENTRY_GUARD"
STAGE_ENTRY_READINESS = "COALESTRA_ENTRY_READINESS"
STAGE_EXIT_READINESS = "COALESTRA_EXIT_READINESS"
STAGE_PRE_EXECUTION_REVALIDATION = "COALESTRA_PRE_EXECUTION_REVALIDATION"
STAGE_PORTFOLIO_AFTER_REVALIDATION = "PORTFOLIO_GOVERNOR_AFTER_COALESTRA_REVALIDATION"
STAGE_DISPATCH_READINESS = "COALESTRA_DISPATCH_READINESS"

# --- payload keys and startup policy reasons ---------------------------------------------------
PAYLOAD_KEY = "coalestra"
ENABLED_KEY = "coalestra_enabled"
VERSION_KEY = "coalestra_version"
REQUIRED_KEY = "coalestra_required"
REASON_PRODUCTION_REQUIRES = "production_requires_coalestra"
REASON_TESTNET_REQUIRES = "testnet_requires_coalestra"
