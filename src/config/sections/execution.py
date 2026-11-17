"""``ExecutionConfig`` group: environment, mode, authorization, credentials and order policies."""

from __future__ import annotations

from typing import TypedDict

from config.enums import (
    ExchangeEnvironment,
    ExecutionMode,
    ProtectionPolicy,
    ProtectionWorkingType,
    RecoveryAction,
)
from config.env_readers import (
    _get_bool,
    _get_env,
    _get_non_negative_float,
    _get_non_negative_float_alias,
)
from config.smart_settings import SmartExecutionSettings

TESTNET_BASE_URL = "https://demo-fapi.binance.com"
REAL_BASE_URL = "https://fapi.binance.com"
SPOT_TESTNET_BASE_URL = "https://testnet.binance.vision"
SPOT_REAL_BASE_URL = "https://api.binance.com"


class ExecutionFields(TypedDict):
    market_type: str
    allow_custom_binance_host: bool
    execution_mode: ExecutionMode
    environment: ExchangeEnvironment
    use_testnet: bool
    execute_orders: bool
    allow_real_trading: bool
    allow_real_user_data_cache_assisted: bool
    base_url: str
    api_key: str
    api_secret: str
    bot_order_prefix: str
    cancel_open_orders_when_flat: bool
    recover_on_boot: bool
    recovery_action: RecoveryAction
    cancel_orphan_orders_on_boot: bool
    protection_policy: ProtectionPolicy
    protection_working_type: ProtectionWorkingType
    entry_execution_style: str
    limit_entry_enabled: bool
    limit_entry_timeout_seconds: float
    limit_entry_max_price_drift_bps: float
    limit_entry_fallback_to_market: bool
    limit_entry_post_only: bool
    market_entry_require_economic_gate: bool
    smart_execution_settings: SmartExecutionSettings
    pre_dispatch_price_check_enabled: bool
    pre_dispatch_soft_price_drift_bps: float
    pre_dispatch_hard_price_drift_bps: float
    post_fill_protection_revalidation_enabled: bool
    post_fill_max_risk_increase_pct: float
    protection_trigger_buffer_bps: float


def load_execution_fields() -> ExecutionFields:
    legacy_use_testnet = _get_bool("ALPHORA_USE_TESTNET", True)
    legacy_execute_orders = _get_bool("ALPHORA_EXECUTE_ORDERS", False)
    execution_mode = load_execution_mode(
        legacy_use_testnet=legacy_use_testnet,
        legacy_execute_orders=legacy_execute_orders,
    )
    environment = load_environment(execution_mode, legacy_use_testnet)
    market_type = _get_env("ALPHORA_MARKET_TYPE", "SPOT").upper().strip()
    if market_type not in {"SPOT", "FUTURES"}:
        raise ValueError("ALPHORA_MARKET_TYPE deve ser SPOT ou FUTURES")
    return {
        "market_type": market_type,
        "allow_custom_binance_host": _get_bool("ALPHORA_ALLOW_CUSTOM_BINANCE_HOST", False),
        "execution_mode": execution_mode,
        "environment": environment,
        "use_testnet": environment is ExchangeEnvironment.TESTNET,
        "execute_orders": execution_mode.is_live,
        "allow_real_trading": _get_bool("ALPHORA_ALLOW_REAL_TRADING", False),
        "allow_real_user_data_cache_assisted": _get_bool(
            "ALPHORA_ALLOW_REAL_USER_DATA_CACHE_ASSISTED",
            False,
        ),
        "base_url": _base_url(environment, market_type),
        "api_key": (
            (_get_env("BINANCE_SPOT_API_KEY", "") or _get_env("BINANCE_API_KEY", ""))
            if market_type == "SPOT"
            else (_get_env("BINANCE_FUTURES_API_KEY", "") or _get_env("BINANCE_API_KEY", ""))
        ),
        "api_secret": (
            (_get_env("BINANCE_SPOT_API_SECRET", "") or _get_env("BINANCE_API_SECRET", ""))
            if market_type == "SPOT"
            else (_get_env("BINANCE_FUTURES_API_SECRET", "") or _get_env("BINANCE_API_SECRET", ""))
        ),
        "bot_order_prefix": _get_env("ALPHORA_BOT_ORDER_PREFIX", "ALPHORA"),
        "cancel_open_orders_when_flat": _get_bool("ALPHORA_CANCEL_OPEN_ORDERS_WHEN_FLAT", True),
        "recover_on_boot": _get_bool("ALPHORA_RECOVER_ON_BOOT", True),
        "recovery_action": RecoveryAction(_get_env("ALPHORA_RECOVERY_ACTION", "PROTECT").upper()),
        "cancel_orphan_orders_on_boot": _get_bool("ALPHORA_CANCEL_ORPHAN_ORDERS_ON_BOOT", True),
        "protection_policy": ProtectionPolicy(
            _get_env("ALPHORA_PROTECTION_POLICY", "AUTO_REPAIR").upper()
        ),
        "protection_working_type": ProtectionWorkingType(
            _get_env("ALPHORA_PROTECTION_WORKING_TYPE", "MARK_PRICE").upper()
        ),
        **_entry_fields(),
        **_dispatch_fields(),
    }


