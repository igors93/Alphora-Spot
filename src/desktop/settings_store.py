import json
import os
from pathlib import Path
from typing import Any

from desktop.user_data_cache_presets import (
    USER_DATA_CACHE_PRESET_DISABLED,
    apply_user_data_cache_preset,
)
from module_paths import qualify_module_path
from operational_module_modes import (
    MODULE_DYNAMIC_EXIT,
    MODULE_ENV_KEYS,
    MODULE_LEARNING_ADVISORY,
    MODULE_MODE_ATTRS,
    MODULE_PORTFOLIO_ALLOCATOR,
    MODULE_SMART_EXECUTION,
    MODULE_SMART_SIZING,
    MODULE_STATE_RECONCILIATION,
    modes_for_intelligent_activation,
    module_modes_from_mapping,
    module_modes_to_values,
    module_runtime_flags_from_modes,
)
from research.ai.training_defaults import (
    DEFAULT_HORIZON_CANDLES,
    DEFAULT_MIN_TRAINING_ROWS,
    DEFAULT_THRESHOLD_BPS,
)

LOCAL_CONFIG_DIR = "config"
SETTINGS_PATH = "config/alphora_gui.local.json"
SYMBOL_PROFILES_PATH = "config/alphora_symbols.local.json"
SINGLE_SYMBOL_PROFILES_PATH = "config/alphora_single_symbol.local.json"
PORTFOLIO_RISK_PATH = "config/alphora_portfolio.local.json"
ARCHITECTURE_INTELLIGENT_FLOW = "INTELLIGENT_FLOW"

INTELLIGENT_LEVEL_SHADOW_ONLY = "SHADOW_ONLY"
INTELLIGENT_LEVEL_PAPER_ACTIVE = "PAPER_ACTIVE"
INTELLIGENT_LEVEL_TESTNET_OBSERVE = "TESTNET_OBSERVE"
INTELLIGENT_LEVEL_TESTNET_REDUCE = "TESTNET_REDUCE"
INTELLIGENT_LEVEL_TESTNET_BLOCK = "TESTNET_BLOCK"
INTELLIGENT_LEVEL_REAL_OBSERVE = "REAL_OBSERVE"
INTELLIGENT_LEVEL_REAL_REDUCE = "REAL_REDUCE"
INTELLIGENT_LEVEL_REAL_BLOCK = "REAL_BLOCK"

INTELLIGENT_ACTIVATION_LEVELS = (
    INTELLIGENT_LEVEL_SHADOW_ONLY,
    INTELLIGENT_LEVEL_PAPER_ACTIVE,
    INTELLIGENT_LEVEL_TESTNET_OBSERVE,
    INTELLIGENT_LEVEL_TESTNET_REDUCE,
    INTELLIGENT_LEVEL_TESTNET_BLOCK,
    INTELLIGENT_LEVEL_REAL_OBSERVE,
    INTELLIGENT_LEVEL_REAL_REDUCE,
    INTELLIGENT_LEVEL_REAL_BLOCK,
)


