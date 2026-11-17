import json

from config.env_readers import _get_bool
from config.models import AppConfig


def build_effective_config_summary(config: AppConfig) -> dict:
    launched_by_gui = _get_bool("ALPHORA_LAUNCHED_BY_GUI", False)
    if config.multi_symbol_enabled or len(config.symbols) > 1:
        operation_mode = "GOVERNOR_MULTI_SYMBOL"
    else:
        operation_mode = "GOVERNOR_SINGLE_SYMBOL"
    return {
        "origin": "GUI" if launched_by_gui else "CLI/dev",
        "execution_mode": config.execution_mode.value,
        "environment": config.environment.value,
        "base_url": config.base_url,
        "env_file": config.env_file,
        "symbol": config.symbol,
        "operation_mode": operation_mode,
        "symbols": list(config.symbols if config.multi_symbol_enabled else (config.symbol,)),
        "strategy": f"{config.strategy_module}.{config.strategy_class}",
        "strategy_runtime_mode": config.strategy_runtime_mode,
        "strategy_params_enabled": bool(config.strategy_params),
        "ai_probabilistic_enabled": config.ai_probabilistic_enabled,
        "ai_global_model_path": config.ai_global_model_path,
        "runtime_dir": config.runtime_dir,
        "state_path": config.state_path,
        "trade_journal_path": config.trade_journal_path,
        "trade_audit_path": config.trade_audit_path,
        "audit_runs_dir": config.audit_runs_dir,
        "audit_jsonl_enabled": config.audit_jsonl_enabled,
        "audit_text_logs_enabled": config.audit_text_logs_enabled,
        "operational_module_modes": config.operational_module_modes,
        "operational_module_mode_effects": config.operational_module_mode_effects,
        "lock_enabled": config.lock_enabled,
        "mutations_enabled": config.mutations_enabled,
        "market_ws_enabled": config.market_ws_enabled,
        "market_ws_base_url": config.market_ws_base_url,
        "candle_hub_mode": config.candle_hub_mode,
        "candle_klines_url": config.candle_klines_url,
        "rest_weight": {
            "guard_mode": config.rest_weight_guard_mode,
            "guard_ratio": config.rest_weight_guard_ratio,
            "limit_1m": config.rest_weight_limit_1m,
        },
        "user_data_cache_operational_preset": config.user_data_cache_operational_preset,
        "user_data_cache_effective_preset": config.user_data_cache_effective_preset,
        "user_data_stream_enabled": config.user_data_stream_enabled,
        "user_data_stream_cache_position_enabled": (config.user_data_stream_cache_position_enabled),
        "user_data_stream_cache_account_enabled": (config.user_data_stream_cache_account_enabled),
        "user_data_stream_cache_open_orders_enabled": (
            config.user_data_stream_cache_open_orders_enabled
        ),
        "allow_real_user_data_cache_assisted": (config.allow_real_user_data_cache_assisted),
        "snapshot_provider": {
            "deadline_seconds": config.snapshot_deadline_seconds,
            "rest_timeout_seconds": config.snapshot_rest_timeout_seconds,
            "rest_transport_timeout_seconds": config.snapshot_rest_transport_timeout_seconds,
            "rest_connect_timeout_seconds": config.snapshot_rest_connect_timeout_seconds,
            "rest_read_timeout_seconds": config.snapshot_rest_read_timeout_seconds,
            "rest_max_attempts": config.snapshot_rest_max_attempts,
            "rest_retry_delay_seconds": config.snapshot_rest_retry_delay_seconds,
            "execution_max_age_seconds": config.snapshot_execution_max_age_seconds,
            "execution_max_skew_seconds": config.snapshot_execution_max_skew_seconds,
            "evaluation_max_age_seconds": config.snapshot_evaluation_max_age_seconds,
            "fallback_allowed": config.snapshot_fallback_allowed,
        },
    }


def format_effective_config_summary(summary: dict) -> str:
    lines = [
        "Effective Alphora configuration:",
        f"- origin: {summary['origin']}",
        f"- execution_mode: {summary['execution_mode']}",
        f"- environment: {summary['environment']}",
        f"- base_url: {summary['base_url']}",
        f"- env_file: {summary['env_file'] or '-'}",
        f"- main symbol: {summary['symbol']}",
        f"- operation mode: {summary['operation_mode']}",
        f"- effective symbols: {', '.join(summary['symbols'])}",
        f"- main strategy: {summary['strategy']}",
        f"- strategy_runtime_mode: {summary['strategy_runtime_mode']}",
        f"- strategy_params: {'present' if summary['strategy_params_enabled'] else 'empty'}",
        f"- ai_probabilistic_enabled: {summary['ai_probabilistic_enabled']}",
        f"- ai_global_model_path: {summary['ai_global_model_path'] or '-'}",
        f"- runtime_dir: {summary['runtime_dir']}",
        f"- state_path: {summary['state_path']}",
        f"- trade_journal_path: {summary['trade_journal_path']}",
        f"- audit_runs_dir: {summary['audit_runs_dir']}",
        f"- audit_jsonl_enabled: {summary['audit_jsonl_enabled']}",
        f"- audit_text_logs_enabled: {summary['audit_text_logs_enabled']}",
        "- operational_module_modes: "
        f"{json.dumps(summary['operational_module_modes'], sort_keys=True)}",
        f"- lock_enabled: {summary['lock_enabled']}",
        f"- mutations_enabled: {summary['mutations_enabled']}",
        f"- market_ws_enabled: {summary['market_ws_enabled']}",
        f"- market_ws_base_url: {summary['market_ws_base_url']}",
        f"- candle_hub_mode: {summary['candle_hub_mode']}",
        f"- candle_klines_url: {summary['candle_klines_url']}",
        f"- rest_weight: {json.dumps(summary['rest_weight'], sort_keys=True)}",
        "- user_data_cache_operational_preset: "
        f"{summary['user_data_cache_operational_preset'] or '-'}",
        f"- user_data_cache_effective_preset: {summary['user_data_cache_effective_preset'] or '-'}",
        f"- user_data_stream_enabled: {summary['user_data_stream_enabled']}",
        f"- user_data_cache_position_enabled: {summary['user_data_stream_cache_position_enabled']}",
        f"- user_data_cache_account_enabled: {summary['user_data_stream_cache_account_enabled']}",
        "- user_data_cache_open_orders_enabled: "
        f"{summary['user_data_stream_cache_open_orders_enabled']}",
        f"- allow_real_user_data_cache_assisted: {summary['allow_real_user_data_cache_assisted']}",
        f"- snapshot_provider: {json.dumps(summary['snapshot_provider'], sort_keys=True)}",
    ]
    return "\n".join(lines)
