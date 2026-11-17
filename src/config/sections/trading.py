"""``TradingConfig`` group: symbols, strategy, trade parameters, exit model and trend context."""

from __future__ import annotations

from typing import Any, TypedDict

from config.enums import ExitModelType, MarginType
from config.env_readers import (
    _get_bool,
    _get_env,
    _get_float,
    _get_int,
    _get_json_dict,
    _get_non_negative_float,
    _get_non_negative_int,
)
from config.sections.intelligence import (
    PROBABILISTIC_STRATEGY_CLASS,
    PROBABILISTIC_STRATEGY_MODULE,
    ProbabilisticAISettings,
)
from config.validators import _validate_max_active_symbols, parse_symbols
from module_paths import qualify_module_path

DEFAULT_STRATEGY_MODULE = "strategies.futures.fixed.random_timed_signal"
DEFAULT_STRATEGY_CLASS = "RandomTimedSignalStrategy"


class SymbolFields(TypedDict):
    symbol: str
    symbols: tuple[str, ...]
    max_active_symbols: int


def load_symbol_fields() -> SymbolFields:
    configured_symbol = _get_env("ALPHORA_SYMBOL", "BTCUSDT").upper()
    max_active_symbols = _get_int("ALPHORA_MAX_ACTIVE_SYMBOLS", 1)
    # Must run before slicing: 0 or a negative limit would empty the tuple and
    # surface as IndexError instead of a named configuration error.
    _validate_max_active_symbols(max_active_symbols)
    symbols = parse_symbols(_get_env("ALPHORA_SYMBOLS", ""), fallback=configured_symbol)
    symbols = symbols[:max_active_symbols]
    return {"symbol": symbols[0], "symbols": symbols, "max_active_symbols": max_active_symbols}


class StrategyFields(TypedDict):
    strategy_module: str
    strategy_class: str
    strategy_params: dict[str, Any]
    strategy_runtime_mode: str


def load_strategy_fields(ai: ProbabilisticAISettings) -> StrategyFields:
    """The configured strategy, replaced by the probabilistic AI strategy when AI is enabled."""

    module = qualify_module_path(_get_env("ALPHORA_STRATEGY_MODULE", DEFAULT_STRATEGY_MODULE))
    strategy_class = _get_env("ALPHORA_STRATEGY_CLASS", DEFAULT_STRATEGY_CLASS)
    params = _get_json_dict("ALPHORA_STRATEGY_PARAMS", {})
    if not ai.enabled:
        return {
            "strategy_module": module,
            "strategy_class": strategy_class,
            "strategy_params": params,
            "strategy_runtime_mode": "FIXED",
        }
    return {
        "strategy_module": PROBABILISTIC_STRATEGY_MODULE,
        "strategy_class": PROBABILISTIC_STRATEGY_CLASS,
        "strategy_params": {**params, **ai.strategy_params()},
        "strategy_runtime_mode": "PROBABILISTIC_AI",
    }


class TradeParameterFields(TypedDict):
    margin_type: MarginType
    leverage: int
    position_notional_usdt: float
    stop_loss_pct: float
    take_profit_pct: float
    exit_model: ExitModelType
    atr_period: int
    atr_stop_multiplier: float
    atr_take_multiplier: float
    multi_symbol_enabled: bool
    symbol_profiles_path: str
    portfolio_risk_path: str
    trend_context_enabled: bool
    trend_context_mode: str
    trend_fast_ma_period: int
    trend_slow_ma_period: int
    trend_slope_lookback: int
    trend_volatility_lookback: int
    trend_high_volatility_threshold_pct: float
    trend_chop_threshold: float
    trend_timeframe: str


def load_trade_parameter_fields() -> TradeParameterFields:
    return {
        "margin_type": MarginType(_get_env("ALPHORA_MARGIN_TYPE", "ISOLATED").upper()),
        "leverage": _get_int("ALPHORA_LEVERAGE", 1),
        "position_notional_usdt": _get_float("ALPHORA_POSITION_NOTIONAL_USDT", 20.0),
        "stop_loss_pct": _get_float("ALPHORA_STOP_LOSS_PCT", 0.01),
        "take_profit_pct": _get_float("ALPHORA_TAKE_PROFIT_PCT", 0.02),
        "exit_model": ExitModelType(_get_env("ALPHORA_EXIT_MODEL", "FIXED_PCT").upper()),
        "atr_period": _get_int("ALPHORA_ATR_PERIOD", 14),
        "atr_stop_multiplier": _get_float("ALPHORA_ATR_STOP_MULTIPLIER", 1.5),
        "atr_take_multiplier": _get_float("ALPHORA_ATR_TAKE_MULTIPLIER", 2.0),
        "multi_symbol_enabled": _get_bool("ALPHORA_MULTI_SYMBOL_ENABLED", False),
        "symbol_profiles_path": _get_env(
            "ALPHORA_SYMBOL_PROFILES_PATH",
            "config/alphora_symbols.local.json",
        ),
        "portfolio_risk_path": _get_env(
            "ALPHORA_PORTFOLIO_RISK_PATH",
            "config/alphora_portfolio.local.json",
        ),
        "trend_context_enabled": _get_bool("ALPHORA_TREND_CONTEXT_ENABLED", True),
        "trend_context_mode": _get_env(
            "ALPHORA_TREND_CONTEXT_MODE",
            "BLOCK_STRONG_COUNTER_TREND",
        ).upper(),
        "trend_fast_ma_period": _get_non_negative_int("ALPHORA_TREND_FAST_MA_PERIOD", 20),
        "trend_slow_ma_period": _get_non_negative_int("ALPHORA_TREND_SLOW_MA_PERIOD", 50),
        "trend_slope_lookback": _get_non_negative_int("ALPHORA_TREND_SLOPE_LOOKBACK", 5),
        "trend_volatility_lookback": _get_non_negative_int(
            "ALPHORA_TREND_VOLATILITY_LOOKBACK",
            20,
        ),
        "trend_high_volatility_threshold_pct": _get_non_negative_float(
            "ALPHORA_TREND_HIGH_VOLATILITY_THRESHOLD_PCT",
            3.0,
        ),
        "trend_chop_threshold": _get_non_negative_float("ALPHORA_TREND_CHOP_THRESHOLD", 0.6),
        "trend_timeframe": _get_env("ALPHORA_TREND_TIMEFRAME", ""),
    }


__all__ = [
    "DEFAULT_STRATEGY_CLASS",
    "DEFAULT_STRATEGY_MODULE",
    "StrategyFields",
    "SymbolFields",
    "TradeParameterFields",
    "load_strategy_fields",
    "load_symbol_fields",
    "load_trade_parameter_fields",
]