DEFAULT_SETTINGS: dict[str, Any] = {
    "operation_mode": "SIMPLE",
    "architecture_mode": ARCHITECTURE_INTELLIGENT_FLOW,
    "intelligent_activation_level": "",
    "execution_mode": "DRY_RUN",
    "environment": "TESTNET",
    "testnet_api_key": "",
    "testnet_api_secret": "",
    "real_api_key": "",
    "real_api_secret": "",
    "symbol": "BTCUSDT",
    "symbols": "",
    "max_active_symbols": "1",
    "leverage": "5",
    "position_notional_usdt": "60",
    "stop_loss_pct": "0.01",
    "take_profit_pct": "0.02",
    "loop_interval_seconds": "3",
    "bot_order_prefix": "ALPHORA",
    "exit_model": "FIXED_PCT",
    "atr_period": "14",
    "atr_stop_multiplier": "1.5",
    "atr_take_multiplier": "2.0",
    "protection_policy": "AUTO_REPAIR",
    "protection_working_type": "MARK_PRICE",
    "recovery_action": "PROTECT",
    "strategy_module": "strategies.futures.fixed.random_timed_signal",
    "strategy_class": "RandomTimedSignalStrategy",
    "strategy_params": {},
    "strategy_selection_mode": "FIXED",
    "ai_probabilistic_enabled": False,
    "ai_global_model_path": "",
    "ai_operational_timeframe": "15m",
    "ai_operational_model_id": "",
    "ai_symbol_model_paths": {},
    "ai_use_symbol_refined_models": True,
    "ai_fallback_to_global_model": True,
    "ai_min_long_probability": "0.62",
    "ai_min_short_probability": "0.62",
    "ai_min_direction_gap": "0.18",
    "ai_min_expected_value_bps": "3.0",
    "ai_close_long_min_short_probability": "0.62",
    "ai_close_short_min_long_probability": "0.62",
    "ai_require_model_approved": True,
    "ai_training_symbol": "BTCUSDT",
    "ai_training_global_symbols": "BTCUSDT",
    "ai_training_refined_symbol": "BTCUSDT",
    "ai_training_interval": "15m",
    "ai_training_start": "2024-01-01",
    "ai_training_end": "2024-03-01",
    "ai_training_horizon_candles": str(DEFAULT_HORIZON_CANDLES),
    "ai_training_threshold_bps": f"{DEFAULT_THRESHOLD_BPS:g}",
    "ai_training_min_training_rows": str(DEFAULT_MIN_TRAINING_ROWS),
    "ai_training_csv_path": "",
    "ai_training_csv_map": "",
    "ai_training_global_output_dir": "models/ai/global",
    "ai_training_refined_output_dir": "models/ai/refined",
    "ai_training_download_if_missing": True,
    "backtest_interval": "1m",
    "backtest_csv_path": "",
    "backtest_symbol_scope": "CURRENT",
    "backtest_start": "2026-01-01",
    "backtest_end": "2026-01-31",
    "backtest_quantity_step": "0.0001",
    "backtest_min_quantity": "0.0001",
    "backtest_min_notional": "50",
    "backtest_price_tick": "0.10",
    "backtest_initial_balance": "1000",
    "maker_fee_pct": "0.0002",
    "taker_fee_pct": "0.0005",
    "slippage_bps": "2",
    "spread_bps": "1",
    "funding_enabled": True,
    "default_funding_rate": "0",
    "trade_audit_path": "data/trade_audit.jsonl",
    "trade_plan_shadow": True,
    "trade_planning_enabled": False,
    "cost_model_enabled": False,
    "dynamic_exit_enabled": False,
    "dynamic_sizing_enabled": False,
    "portfolio_allocator_enabled": False,
    "execution_planner_enabled": False,
    "adaptive_manager_enabled": False,
    "adaptive_execution_enabled": False,
    "learning_store_enabled": False,
    "learning_paper_activation_enabled": False,
    "learning_paper_reduction_enabled": False,
    "learning_paper_block_enabled": False,
    "learning_testnet_observation_enabled": False,
    "operational_module_modes_customized": False,
    "dynamic_exit_mode": "OFF",
    "smart_sizing_mode": "OFF",
    "smart_execution_mode": "OFF",
    "state_reconciliation_mode": "ALERT",
    "learning_advisory_mode": "SHADOW",
    "portfolio_allocator_mode": "OFF",
    "economic_gate_enabled": True,
    "min_net_gain_bps": "5",
    "min_net_reward_risk": "1.5",
    "max_round_trip_cost_bps": "20",
    "require_profitable_trade_plan": True,
    "economic_exit_adjustment_enabled": True,
    "max_economic_take_bps": "120",
    "max_economic_take_atr_multiplier": "3.0",
    "economic_exit_diagnostics_enabled": True,
    "entry_execution_style": "MARKET_SAFE",
    "limit_entry_enabled": False,
    "limit_entry_timeout_seconds": "3",
    "limit_entry_max_price_drift_bps": "5",
    "limit_entry_fallback_to_market": False,
    "limit_entry_post_only": True,
    "market_entry_require_economic_gate": True,
    "max_daily_loss_usdt": "0",
    "max_daily_trades": "0",
    "max_consecutive_losses": "0",
    "cooldown_after_loss_seconds": "0",
    "max_position_hold_seconds": "0",
    "max_exposure_usdt": "0",
    "stop_trading_after_daily_profit_usdt": "0",
    "user_data_cache_operational_preset": USER_DATA_CACHE_PRESET_DISABLED,
    "user_data_cache_effective_preset": USER_DATA_CACHE_PRESET_DISABLED,
    "user_data_cache_preset_degraded": False,
    "user_data_cache_preset_degrade_reason": "",
    "user_data_stream_enabled": False,
    "user_data_stream_shadow_only": True,
    "user_data_stream_keepalive_seconds": "1800",
    "user_data_stream_reconnect_seconds": "5",
    "user_data_stream_heartbeat_seconds": "60",
    "user_data_stream_max_pending_events": "500",
    "user_data_stream_cache_audit_enabled": False,
    "user_data_stream_cache_audit_max_age_seconds": "30",
    "user_data_stream_cache_usage_shadow_enabled": False,
    "user_data_stream_cache_usage_require_audit_match": True,
    "user_data_stream_cache_usage_max_age_seconds": "10",
    "user_data_stream_cache_usage_max_audit_age_seconds": "60",
    "user_data_stream_cache_assisted_plan_shadow_enabled": False,
    "user_data_stream_cache_position_enabled": False,
    "user_data_stream_cache_account_enabled": False,
    "user_data_stream_cache_open_orders_enabled": False,
    "user_data_stream_cache_stale_guard_enabled": False,
    "user_data_stream_cache_stale_guard_max_message_age_seconds": "30",
    "user_data_stream_cache_stale_guard_require_listen_key": True,
    "user_data_stream_cache_reconciliation_enabled": False,
}


