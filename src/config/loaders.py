"""``load_config``: compose the ``AppConfig`` from the section loaders (Plano Diretor §22).

Every section in ``config.sections`` reads the variables of one group and returns its
``AppConfig`` fields; this module only orders the sections that depend on each other, applies the
environment overrides and validates the result.
"""

from dataclasses import replace

from config.enums import ExchangeEnvironment, ProtectionWorkingType
from config.env_file import load_env_file
from config.models import AppConfig
from config.rest_weight_env import load_rest_weight_settings
from config.sections.execution import load_execution_fields
from config.sections.intelligence import (
    load_intelligence_fields,
    load_module_modes,
    load_probabilistic_ai_settings,
)
from config.sections.market_data import (
    load_market_stream_fields,
    load_user_data_cache_fields,
    load_user_data_stream_fields,
)
from config.sections.observability import load_observability_fields
from config.sections.risk import load_economic_fields, load_risk_guard_fields
from config.sections.runtime import load_runtime_path_fields, load_scheduling_fields
from config.sections.trading import (
    load_strategy_fields,
    load_symbol_fields,
    load_trade_parameter_fields,
)
from config.snapshot_env import load_snapshot_settings
from config.validators import _validate_config
from config.ws_endpoints import resolve_public_market_ws_base_url


def load_config() -> AppConfig:
    env_file = load_env_file()

    execution = load_execution_fields()
    execution_mode = execution["execution_mode"]
    modules = load_module_modes(execution_mode)
    ai = load_probabilistic_ai_settings()
    symbols = load_symbol_fields()

    config = AppConfig(
        **execution,
        **load_intelligence_fields(execution_mode=execution_mode, modules=modules, ai=ai),
        **load_market_stream_fields(),
        **load_user_data_stream_fields(),
        **load_user_data_cache_fields(
            execution_mode=execution_mode,
            allow_real_user_data_cache_assisted=execution["allow_real_user_data_cache_assisted"],
            modules=modules,
        ),
        **load_snapshot_settings(),
        **load_rest_weight_settings(),
        **symbols,
        **load_strategy_fields(ai),
        **load_trade_parameter_fields(),
        **load_risk_guard_fields(),
        **load_economic_fields(),
        **load_runtime_path_fields(
            symbols=symbols["symbols"],
            environment=execution["environment"],
            execution_mode=execution_mode,
        ),
        **load_scheduling_fields(),
        **load_observability_fields(),
        env_file=env_file,
    )
    config = _apply_environment_overrides(config)
    _validate_config(config)
    return config


def _apply_environment_overrides(config: AppConfig) -> AppConfig:
    """Resolve the public stream URL and widen the tolerances the demo exchange needs."""

    config = replace(config, market_ws_base_url=resolve_public_market_ws_base_url(config))
    if config.environment is not ExchangeEnvironment.TESTNET:
        return config
    return replace(
        config,
        protection_working_type=ProtectionWorkingType.CONTRACT_PRICE,
        market_ws_max_price_age_seconds=max(config.market_ws_max_price_age_seconds, 15.0),
        limit_entry_max_price_drift_bps=max(config.limit_entry_max_price_drift_bps, 50.0),
        pre_dispatch_soft_price_drift_bps=max(config.pre_dispatch_soft_price_drift_bps, 15.0),
        pre_dispatch_hard_price_drift_bps=max(config.pre_dispatch_hard_price_drift_bps, 50.0),
    )
