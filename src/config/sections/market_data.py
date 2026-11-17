"""``MarketDataConfig`` group: market and user data streams, market shock and candles.

The snapshot provider (``config.snapshot_env``) and the shared REST weight count
(``config.rest_weight_env``) belong to the same group and keep their own loaders.
"""

from __future__ import annotations

from typing import TypedDict

from config.enums import ExecutionMode
from config.env_readers import (
    _get_bool,
    _get_env,
    _get_float,
    _get_int,
    _get_non_negative_float,
    _get_non_negative_int,
)
from config.snapshot_env import load_candle_klines_url
from operational_module_modes import MODULE_STATE_RECONCILIATION, ModuleModeResolution


class MarketStreamFields(TypedDict):
    market_ws_enabled: bool
    market_ws_base_url: str
    market_ws_stream: str
    market_ws_heartbeat_seconds: float
    market_ws_max_price_age_seconds: float
    market_ws_reconnect_seconds: float
    market_ws_warmup_seconds: float
    market_ws_dirty_symbols_limit: int
    market_ws_price_window: int
    market_ws_max_symbols_per_drain: int
    market_shock_detector_enabled: bool
    market_shock_window_seconds: float
    market_shock_move_bps_threshold: float
    market_shock_critical_bps_threshold: float
    market_shock_cooldown_seconds: float
    candle_hub_mode: str
    candle_klines_url: str


def load_market_stream_fields() -> MarketStreamFields:
    return {
        "market_ws_enabled": _get_bool("ALPHORA_MARKET_WS_ENABLED", True),
        "market_ws_base_url": _get_env("ALPHORA_MARKET_WS_BASE_URL", ""),
        "market_ws_stream": _get_env("ALPHORA_MARKET_WS_STREAM", "bookTicker"),
        "market_ws_heartbeat_seconds": _get_float("ALPHORA_MARKET_WS_HEARTBEAT_SECONDS", 60.0),
        "market_ws_max_price_age_seconds": _get_float(
            "ALPHORA_MARKET_WS_MAX_PRICE_AGE_SECONDS",
            3.0,
        ),
        "market_ws_reconnect_seconds": _get_float("ALPHORA_MARKET_WS_RECONNECT_SECONDS", 5.0),
        "market_ws_warmup_seconds": _get_non_negative_float(
            "ALPHORA_MARKET_WS_WARMUP_SECONDS",
            2.0,
        ),
        "market_ws_dirty_symbols_limit": _get_int("ALPHORA_MARKET_WS_DIRTY_SYMBOLS_LIMIT", 500),
        "market_ws_price_window": _get_int("ALPHORA_MARKET_WS_PRICE_WINDOW", 120),
        "market_ws_max_symbols_per_drain": _get_int(
            "ALPHORA_MARKET_WS_MAX_SYMBOLS_PER_DRAIN",
            50,
        ),
        "market_shock_detector_enabled": _get_bool("ALPHORA_MARKET_SHOCK_DETECTOR_ENABLED", True),
        "market_shock_window_seconds": _get_float("ALPHORA_MARKET_SHOCK_WINDOW_SECONDS", 3.0),
        "market_shock_move_bps_threshold": _get_float(
            "ALPHORA_MARKET_SHOCK_MOVE_BPS_THRESHOLD",
            50.0,
        ),
        "market_shock_critical_bps_threshold": _get_float(
            "ALPHORA_MARKET_SHOCK_CRITICAL_BPS_THRESHOLD",
            120.0,
        ),
        "market_shock_cooldown_seconds": _get_non_negative_float(
            "ALPHORA_MARKET_SHOCK_COOLDOWN_SECONDS",
            10.0,
        ),
        "candle_hub_mode": _get_env("ALPHORA_CANDLE_HUB_MODE", "AUTO").upper(),
        "candle_klines_url": load_candle_klines_url(),
    }


class UserDataCacheFields(TypedDict):
    user_data_cache_operational_preset: str
    user_data_cache_effective_preset: str
    user_data_stream_cache_position_enabled: bool
    user_data_stream_cache_account_enabled: bool
    user_data_stream_cache_open_orders_enabled: bool
    user_data_stream_cache_reconciliation_enabled: bool


def load_user_data_cache_fields(
    *,
    execution_mode: ExecutionMode,
    allow_real_user_data_cache_assisted: bool,
    modules: ModuleModeResolution,
) -> UserDataCacheFields:
    """Cache sources the operator enabled, minus what the mode or the preset forbids."""

    operational_preset = _get_env("ALPHORA_USER_DATA_CACHE_OPERATIONAL_PRESET", "")
    effective_preset = _get_env("ALPHORA_USER_DATA_CACHE_EFFECTIVE_PRESET", operational_preset)
    position = _get_bool("ALPHORA_USER_DATA_STREAM_CACHE_POSITION_ENABLED", False)
    account = _get_bool("ALPHORA_USER_DATA_STREAM_CACHE_ACCOUNT_ENABLED", False)
    open_orders = _get_bool("ALPHORA_USER_DATA_STREAM_CACHE_OPEN_ORDERS_ENABLED", False)
    real_without_permission = (
        execution_mode is ExecutionMode.REAL_LIVE and not allow_real_user_data_cache_assisted
    )
    # An explicitly DISABLED preset turns off every private-source component, whatever the
    # state_reconciliation mode: ALERT must not reactivate a cache the operator turned off.
    preset_disabled = str(effective_preset or "").upper().strip() == "DISABLED"
    if real_without_permission or preset_disabled:
        position = account = open_orders = False
    reconciliation = modules.modes[MODULE_STATE_RECONCILIATION].is_enabled
    return {
        "user_data_cache_operational_preset": operational_preset,
        "user_data_cache_effective_preset": effective_preset,
        "user_data_stream_cache_position_enabled": position,
        "user_data_stream_cache_account_enabled": account,
        "user_data_stream_cache_open_orders_enabled": open_orders,
        "user_data_stream_cache_reconciliation_enabled": (
            False if preset_disabled else reconciliation
        ),
    }