DEFAULT_SYMBOL_PROFILE: dict[str, Any] = {
    "enabled": True,
    "symbol": "BTCUSDT",
    "strategy_params": {},
    "ai_use_symbol_refined_model": True,
    "ai_refined_model_path": "",
    "leverage": "5",
    "margin_type": "ISOLATED",
    "position_notional_usdt": "60",
    "stop_loss_pct": "0.01",
    "take_profit_pct": "0.02",
    "exit_model": "FIXED_PCT",
    "atr_period": "14",
    "atr_stop_multiplier": "1.5",
    "atr_take_multiplier": "2.0",
    "protection_policy": "AUTO_REPAIR",
    "protection_working_type": "MARK_PRICE",
    "recovery_action": "PROTECT",
    "max_daily_loss_usdt": "0",
    "max_daily_trades": "0",
    "max_consecutive_losses": "0",
    "cooldown_after_loss_seconds": "0",
    "max_position_hold_seconds": "0",
    "max_exposure_usdt": "0",
}


DEFAULT_PORTFOLIO_RISK_SETTINGS: dict[str, Any] = {
    "max_open_positions": "1",
    "max_total_exposure_usdt": "0",
    "max_long_exposure_usdt": "0",
    "max_short_exposure_usdt": "0",
    "max_symbol_exposure_usdt": "0",
    "max_group_exposure_usdt": "0",
    "max_daily_loss_usdt": "0",
    "stop_after_daily_profit_usdt": "0",
    "max_correlated_positions": "0",
    "allow_multiple_same_direction": True,
    "allow_multiple_positions_same_symbol": False,
    "max_new_positions_per_cycle": "1",
    "governor_max_symbols_per_cycle": "20",
}


class SettingsStore:
    def __init__(self, path: str | Path = SETTINGS_PATH):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return dict(DEFAULT_SETTINGS)

        try:
            with self.path.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Arquivo de configuração da interface corrompido ou inválido: {self.path}"
            ) from exc

        if not isinstance(loaded, dict):
            raise ValueError("Arquivo de configuracao da interface precisa conter um objeto JSON")

        settings = dict(DEFAULT_SETTINGS)
        settings.update(loaded)
        # Settings saved before the alphora namespace use the old module path.
        settings["strategy_module"] = qualify_module_path(settings.get("strategy_module", ""))
        return settings

    def save(self, settings: dict[str, Any]) -> None:
        payload = dict(DEFAULT_SETTINGS)
        payload.update(settings)
        _atomic_write_json(self.path, payload)
        # The file stores API credentials: keep it private to the user on POSIX.
        if os.name == "posix":
            self.path.chmod(0o600)

    @property
    def is_local_settings_path(self) -> bool:
        name = self.path.name
        return name == SETTINGS_PATH or name.endswith(".local.json")


class SymbolProfilesStore:
    def __init__(self, path: str | Path = SYMBOL_PROFILES_PATH):
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return [dict(DEFAULT_SYMBOL_PROFILE)]

        try:
            with self.path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Arquivo de perfis de símbolos corrompido ou inválido: {self.path}"
            ) from exc

        profiles = payload.get("symbol_profiles", []) if isinstance(payload, dict) else payload
        if not isinstance(profiles, list):
            raise ValueError("Arquivo de perfis multi-moedas precisa conter uma lista")

        result = []
        for profile in profiles:
            if not isinstance(profile, dict):
                raise ValueError("Cada perfil multi-moedas precisa ser objeto JSON")
            normalized = dict(DEFAULT_SYMBOL_PROFILE)
            normalized.update(profile)
            result.append(normalized)
        return result or [dict(DEFAULT_SYMBOL_PROFILE)]

    def save(self, profiles: list[dict[str, Any]]) -> None:
        from config.symbol_profile_schema import canonicalize_profiles_for_persistence

        canonical = canonicalize_profiles_for_persistence(profiles)
        _atomic_write_json(self.path, {"symbol_profiles": canonical})


