"""``IntelligenceConfig`` group: architecture, activation level, module modes and probabilistic AI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from config.enums import ArchitectureMode, ExecutionMode, IntelligentActivationLevel
from config.env_readers import (
    _get_bool,
    _get_env,
    _get_json_dict,
    _get_non_negative_float,
    _get_non_negative_int,
    _get_probability,
)
from operational_module_modes import (
    MODULE_DYNAMIC_EXIT,
    MODULE_LEARNING_ADVISORY,
    MODULE_PORTFOLIO_ALLOCATOR,
    MODULE_SMART_EXECUTION,
    MODULE_SMART_SIZING,
    MODULE_STATE_RECONCILIATION,
    LearningAdvisoryMode,
    ModuleModeResolution,
    OperationalModuleMode,
    ReconciliationMode,
    SmartSizingMode,
    load_module_modes_from_env,
    module_runtime_flags_from_modes,
)

PROBABILISTIC_STRATEGY_MODULE = "strategies.futures.ai.probabilistic_strategy"
PROBABILISTIC_STRATEGY_CLASS = "ProbabilisticAIStrategy"


@dataclass(frozen=True)
class ProbabilisticAISettings:
    """``ALPHORA_AI_*`` as read; the strategy receives the symbol model map unnormalized."""

    enabled: bool
    global_model_path: str
    symbol_model_paths: dict[Any, Any]
    use_symbol_refined_models: bool
    fallback_to_global_model: bool
    min_long_probability: float
    min_short_probability: float
    min_direction_gap: float
    min_expected_value_bps: float
    close_long_min_short_probability: float
    close_short_min_long_probability: float
    require_model_approved: bool
    # 0 = hold for the model horizon (the label's holding time).
    min_hold_candles: int = 0
    close_on_probability_fade: bool = False

    def strategy_params(self) -> dict[str, Any]:
        return {
            "global_model_path": self.global_model_path,
            "symbol_model_paths": self.symbol_model_paths,
            "use_symbol_refined_models": self.use_symbol_refined_models,
            "fallback_to_global_model": self.fallback_to_global_model,
            "min_long_probability": self.min_long_probability,
            "min_short_probability": self.min_short_probability,
            "min_direction_gap": self.min_direction_gap,
            "min_expected_value_bps": self.min_expected_value_bps,
            "close_long_min_short_probability": self.close_long_min_short_probability,
            "close_short_min_long_probability": self.close_short_min_long_probability,
            "min_hold_candles": self.min_hold_candles or None,
            "close_on_probability_fade": self.close_on_probability_fade,
        }


def load_probabilistic_ai_settings() -> ProbabilisticAISettings:
    return ProbabilisticAISettings(
        enabled=_get_bool("ALPHORA_AI_PROBABILISTIC_ENABLED", False),
        global_model_path=_get_env("ALPHORA_AI_GLOBAL_MODEL_PATH", ""),
        symbol_model_paths=_get_json_dict("ALPHORA_AI_SYMBOL_MODEL_PATHS", {}),
        use_symbol_refined_models=_get_bool("ALPHORA_AI_USE_SYMBOL_REFINED_MODELS", True),
        fallback_to_global_model=_get_bool("ALPHORA_AI_FALLBACK_TO_GLOBAL_MODEL", True),
        min_long_probability=_get_probability("ALPHORA_AI_MIN_LONG_PROBABILITY", 0.62),
        min_short_probability=_get_probability("ALPHORA_AI_MIN_SHORT_PROBABILITY", 0.62),
        min_direction_gap=_get_probability("ALPHORA_AI_MIN_DIRECTION_GAP", 0.18),
        min_expected_value_bps=_get_non_negative_float("ALPHORA_AI_MIN_EXPECTED_VALUE_BPS", 3.0),
        close_long_min_short_probability=_get_probability(
            "ALPHORA_AI_CLOSE_LONG_MIN_SHORT_PROBABILITY",
            0.62,
        ),
        close_short_min_long_probability=_get_probability(
            "ALPHORA_AI_CLOSE_SHORT_MIN_LONG_PROBABILITY",
            0.62,
        ),
        require_model_approved=_get_bool("ALPHORA_AI_REQUIRE_MODEL_APPROVED", True),
        min_hold_candles=_get_non_negative_int("ALPHORA_AI_MIN_HOLD_CANDLES", 0),
        close_on_probability_fade=_get_bool(
            "ALPHORA_AI_CLOSE_ON_PROBABILITY_FADE",
            False,
        ),
    )


def load_module_modes(execution_mode: ExecutionMode) -> ModuleModeResolution:
    return load_module_modes_from_env(execution_mode=execution_mode.value)


class IntelligenceFields(TypedDict):
    architecture_mode: ArchitectureMode
    intelligent_activation_level: IntelligentActivationLevel
    trade_plan_shadow: bool
    trade_planning_enabled: bool
    dynamic_exit_enabled: bool
    dynamic_sizing_enabled: bool
    portfolio_allocator_enabled: bool
    execution_planner_enabled: bool
    adaptive_manager_enabled: bool
    adaptive_execution_enabled: bool
    learning_store_enabled: bool
    learning_paper_activation_enabled: bool
    learning_paper_reduction_enabled: bool
    learning_paper_block_enabled: bool
    learning_testnet_observation_enabled: bool
    dynamic_exit_mode: OperationalModuleMode | str | None
    smart_sizing_mode: SmartSizingMode | str | None
    smart_execution_mode: OperationalModuleMode | str | None
    state_reconciliation_mode: ReconciliationMode | str | None
    learning_advisory_mode: LearningAdvisoryMode | str | None
    portfolio_allocator_mode: OperationalModuleMode | str | None
    operational_module_mode_sources: dict[str, str]
    ai_probabilistic_enabled: bool
    ai_global_model_path: str
    ai_symbol_model_paths: dict[str, str]
    ai_use_symbol_refined_models: bool
    ai_fallback_to_global_model: bool
    ai_min_long_probability: float
    ai_min_short_probability: float
    ai_min_direction_gap: float
    ai_min_expected_value_bps: float
    ai_close_long_min_short_probability: float
    ai_close_short_min_long_probability: float
    ai_require_model_approved: bool
    ai_min_hold_candles: int
    ai_close_on_probability_fade: bool


def load_intelligence_fields(
    *,
    execution_mode: ExecutionMode,
    modules: ModuleModeResolution,
    ai: ProbabilisticAISettings,
) -> IntelligenceFields:
    flags = module_runtime_flags_from_modes(modules.modes, execution_mode=execution_mode.value)
    modes = modules.modes
    return {
        "architecture_mode": load_architecture_mode(),
        "intelligent_activation_level": load_intelligent_activation_level(
            execution_mode=execution_mode,
        ),
        "trade_plan_shadow": _get_bool("ALPHORA_TRADE_PLAN_SHADOW", True),
        "trade_planning_enabled": _get_bool("ALPHORA_TRADE_PLANNING_ENABLED", False),
        "dynamic_exit_enabled": flags["dynamic_exit_enabled"],
        "dynamic_sizing_enabled": flags["dynamic_sizing_enabled"],
        "portfolio_allocator_enabled": flags["portfolio_allocator_enabled"],
        "execution_planner_enabled": flags["execution_planner_enabled"],
        "adaptive_manager_enabled": flags["adaptive_manager_enabled"],
        "adaptive_execution_enabled": flags["adaptive_execution_enabled"],
        "learning_store_enabled": flags["learning_store_enabled"],
        "learning_paper_activation_enabled": flags["learning_paper_activation_enabled"],
        "learning_paper_reduction_enabled": flags["learning_paper_reduction_enabled"],
        "learning_paper_block_enabled": flags["learning_paper_block_enabled"],
        "learning_testnet_observation_enabled": flags["learning_testnet_observation_enabled"],
        "dynamic_exit_mode": modes[MODULE_DYNAMIC_EXIT],
        "smart_sizing_mode": modes[MODULE_SMART_SIZING],
        "smart_execution_mode": modes[MODULE_SMART_EXECUTION],
        "state_reconciliation_mode": modes[MODULE_STATE_RECONCILIATION],
        "learning_advisory_mode": modes[MODULE_LEARNING_ADVISORY],
        "portfolio_allocator_mode": modes[MODULE_PORTFOLIO_ALLOCATOR],
        "operational_module_mode_sources": modules.sources,
        "ai_probabilistic_enabled": ai.enabled,
        "ai_global_model_path": ai.global_model_path,
        "ai_symbol_model_paths": {
            str(symbol).upper().strip(): str(path).strip()
            for symbol, path in ai.symbol_model_paths.items()
            if str(symbol).strip() and str(path).strip()
        },
        "ai_use_symbol_refined_models": ai.use_symbol_refined_models,
        "ai_fallback_to_global_model": ai.fallback_to_global_model,
        "ai_min_long_probability": ai.min_long_probability,
        "ai_min_short_probability": ai.min_short_probability,
        "ai_min_direction_gap": ai.min_direction_gap,
        "ai_min_expected_value_bps": ai.min_expected_value_bps,
        "ai_close_long_min_short_probability": ai.close_long_min_short_probability,
        "ai_close_short_min_long_probability": ai.close_short_min_long_probability,
        "ai_require_model_approved": ai.require_model_approved,
        "ai_min_hold_candles": ai.min_hold_candles,
        "ai_close_on_probability_fade": ai.close_on_probability_fade,
    }


def load_architecture_mode() -> ArchitectureMode:
    raw_mode = _get_env(
        "ALPHORA_ARCHITECTURE_MODE",
        ArchitectureMode.INTELLIGENT_FLOW.value,
    ).upper()

    try:
        return ArchitectureMode(raw_mode)
    except ValueError:
        allowed = ", ".join(mode.value for mode in ArchitectureMode)
        raise ValueError(
            f"ALPHORA_ARCHITECTURE_MODE invalido: {raw_mode}. Valores aceitos: {allowed}."
        ) from None


def load_intelligent_activation_level(
    *,
    execution_mode: ExecutionMode,
) -> IntelligentActivationLevel:
    raw_level = _get_env("ALPHORA_INTELLIGENT_ACTIVATION_LEVEL", "").upper()

    if raw_level:
        try:
            return IntelligentActivationLevel(raw_level)
        except ValueError:
            allowed = ", ".join(level.value for level in IntelligentActivationLevel)
            raise ValueError(
                f"ALPHORA_INTELLIGENT_ACTIVATION_LEVEL invalido: {raw_level}. "
                f"Valores aceitos: {allowed}."
            ) from None

    if execution_mode is ExecutionMode.PAPER_LIVE:
        return IntelligentActivationLevel.PAPER_ACTIVE

    if execution_mode is ExecutionMode.TESTNET_LIVE:
        return IntelligentActivationLevel.TESTNET_OBSERVE

    if execution_mode is ExecutionMode.REAL_LIVE:
        return IntelligentActivationLevel.REAL_OBSERVE

    return IntelligentActivationLevel.SHADOW_ONLY


__all__ = [
    "PROBABILISTIC_STRATEGY_CLASS",
    "PROBABILISTIC_STRATEGY_MODULE",
    "IntelligenceFields",
    "ProbabilisticAISettings",
    "load_architecture_mode",
    "load_intelligence_fields",
    "load_intelligent_activation_level",
    "load_module_modes",
    "load_probabilistic_ai_settings",
]
