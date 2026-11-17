"""``RiskConfig`` group: daily guards, defensive mode, economic gate, costs and smart sizing."""

from __future__ import annotations

from typing import TypedDict

from config.env_readers import (
    _get_bool,
    _get_env,
    _get_finite_float_alias,
    _get_non_negative_float,
    _get_non_negative_float_alias,
    _get_non_negative_int,
)
from config.smart_settings import SmartSizingMinNotionalPolicy, SmartSizingSettings
from trading_cadence import DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES


class RiskGuardFields(TypedDict):
    max_daily_loss_usdt: float
    max_daily_trades: int
    max_consecutive_losses_guard: int
    cooldown_after_loss_seconds: float
    max_position_hold_seconds: float
    max_exposure_usdt: float
    min_seconds_between_entries: float
    stop_trading_after_daily_profit_usdt: float
    governor_defensive_mode_enabled: bool
    governor_defensive_mode_shadow_only: bool
    governor_defensive_mode_min_shock_symbols: int
    governor_defensive_mode_max_stale_symbols: int
    governor_defensive_mode_max_price_age_seconds: float
    governor_defensive_mode_budget_pressure_enabled: bool
    governor_defensive_mode_enforcement_enabled: bool
    governor_defensive_mode_block_new_entries: bool


def load_risk_guard_fields() -> RiskGuardFields:
    return {
        "max_daily_loss_usdt": _get_non_negative_float("ALPHORA_MAX_DAILY_LOSS_USDT", 0.0),
        "max_daily_trades": _get_non_negative_int("ALPHORA_MAX_DAILY_TRADES", 0),
        "max_consecutive_losses_guard": _get_non_negative_int(
            "ALPHORA_MAX_CONSECUTIVE_LOSSES",
            0,
        ),
        "cooldown_after_loss_seconds": _get_non_negative_float(
            "ALPHORA_COOLDOWN_AFTER_LOSS_SECONDS",
            0.0,
        ),
        "max_position_hold_seconds": _get_non_negative_float(
            "ALPHORA_MAX_POSITION_HOLD_SECONDS",
            0.0,
        ),
        "max_exposure_usdt": _get_non_negative_float("ALPHORA_MAX_EXPOSURE_USDT", 0.0),
        "min_seconds_between_entries": _get_non_negative_float(
            "ALPHORA_MIN_SECONDS_BETWEEN_ENTRIES",
            DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES,
        ),
        "stop_trading_after_daily_profit_usdt": _get_non_negative_float(
            "ALPHORA_STOP_TRADING_AFTER_DAILY_PROFIT_USDT",
            0.0,
        ),
        "governor_defensive_mode_enabled": _get_bool(
            "ALPHORA_GOVERNOR_DEFENSIVE_MODE_ENABLED",
            True,
        ),
        "governor_defensive_mode_shadow_only": _get_bool(
            "ALPHORA_GOVERNOR_DEFENSIVE_MODE_SHADOW_ONLY",
            True,
        ),
        "governor_defensive_mode_min_shock_symbols": _get_non_negative_int(
            "ALPHORA_GOVERNOR_DEFENSIVE_MODE_MIN_SHOCK_SYMBOLS",
            2,
        ),
        "governor_defensive_mode_max_stale_symbols": _get_non_negative_int(
            "ALPHORA_GOVERNOR_DEFENSIVE_MODE_MAX_STALE_SYMBOLS",
            0,
        ),
        "governor_defensive_mode_max_price_age_seconds": _get_non_negative_float(
            "ALPHORA_GOVERNOR_DEFENSIVE_MODE_MAX_PRICE_AGE_SECONDS",
            5.0,
        ),
        "governor_defensive_mode_budget_pressure_enabled": _get_bool(
            "ALPHORA_GOVERNOR_DEFENSIVE_MODE_BUDGET_PRESSURE_ENABLED",
            True,
        ),
        "governor_defensive_mode_enforcement_enabled": _get_bool(
            "ALPHORA_GOVERNOR_DEFENSIVE_MODE_ENFORCEMENT_ENABLED",
            False,
        ),
        "governor_defensive_mode_block_new_entries": _get_bool(
            "ALPHORA_GOVERNOR_DEFENSIVE_MODE_BLOCK_NEW_ENTRIES",
            True,
        ),
    }