class PortfolioRiskStore:
    def __init__(self, path: str | Path = PORTFOLIO_RISK_PATH):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return dict(DEFAULT_PORTFOLIO_RISK_SETTINGS)

        try:
            with self.path.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Arquivo de risco de portfolio corrompido ou inválido: {self.path}"
            ) from exc

        if not isinstance(loaded, dict):
            raise ValueError("Arquivo de risco de portfolio precisa conter objeto JSON")

        settings = dict(DEFAULT_PORTFOLIO_RISK_SETTINGS)
        settings.update(loaded)
        return settings

    def save(self, settings: dict[str, Any]) -> None:
        payload = dict(DEFAULT_PORTFOLIO_RISK_SETTINGS)
        payload.update(settings)
        _atomic_write_json(self.path, payload)


def normalize_environment(settings: dict[str, Any]) -> str:
    execution_mode = str(settings.get("execution_mode", "DRY_RUN")).upper()

    if execution_mode == "TESTNET_LIVE":
        return "TESTNET"

    if execution_mode == "REAL_LIVE":
        return "REAL"

    return str(settings.get("environment", "TESTNET")).upper()


def selected_api_credentials(settings: dict[str, Any]) -> tuple[str, str]:
    environment = normalize_environment(settings)

    if environment == "REAL":
        return (
            str(settings.get("real_api_key", "")).strip(),
            str(settings.get("real_api_secret", "")).strip(),
        )

    return (
        str(settings.get("testnet_api_key", "")).strip(),
        str(settings.get("testnet_api_secret", "")).strip(),
    )


def default_intelligent_activation_level(execution_mode: str) -> str:
    mode = str(execution_mode).upper()

    if mode == "PAPER_LIVE":
        return INTELLIGENT_LEVEL_PAPER_ACTIVE

    if mode == "TESTNET_LIVE":
        return INTELLIGENT_LEVEL_TESTNET_OBSERVE

    if mode == "REAL_LIVE":
        return INTELLIGENT_LEVEL_REAL_OBSERVE

    return INTELLIGENT_LEVEL_SHADOW_ONLY


def allowed_intelligent_activation_levels(execution_mode: str) -> tuple[str, ...]:
    mode = str(execution_mode).upper()

    if mode == "PAPER_LIVE":
        return (
            INTELLIGENT_LEVEL_SHADOW_ONLY,
            INTELLIGENT_LEVEL_PAPER_ACTIVE,
        )

    if mode == "TESTNET_LIVE":
        return (
            INTELLIGENT_LEVEL_SHADOW_ONLY,
            INTELLIGENT_LEVEL_TESTNET_OBSERVE,
            INTELLIGENT_LEVEL_TESTNET_REDUCE,
            INTELLIGENT_LEVEL_TESTNET_BLOCK,
        )

    if mode == "REAL_LIVE":
        return (
            INTELLIGENT_LEVEL_SHADOW_ONLY,
            INTELLIGENT_LEVEL_REAL_OBSERVE,
            INTELLIGENT_LEVEL_REAL_REDUCE,
            INTELLIGENT_LEVEL_REAL_BLOCK,
        )

    return (INTELLIGENT_LEVEL_SHADOW_ONLY,)


def normalize_intelligent_activation_level(
    *,
    level: str,
    execution_mode: str,
) -> str:
    normalized = str(level or "").upper()
    allowed = allowed_intelligent_activation_levels(execution_mode)

    if normalized in allowed:
        return normalized

    return default_intelligent_activation_level(execution_mode)


def operational_module_modes_for_settings(settings: dict[str, Any]) -> dict[str, str]:
    prepared = dict(settings)
    execution_mode = str(prepared.get("execution_mode", "DRY_RUN")).upper()
    activation_level = normalize_intelligent_activation_level(
        level=str(
            prepared.get(
                "intelligent_activation_level",
                default_intelligent_activation_level(execution_mode),
            )
        ),
        execution_mode=execution_mode,
    )
    customized = _as_bool(prepared.get("operational_module_modes_customized", False))
    if customized:
        resolution = module_modes_from_mapping(
            {
                module: prepared.get(attr, DEFAULT_SETTINGS.get(attr, "OFF"))
                for module, attr in MODULE_MODE_ATTRS.items()
            },
            source="DESKTOP",
        )
        return module_modes_to_values(resolution.modes)

    return module_modes_to_values(
        modes_for_intelligent_activation(
            execution_mode=execution_mode,
            activation_level=activation_level,
        )
    )