class _EntryFields(TypedDict):
    entry_execution_style: str
    limit_entry_enabled: bool
    limit_entry_timeout_seconds: float
    limit_entry_max_price_drift_bps: float
    limit_entry_fallback_to_market: bool
    limit_entry_post_only: bool
    market_entry_require_economic_gate: bool
    smart_execution_settings: SmartExecutionSettings


def _entry_fields() -> _EntryFields:
    return {
        "entry_execution_style": _get_env("ALPHORA_ENTRY_EXECUTION_STYLE", "MARKET_SAFE").upper(),
        "limit_entry_enabled": _get_bool("ALPHORA_LIMIT_ENTRY_ENABLED", False),
        "limit_entry_timeout_seconds": _get_non_negative_float(
            "ALPHORA_LIMIT_ENTRY_TIMEOUT_SECONDS",
            3.0,
        ),
        "limit_entry_max_price_drift_bps": _get_non_negative_float(
            "ALPHORA_LIMIT_ENTRY_MAX_PRICE_DRIFT_BPS",
            5.0,
        ),
        "limit_entry_fallback_to_market": _get_bool(
            "ALPHORA_LIMIT_ENTRY_FALLBACK_TO_MARKET",
            False,
        ),
        "limit_entry_post_only": _get_bool("ALPHORA_LIMIT_ENTRY_POST_ONLY", True),
        "market_entry_require_economic_gate": _get_bool(
            "ALPHORA_MARKET_ENTRY_REQUIRE_ECONOMIC_GATE",
            True,
        ),
        "smart_execution_settings": load_smart_execution_settings(),
    }


class _DispatchFields(TypedDict):
    pre_dispatch_price_check_enabled: bool
    pre_dispatch_soft_price_drift_bps: float
    pre_dispatch_hard_price_drift_bps: float
    post_fill_protection_revalidation_enabled: bool
    post_fill_max_risk_increase_pct: float
    protection_trigger_buffer_bps: float


def _dispatch_fields() -> _DispatchFields:
    return {
        "pre_dispatch_price_check_enabled": _get_bool(
            "ALPHORA_PRE_DISPATCH_PRICE_CHECK_ENABLED",
            True,
        ),
        "pre_dispatch_soft_price_drift_bps": _get_non_negative_float(
            "ALPHORA_PRE_DISPATCH_SOFT_PRICE_DRIFT_BPS",
            10.0,
        ),
        "pre_dispatch_hard_price_drift_bps": _get_non_negative_float(
            "ALPHORA_PRE_DISPATCH_HARD_PRICE_DRIFT_BPS",
            30.0,
        ),
        "post_fill_protection_revalidation_enabled": _get_bool(
            "ALPHORA_POST_FILL_PROTECTION_REVALIDATION_ENABLED",
            True,
        ),
        "post_fill_max_risk_increase_pct": _get_non_negative_float(
            "ALPHORA_POST_FILL_MAX_RISK_INCREASE_PCT",
            10.0,
        ),
        "protection_trigger_buffer_bps": _get_non_negative_float(
            "ALPHORA_PROTECTION_TRIGGER_BUFFER_BPS",
            3.0,
        ),
    }