class EconomicFields(TypedDict):
    cost_model_enabled: bool
    economic_gate_enabled: bool
    min_net_gain_bps: float
    min_net_reward_risk: float
    max_round_trip_cost_bps: float
    require_profitable_trade_plan: bool
    economic_exit_adjustment_enabled: bool
    max_economic_take_bps: float
    max_economic_take_atr_multiplier: float
    economic_exit_diagnostics_enabled: bool
    maker_fee_pct: float
    taker_fee_pct: float
    estimated_spread_bps: float
    estimated_slippage_bps: float
    funding_enabled: bool
    estimated_funding_rate: float
    smart_sizing_settings: SmartSizingSettings


def load_economic_fields() -> EconomicFields:
    return {
        "cost_model_enabled": _get_bool("ALPHORA_COST_MODEL_ENABLED", False),
        "economic_gate_enabled": _get_bool("ALPHORA_ECONOMIC_GATE_ENABLED", True),
        "min_net_gain_bps": _get_non_negative_float("ALPHORA_MIN_NET_GAIN_BPS", 5.0),
        "min_net_reward_risk": _get_non_negative_float("ALPHORA_MIN_NET_REWARD_RISK", 1.5),
        "max_round_trip_cost_bps": _get_non_negative_float(
            "ALPHORA_MAX_ROUND_TRIP_COST_BPS",
            20.0,
        ),
        "require_profitable_trade_plan": _get_bool(
            "ALPHORA_REQUIRE_PROFITABLE_TRADE_PLAN",
            True,
        ),
        "economic_exit_adjustment_enabled": _get_bool(
            "ALPHORA_ECONOMIC_EXIT_ADJUSTMENT_ENABLED",
            True,
        ),
        "max_economic_take_bps": _get_non_negative_float("ALPHORA_MAX_ECONOMIC_TAKE_BPS", 120.0),
        "max_economic_take_atr_multiplier": _get_non_negative_float(
            "ALPHORA_MAX_ECONOMIC_TAKE_ATR_MULTIPLIER",
            3.0,
        ),
        "economic_exit_diagnostics_enabled": _get_bool(
            "ALPHORA_ECONOMIC_EXIT_DIAGNOSTICS_ENABLED",
            True,
        ),
        "maker_fee_pct": _get_non_negative_float("ALPHORA_MAKER_FEE_PCT", 0.0002),
        "taker_fee_pct": _get_non_negative_float("ALPHORA_TAKER_FEE_PCT", 0.0004),
        "estimated_spread_bps": _get_non_negative_float_alias(
            "ALPHORA_ESTIMATED_SPREAD_BPS",
            "ALPHORA_SPREAD_BPS",
            2.0,
        ),
        "estimated_slippage_bps": _get_non_negative_float_alias(
            "ALPHORA_ESTIMATED_SLIPPAGE_BPS",
            "ALPHORA_SLIPPAGE_BPS",
            3.0,
        ),
        "funding_enabled": _get_bool("ALPHORA_FUNDING_ENABLED", True),
        "estimated_funding_rate": _get_finite_float_alias(
            "ALPHORA_ESTIMATED_FUNDING_RATE",
            "ALPHORA_DEFAULT_FUNDING_RATE",
            0.0,
        ),
        "smart_sizing_settings": load_smart_sizing_settings(),
    }


def load_smart_sizing_settings() -> SmartSizingSettings:
    defaults = SmartSizingSettings()
    return SmartSizingSettings(
        low_reward_risk=_get_non_negative_float(
            "ALPHORA_SMART_SIZING_LOW_REWARD_RISK",
            float(defaults.low_reward_risk),
        ),
        medium_reward_risk=_get_non_negative_float(
            "ALPHORA_SMART_SIZING_MEDIUM_REWARD_RISK",
            float(defaults.medium_reward_risk),
        ),
        elevated_cost_to_reward_ratio=_get_non_negative_float(
            "ALPHORA_SMART_SIZING_ELEVATED_COST_TO_REWARD_RATIO",
            float(defaults.elevated_cost_to_reward_ratio),
        ),
        high_cost_to_reward_ratio=_get_non_negative_float(
            "ALPHORA_SMART_SIZING_HIGH_COST_TO_REWARD_RATIO",
            float(defaults.high_cost_to_reward_ratio),
        ),
        min_notional_policy=SmartSizingMinNotionalPolicy.normalize(
            _get_env(
                "ALPHORA_SMART_SIZING_MIN_NOTIONAL_POLICY",
                defaults.min_notional_policy.value,  # type: ignore[union-attr,attr-defined]
            )
        ),
    )


__all__ = [
    "EconomicFields",
    "RiskGuardFields",
    "load_economic_fields",
    "load_risk_guard_fields",
    "load_smart_sizing_settings",
]