def apply_architecture_preset(settings: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(settings)
    execution_mode = str(prepared.get("execution_mode", "DRY_RUN")).upper()

    activation_level = normalize_intelligent_activation_level(
        level=str(
            prepared.get(
                "intelligent_activation_level",
                default_intelligent_activation_level(execution_mode),
            )
        ),
        execution_mode=execution_mode,
    )

    prepared["architecture_mode"] = ARCHITECTURE_INTELLIGENT_FLOW
    prepared["intelligent_activation_level"] = activation_level
    mode_values = operational_module_modes_for_settings(prepared)
    prepared.update({MODULE_MODE_ATTRS[module]: value for module, value in mode_values.items()})
    mode_resolution = module_modes_from_mapping(mode_values, source="DESKTOP")
    mode_flags = module_runtime_flags_from_modes(
        mode_resolution.modes,
        execution_mode=execution_mode,
    )

    # Always build the plan for auditing; only safe activation levels may apply it.
    prepared["trade_plan_shadow"] = True
    prepared["trade_planning_enabled"] = True
    prepared["cost_model_enabled"] = True
    prepared.update(mode_flags)
    return prepared


def settings_to_env(settings: dict[str, Any]) -> dict[str, str]:
    settings = apply_architecture_preset(settings)
    settings = apply_user_data_cache_preset(settings)
    execution_mode = str(settings.get("execution_mode", "DRY_RUN")).upper()
    environment = normalize_environment(settings)
    api_key, api_secret = selected_api_credentials(settings)

    env = {
        "ALPHORA_ARCHITECTURE_MODE": str(
            settings.get("architecture_mode", ARCHITECTURE_INTELLIGENT_FLOW)
        ),
        "ALPHORA_INTELLIGENT_ACTIVATION_LEVEL": str(
            settings.get("intelligent_activation_level", INTELLIGENT_LEVEL_SHADOW_ONLY)
        ),
        "ALPHORA_EXECUTION_MODE": execution_mode,
        "ALPHORA_ENVIRONMENT": environment,
        "ALPHORA_ALLOW_REAL_TRADING": "true" if execution_mode == "REAL_LIVE" else "false",
        "ALPHORA_ALLOW_REAL_USER_DATA_CACHE_ASSISTED": "false",
        "ALPHORA_USE_TESTNET": "true" if environment == "TESTNET" else "false",
        "ALPHORA_EXECUTE_ORDERS": "true"
        if execution_mode in {"TESTNET_LIVE", "REAL_LIVE"}
        else "false",
        "BINANCE_FUTURES_API_KEY": api_key,
        "BINANCE_FUTURES_API_SECRET": api_secret,
        "ALPHORA_SYMBOL": str(settings.get("symbol", "BTCUSDT")).upper(),
        "ALPHORA_SYMBOLS": str(settings.get("symbols", "")).upper(),
        "ALPHORA_MAX_ACTIVE_SYMBOLS": str(settings.get("max_active_symbols", "1")),
        "ALPHORA_LEVERAGE": str(settings.get("leverage", "5")),
        "ALPHORA_POSITION_NOTIONAL_USDT": str(settings.get("position_notional_usdt", "60")),
        "ALPHORA_STOP_LOSS_PCT": str(settings.get("stop_loss_pct", "0.01")),
        "ALPHORA_TAKE_PROFIT_PCT": str(settings.get("take_profit_pct", "0.02")),
        "ALPHORA_LOOP_INTERVAL_SECONDS": str(settings.get("loop_interval_seconds", "3")),
        "ALPHORA_BOT_ORDER_PREFIX": str(settings.get("bot_order_prefix", "ALPHORA")),
        "ALPHORA_EXIT_MODEL": str(settings.get("exit_model", "FIXED_PCT")),
        "ALPHORA_ATR_PERIOD": str(settings.get("atr_period", "14")),
        "ALPHORA_ATR_STOP_MULTIPLIER": str(settings.get("atr_stop_multiplier", "1.5")),
        "ALPHORA_ATR_TAKE_MULTIPLIER": str(settings.get("atr_take_multiplier", "2.0")),
        "ALPHORA_PROTECTION_POLICY": str(settings.get("protection_policy", "AUTO_REPAIR")),
        "ALPHORA_PROTECTION_WORKING_TYPE": str(
            settings.get("protection_working_type", "MARK_PRICE")
        ),
        "ALPHORA_RECOVERY_ACTION": str(settings.get("recovery_action", "PROTECT")),
        "ALPHORA_STRATEGY_MODULE": str(settings.get("strategy_module", "")),
        "ALPHORA_STRATEGY_CLASS": str(settings.get("strategy_class", "")),
        "ALPHORA_STRATEGY_PARAMS": json.dumps(
            _json_object(settings.get("strategy_params", {})),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "ALPHORA_TRADE_AUDIT_PATH": str(settings.get("trade_audit_path", "data/trade_audit.jsonl")),
        "ALPHORA_AI_PROBABILISTIC_ENABLED": _bool_env(
            settings.get("ai_probabilistic_enabled", False)
        ),
        "ALPHORA_AI_GLOBAL_MODEL_PATH": str(settings.get("ai_global_model_path", "")),
        "ALPHORA_AI_SYMBOL_MODEL_PATHS": json.dumps(
            _json_object(settings.get("ai_symbol_model_paths", {})),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "ALPHORA_AI_USE_SYMBOL_REFINED_MODELS": _bool_env(
            settings.get("ai_use_symbol_refined_models", True),
            default=True,
        ),
        "ALPHORA_AI_FALLBACK_TO_GLOBAL_MODEL": _bool_env(
            settings.get("ai_fallback_to_global_model", True),
            default=True,
        ),
        "ALPHORA_AI_MIN_LONG_PROBABILITY": str(settings.get("ai_min_long_probability", "0.62")),
        "ALPHORA_AI_MIN_SHORT_PROBABILITY": str(settings.get("ai_min_short_probability", "0.62")),
        "ALPHORA_AI_MIN_DIRECTION_GAP": str(settings.get("ai_min_direction_gap", "0.18")),
        "ALPHORA_AI_MIN_EXPECTED_VALUE_BPS": str(settings.get("ai_min_expected_value_bps", "3.0")),
        "ALPHORA_AI_CLOSE_LONG_MIN_SHORT_PROBABILITY": str(
            settings.get("ai_close_long_min_short_probability", "0.62")
        ),
        "ALPHORA_AI_CLOSE_SHORT_MIN_LONG_PROBABILITY": str(
            settings.get("ai_close_short_min_long_probability", "0.62")
        ),
        "ALPHORA_AI_REQUIRE_MODEL_APPROVED": _bool_env(
            settings.get("ai_require_model_approved", True)
        ),
        "ALPHORA_LAUNCHED_BY_GUI": "true",
        "ALPHORA_MAKER_FEE_PCT": str(settings.get("maker_fee_pct", "0.0002")),
        "ALPHORA_TAKER_FEE_PCT": str(settings.get("taker_fee_pct", "0.0005")),
        "ALPHORA_SLIPPAGE_BPS": str(settings.get("slippage_bps", "2")),
        "ALPHORA_ESTIMATED_SLIPPAGE_BPS": str(settings.get("slippage_bps", "2")),
        "ALPHORA_SPREAD_BPS": str(settings.get("spread_bps", "1")),
        "ALPHORA_ESTIMATED_SPREAD_BPS": str(settings.get("spread_bps", "1")),
        "ALPHORA_FUNDING_ENABLED": _bool_env(
            settings.get("funding_enabled", True),
            default=True,
        ),
        "ALPHORA_DEFAULT_FUNDING_RATE": str(settings.get("default_funding_rate", "0")),
        "ALPHORA_ESTIMATED_FUNDING_RATE": str(settings.get("default_funding_rate", "0")),
        "ALPHORA_TRADE_PLAN_SHADOW": _bool_env(
            settings.get("trade_plan_shadow", True),
            default=True,
        ),
        "ALPHORA_TRADE_PLANNING_ENABLED": _bool_env(settings.get("trade_planning_enabled", False)),
        "ALPHORA_COST_MODEL_ENABLED": _bool_env(settings.get("cost_model_enabled", False)),
        MODULE_ENV_KEYS[MODULE_DYNAMIC_EXIT]: str(settings.get("dynamic_exit_mode", "OFF")),
        MODULE_ENV_KEYS[MODULE_SMART_SIZING]: str(settings.get("smart_sizing_mode", "OFF")),
        MODULE_ENV_KEYS[MODULE_SMART_EXECUTION]: str(settings.get("smart_execution_mode", "OFF")),
        MODULE_ENV_KEYS[MODULE_STATE_RECONCILIATION]: str(
            settings.get("state_reconciliation_mode", "ALERT")
        ),
        MODULE_ENV_KEYS[MODULE_LEARNING_ADVISORY]: str(
            settings.get("learning_advisory_mode", "OFF")
        ),
        MODULE_ENV_KEYS[MODULE_PORTFOLIO_ALLOCATOR]: str(
            settings.get("portfolio_allocator_mode", "OFF")
        ),
        "ALPHORA_ECONOMIC_GATE_ENABLED": _bool_env(
            settings.get("economic_gate_enabled", True),
            default=True,
        ),
        "ALPHORA_MIN_NET_GAIN_BPS": str(settings.get("min_net_gain_bps", "5")),
        "ALPHORA_MIN_NET_REWARD_RISK": str(settings.get("min_net_reward_risk", "1.5")),
        "ALPHORA_MAX_ROUND_TRIP_COST_BPS": str(settings.get("max_round_trip_cost_bps", "20")),
        "ALPHORA_REQUIRE_PROFITABLE_TRADE_PLAN": _bool_env(
            settings.get("require_profitable_trade_plan", True),
            default=True,
        ),
        "ALPHORA_ECONOMIC_EXIT_ADJUSTMENT_ENABLED": _bool_env(
            settings.get("economic_exit_adjustment_enabled", True),
            default=True,
        ),
        "ALPHORA_MAX_ECONOMIC_TAKE_BPS": str(settings.get("max_economic_take_bps", "120")),
        "ALPHORA_MAX_ECONOMIC_TAKE_ATR_MULTIPLIER": str(
            settings.get("max_economic_take_atr_multiplier", "3.0")
        ),
        "ALPHORA_ECONOMIC_EXIT_DIAGNOSTICS_ENABLED": _bool_env(
            settings.get("economic_exit_diagnostics_enabled", True),
            default=True,
        ),
        "ALPHORA_ENTRY_EXECUTION_STYLE": str(settings.get("entry_execution_style", "MARKET_SAFE")),
        "ALPHORA_LIMIT_ENTRY_ENABLED": _bool_env(settings.get("limit_entry_enabled", False)),
        "ALPHORA_LIMIT_ENTRY_TIMEOUT_SECONDS": str(
            settings.get("limit_entry_timeout_seconds", "3")
        ),
        "ALPHORA_LIMIT_ENTRY_MAX_PRICE_DRIFT_BPS": str(
            settings.get("limit_entry_max_price_drift_bps", "5")
        ),
        "ALPHORA_LIMIT_ENTRY_FALLBACK_TO_MARKET": _bool_env(
            settings.get("limit_entry_fallback_to_market", False)
        ),
        "ALPHORA_LIMIT_ENTRY_POST_ONLY": _bool_env(
            settings.get("limit_entry_post_only", True),
            default=True,
        ),
        "ALPHORA_MARKET_ENTRY_REQUIRE_ECONOMIC_GATE": _bool_env(
            settings.get("market_entry_require_economic_gate", True),
            default=True,
        ),
        "ALPHORA_USER_DATA_STREAM_ENABLED": _bool_env(
            settings.get("user_data_stream_enabled", False)
        ),
        "ALPHORA_USER_DATA_STREAM_SHADOW_ONLY": _bool_env(
            settings.get("user_data_stream_shadow_only", True),
            default=True,
        ),
        "ALPHORA_USER_DATA_STREAM_KEEPALIVE_SECONDS": str(
            settings.get("user_data_stream_keepalive_seconds", "1800")
        ),
        "ALPHORA_USER_DATA_STREAM_RECONNECT_SECONDS": str(
            settings.get("user_data_stream_reconnect_seconds", "5")
        ),
        "ALPHORA_USER_DATA_STREAM_HEARTBEAT_SECONDS": str(
            settings.get("user_data_stream_heartbeat_seconds", "60")
        ),
        "ALPHORA_USER_DATA_STREAM_MAX_PENDING_EVENTS": str(
            settings.get("user_data_stream_max_pending_events", "500")
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_AUDIT_ENABLED": _bool_env(
            settings.get("user_data_stream_cache_audit_enabled", False)
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_AUDIT_MAX_AGE_SECONDS": str(
            settings.get("user_data_stream_cache_audit_max_age_seconds", "30")
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_USAGE_SHADOW_ENABLED": _bool_env(
            settings.get("user_data_stream_cache_usage_shadow_enabled", False)
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_USAGE_REQUIRE_AUDIT_MATCH": _bool_env(
            settings.get("user_data_stream_cache_usage_require_audit_match", True),
            default=True,
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_USAGE_MAX_AGE_SECONDS": str(
            settings.get("user_data_stream_cache_usage_max_age_seconds", "10")
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_USAGE_MAX_AUDIT_AGE_SECONDS": str(
            settings.get("user_data_stream_cache_usage_max_audit_age_seconds", "60")
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_ASSISTED_PLAN_SHADOW_ENABLED": _bool_env(
            settings.get("user_data_stream_cache_assisted_plan_shadow_enabled", False)
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_POSITION_ENABLED": _bool_env(
            settings.get("user_data_stream_cache_position_enabled", False)
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_ACCOUNT_ENABLED": _bool_env(
            settings.get("user_data_stream_cache_account_enabled", False)
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_OPEN_ORDERS_ENABLED": _bool_env(
            settings.get("user_data_stream_cache_open_orders_enabled", False)
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_STALE_GUARD_ENABLED": _bool_env(
            settings.get("user_data_stream_cache_stale_guard_enabled", False)
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_STALE_GUARD_MAX_MESSAGE_AGE_SECONDS": str(
            settings.get(
                "user_data_stream_cache_stale_guard_max_message_age_seconds",
                "30",
            )
        ),
        "ALPHORA_USER_DATA_STREAM_CACHE_STALE_GUARD_REQUIRE_LISTEN_KEY": _bool_env(
            settings.get("user_data_stream_cache_stale_guard_require_listen_key", True),
            default=True,
        ),
        "ALPHORA_USER_DATA_CACHE_OPERATIONAL_PRESET": str(
            settings.get("user_data_cache_operational_preset", USER_DATA_CACHE_PRESET_DISABLED)
        ),
        "ALPHORA_USER_DATA_CACHE_EFFECTIVE_PRESET": str(
            settings.get("user_data_cache_effective_preset", USER_DATA_CACHE_PRESET_DISABLED)
        ),
        "ALPHORA_BACKTEST_INITIAL_BALANCE": str(settings.get("backtest_initial_balance", "1000")),
        "ALPHORA_MAX_DAILY_LOSS_USDT": str(settings.get("max_daily_loss_usdt", "0")),
        "ALPHORA_MAX_DAILY_TRADES": str(settings.get("max_daily_trades", "0")),
        "ALPHORA_MAX_CONSECUTIVE_LOSSES": str(settings.get("max_consecutive_losses", "0")),
        "ALPHORA_COOLDOWN_AFTER_LOSS_SECONDS": str(
            settings.get("cooldown_after_loss_seconds", "0")
        ),
        "ALPHORA_MAX_POSITION_HOLD_SECONDS": str(settings.get("max_position_hold_seconds", "0")),
        "ALPHORA_MAX_EXPOSURE_USDT": str(settings.get("max_exposure_usdt", "0")),
        "ALPHORA_STOP_TRADING_AFTER_DAILY_PROFIT_USDT": str(
            settings.get("stop_trading_after_daily_profit_usdt", "0")
        ),
    }
    return env


def settings_to_governor_env(
    settings: dict[str, Any],
    *,
    symbol_profiles_path: str = SYMBOL_PROFILES_PATH,
    portfolio_risk_path: str = PORTFOLIO_RISK_PATH,
    multi_symbol: bool = True,
) -> dict[str, str]:
    env = settings_to_env(settings)
    env.update(
        {
            "ALPHORA_MULTI_SYMBOL_ENABLED": "true" if multi_symbol else "false",
            "ALPHORA_SYMBOL_PROFILES_PATH": symbol_profiles_path,
            "ALPHORA_PORTFOLIO_RISK_PATH": portfolio_risk_path,
        }
    )
    return env


def profile_from_simple_settings(settings: dict[str, Any]) -> dict[str, Any]:
    profile = dict(DEFAULT_SYMBOL_PROFILE)
    for key in profile:
        if key in settings:
            profile[key] = settings[key]
    profile["symbol"] = str(settings.get("symbol", profile["symbol"])).upper()
    return profile


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write via temp file + rename so the engine never reads a half-written file."""
    from config.symbol_profile_schema import atomic_write_json

    atomic_write_json(path, payload)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "sim"}:
        return True
    if normalized in {"false", "0", "no", "n", "nao"}:
        return False
    return default


def _bool_env(value: Any, default: bool = False) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"

    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "sim"}:
        return "true"

    if normalized in {"false", "0", "no", "n", "nao", "não"}:
        return "false"

    return "true" if default else "false"


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
