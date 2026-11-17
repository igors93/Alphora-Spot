"""``RuntimeConfig`` group: files and paths, loop cadence, back-off and Governor scheduling."""

from __future__ import annotations

from typing import TypedDict

from config.enums import ExchangeEnvironment, ExecutionMode
from config.env_readers import (
    _get_bool,
    _get_env,
    _get_float,
    _get_int,
    _get_non_negative_float,
    _get_non_negative_int,
)
from config.models import TradingSymbolConfig
from config.state_paths import financial_path_key, resolve_financial_paths
from config.validators import _build_symbol_contexts

DEFAULT_TRADE_AUDIT_PATH = "data/trade_audit.jsonl"


class RuntimePathFields(TypedDict):
    runtime_dir: str
    state_path: str
    trade_journal_path: str
    trade_audit_path: str
    symbol_contexts: tuple[TradingSymbolConfig, ...]
    lock_enabled: bool


def load_runtime_path_fields(
    *,
    symbols: tuple[str, ...],
    environment: ExchangeEnvironment,
    execution_mode: ExecutionMode,
) -> RuntimePathFields:
    """State and journal files of every symbol, separated by environment and mode."""

    runtime_dir = _get_env("ALPHORA_RUNTIME_DIR", "runtime")
    path_key = financial_path_key(environment.value, execution_mode.value)
    state_path, journal_path = resolve_financial_paths(runtime_dir, symbols[0], path_key)
    return {
        "runtime_dir": runtime_dir,
        "state_path": state_path,
        "trade_journal_path": journal_path,
        "trade_audit_path": _get_env("ALPHORA_TRADE_AUDIT_PATH", DEFAULT_TRADE_AUDIT_PATH),
        "symbol_contexts": _build_symbol_contexts(
            symbols=symbols,
            runtime_dir=runtime_dir,
            default_state_path=state_path,
            default_journal_path=journal_path,
            environment_key=path_key,
        ),
        "lock_enabled": _get_bool("ALPHORA_LOCK_ENABLED", True),
    }


class SchedulingFields(TypedDict):
    loop_interval_seconds: float
    max_consecutive_errors: int
    initial_backoff_seconds: float
    max_backoff_seconds: float
    governor_fast_lane_enabled: bool
    governor_fast_lane_max_symbols_per_cycle: int
    governor_fast_lane_cooldown_seconds: float
    governor_wakeup_enabled: bool
    governor_wakeup_cooldown_seconds: float
    governor_request_budget_enabled: bool
    governor_request_budget_max_heavy_checks_per_cycle: int
    governor_request_budget_open_position_override: bool
    governor_parallel_heavy_prefetch_enabled: bool
    governor_max_concurrent_symbol_snapshots: int
    governor_prefetched_snapshot_max_age_seconds: float
    governor_shared_private_cycle_ttl_seconds: float


def load_scheduling_fields() -> SchedulingFields:
    return {
        "loop_interval_seconds": _get_float("ALPHORA_LOOP_INTERVAL_SECONDS", 3.0),
        "max_consecutive_errors": _get_int("ALPHORA_MAX_CONSECUTIVE_ERRORS", 5),
        "initial_backoff_seconds": _get_float("ALPHORA_INITIAL_BACKOFF_SECONDS", 1.0),
        "max_backoff_seconds": _get_float("ALPHORA_MAX_BACKOFF_SECONDS", 30.0),
        "governor_fast_lane_enabled": _get_bool("ALPHORA_GOVERNOR_FAST_LANE_ENABLED", True),
        "governor_fast_lane_max_symbols_per_cycle": _get_non_negative_int(
            "ALPHORA_GOVERNOR_FAST_LANE_MAX_SYMBOLS_PER_CYCLE",
            3,
        ),
        "governor_fast_lane_cooldown_seconds": _get_non_negative_float(
            "ALPHORA_GOVERNOR_FAST_LANE_COOLDOWN_SECONDS",
            2.0,
        ),
        "governor_wakeup_enabled": _get_bool("ALPHORA_GOVERNOR_WAKEUP_ENABLED", True),
        "governor_wakeup_cooldown_seconds": _get_non_negative_float(
            "ALPHORA_GOVERNOR_WAKEUP_COOLDOWN_SECONDS",
            1.0,
        ),
        "governor_request_budget_enabled": _get_bool(
            "ALPHORA_GOVERNOR_REQUEST_BUDGET_ENABLED",
            True,
        ),
        "governor_request_budget_max_heavy_checks_per_cycle": _get_non_negative_int(
            "ALPHORA_GOVERNOR_REQUEST_BUDGET_MAX_HEAVY_CHECKS_PER_CYCLE",
            3,
        ),
        "governor_request_budget_open_position_override": _get_bool(
            "ALPHORA_GOVERNOR_REQUEST_BUDGET_OPEN_POSITION_OVERRIDE",
            True,
        ),
        "governor_parallel_heavy_prefetch_enabled": _get_bool(
            "ALPHORA_GOVERNOR_PARALLEL_HEAVY_PREFETCH_ENABLED",
            True,
        ),
        "governor_max_concurrent_symbol_snapshots": _get_non_negative_int(
            "ALPHORA_GOVERNOR_MAX_CONCURRENT_SYMBOL_SNAPSHOTS",
            3,
        ),
        "governor_prefetched_snapshot_max_age_seconds": _get_non_negative_float(
            "ALPHORA_GOVERNOR_PREFETCHED_SNAPSHOT_MAX_AGE_SECONDS",
            3.0,
        ),
        "governor_shared_private_cycle_ttl_seconds": _get_non_negative_float(
            "ALPHORA_GOVERNOR_SHARED_PRIVATE_CYCLE_TTL_SECONDS",
            2.0,
        ),
    }


__all__ = [
    "DEFAULT_TRADE_AUDIT_PATH",
    "RuntimePathFields",
    "SchedulingFields",
    "load_runtime_path_fields",
    "load_scheduling_fields",
]