def load_smart_execution_settings() -> SmartExecutionSettings:
    defaults = SmartExecutionSettings()
    return SmartExecutionSettings(
        max_spread_bps=_get_non_negative_float(
            "ALPHORA_SMART_EXECUTION_MAX_SPREAD_BPS",
            float(defaults.max_spread_bps),
        ),
        max_price_drift_bps=_get_non_negative_float_alias(
            "ALPHORA_SMART_EXECUTION_MAX_PRICE_DRIFT_BPS",
            "ALPHORA_LIMIT_ENTRY_MAX_PRICE_DRIFT_BPS",
            5.0,
        ),
        max_market_data_age_seconds=_get_non_negative_float(
            "ALPHORA_SMART_EXECUTION_MAX_MARKET_DATA_AGE_SECONDS",
            float(defaults.max_market_data_age_seconds),
        ),
        limit_entry_enabled=_get_bool("ALPHORA_LIMIT_ENTRY_ENABLED", False),
        limit_entry_timeout_seconds=_get_non_negative_float(
            "ALPHORA_LIMIT_ENTRY_TIMEOUT_SECONDS",
            float(defaults.limit_entry_timeout_seconds),
        ),
        limit_entry_fallback_to_market=_get_bool(
            "ALPHORA_LIMIT_ENTRY_FALLBACK_TO_MARKET",
            False,
        ),
        limit_entry_post_only=_get_bool("ALPHORA_LIMIT_ENTRY_POST_ONLY", True),
    )


def load_execution_mode(
    *,
    legacy_use_testnet: bool,
    legacy_execute_orders: bool,
) -> ExecutionMode:
    raw_mode = _get_env("ALPHORA_EXECUTION_MODE", "")

    if raw_mode:
        return ExecutionMode(raw_mode.upper())

    if not legacy_execute_orders:
        return ExecutionMode.DRY_RUN

    if legacy_use_testnet:
        return ExecutionMode.TESTNET_LIVE

    return ExecutionMode.REAL_LIVE


def load_environment(
    execution_mode: ExecutionMode,
    legacy_use_testnet: bool,
) -> ExchangeEnvironment:
    if execution_mode is ExecutionMode.TESTNET_LIVE:
        return ExchangeEnvironment.TESTNET

    if execution_mode is ExecutionMode.REAL_LIVE:
        return ExchangeEnvironment.REAL

    raw_environment = _get_env("ALPHORA_ENVIRONMENT", "")
    if raw_environment:
        return ExchangeEnvironment(raw_environment.upper())

    return ExchangeEnvironment.TESTNET if legacy_use_testnet else ExchangeEnvironment.REAL


def _base_url(environment: ExchangeEnvironment, market_type: str) -> str:
    configured_base_url = (
        (_get_env("BINANCE_SPOT_BASE_URL", "") if market_type == "SPOT"
         else _get_env("BINANCE_FUTURES_BASE_URL", ""))
        or _get_env("BINANCE_BASE_URL", "")
    )
    if configured_base_url:
        return configured_base_url
    if market_type == "SPOT":
        if environment is ExchangeEnvironment.TESTNET:
            return SPOT_TESTNET_BASE_URL
        return SPOT_REAL_BASE_URL
    if environment is ExchangeEnvironment.TESTNET:
        return TESTNET_BASE_URL
    return REAL_BASE_URL


__all__ = [
    "REAL_BASE_URL",
    "SPOT_REAL_BASE_URL",
    "SPOT_TESTNET_BASE_URL",
    "TESTNET_BASE_URL",
    "ExecutionFields",
    "load_environment",
    "load_execution_fields",
    "load_execution_mode",
    "load_smart_execution_settings",
]