class UserDataStreamFields(TypedDict):
    user_data_stream_enabled: bool
    user_data_stream_shadow_only: bool
    user_data_stream_ws_base_url: str
    user_data_stream_keepalive_seconds: float
    user_data_stream_reconnect_seconds: float
    user_data_stream_heartbeat_seconds: float
    user_data_stream_max_pending_events: int
    user_data_stream_cache_audit_enabled: bool
    user_data_stream_cache_audit_max_age_seconds: float
    user_data_stream_cache_usage_shadow_enabled: bool
    user_data_stream_cache_usage_require_audit_match: bool
    user_data_stream_cache_usage_max_age_seconds: float
    user_data_stream_cache_usage_max_audit_age_seconds: float
    user_data_stream_cache_assisted_plan_shadow_enabled: bool
    user_data_stream_cache_stale_guard_enabled: bool
    user_data_stream_cache_stale_guard_max_message_age_seconds: float
    user_data_stream_cache_stale_guard_require_listen_key: bool


def load_user_data_stream_fields() -> UserDataStreamFields:
    return {
        "user_data_stream_enabled": _get_bool("ALPHORA_USER_DATA_STREAM_ENABLED", False),
        "user_data_stream_shadow_only": _get_bool("ALPHORA_USER_DATA_STREAM_SHADOW_ONLY", True),
        "user_data_stream_ws_base_url": _get_env("ALPHORA_USER_DATA_STREAM_WS_BASE_URL", ""),
        "user_data_stream_keepalive_seconds": _get_non_negative_float(
            "ALPHORA_USER_DATA_STREAM_KEEPALIVE_SECONDS",
            30 * 60,
        ),
        "user_data_stream_reconnect_seconds": _get_non_negative_float(
            "ALPHORA_USER_DATA_STREAM_RECONNECT_SECONDS",
            5.0,
        ),
        "user_data_stream_heartbeat_seconds": _get_non_negative_float(
            "ALPHORA_USER_DATA_STREAM_HEARTBEAT_SECONDS",
            60.0,
        ),
        "user_data_stream_max_pending_events": _get_non_negative_int(
            "ALPHORA_USER_DATA_STREAM_MAX_PENDING_EVENTS",
            500,
        ),
        "user_data_stream_cache_audit_enabled": _get_bool(
            "ALPHORA_USER_DATA_STREAM_CACHE_AUDIT_ENABLED", True
        ),
        "user_data_stream_cache_audit_max_age_seconds": _get_non_negative_float(
            "ALPHORA_USER_DATA_STREAM_CACHE_AUDIT_MAX_AGE_SECONDS",
            30.0,
        ),
        "user_data_stream_cache_usage_shadow_enabled": _get_bool(
            "ALPHORA_USER_DATA_STREAM_CACHE_USAGE_SHADOW_ENABLED",
            True,
        ),
        "user_data_stream_cache_usage_require_audit_match": _get_bool(
            "ALPHORA_USER_DATA_STREAM_CACHE_USAGE_REQUIRE_AUDIT_MATCH",
            True,
        ),
        "user_data_stream_cache_usage_max_age_seconds": _get_non_negative_float(
            "ALPHORA_USER_DATA_STREAM_CACHE_USAGE_MAX_AGE_SECONDS",
            10.0,
        ),
        "user_data_stream_cache_usage_max_audit_age_seconds": _get_non_negative_float(
            "ALPHORA_USER_DATA_STREAM_CACHE_USAGE_MAX_AUDIT_AGE_SECONDS",
            60.0,
        ),
        "user_data_stream_cache_assisted_plan_shadow_enabled": _get_bool(
            "ALPHORA_USER_DATA_STREAM_CACHE_ASSISTED_PLAN_SHADOW_ENABLED",
            True,
        ),
        "user_data_stream_cache_stale_guard_enabled": _get_bool(
            "ALPHORA_USER_DATA_STREAM_CACHE_STALE_GUARD_ENABLED",
            True,
        ),
        "user_data_stream_cache_stale_guard_max_message_age_seconds": _get_non_negative_float(
            "ALPHORA_USER_DATA_STREAM_CACHE_STALE_GUARD_MAX_MESSAGE_AGE_SECONDS",
            30.0,
        ),
        "user_data_stream_cache_stale_guard_require_listen_key": _get_bool(
            "ALPHORA_USER_DATA_STREAM_CACHE_STALE_GUARD_REQUIRE_LISTEN_KEY",
            True,
        ),
    }


__all__ = [
    "MarketStreamFields",
    "UserDataCacheFields",
    "UserDataStreamFields",
    "load_market_stream_fields",
    "load_user_data_cache_fields",
    "load_user_data_stream_fields",
]
