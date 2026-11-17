from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

MODULE_DYNAMIC_EXIT = "dynamic_exit"
MODULE_SMART_SIZING = "smart_sizing"
MODULE_SMART_EXECUTION = "smart_execution"
MODULE_STATE_RECONCILIATION = "state_reconciliation"
MODULE_LEARNING_ADVISORY = "learning_advisory"
MODULE_PORTFOLIO_ALLOCATOR = "portfolio_allocator"

OPERATIONAL_MODULES: tuple[str, ...] = (
    MODULE_DYNAMIC_EXIT,
    MODULE_SMART_SIZING,
    MODULE_SMART_EXECUTION,
    MODULE_STATE_RECONCILIATION,
    MODULE_LEARNING_ADVISORY,
    MODULE_PORTFOLIO_ALLOCATOR,
)


class ModuleModeSource(str, Enum):
    ENV = "ENV"
    DEFAULT = "DEFAULT"
    DESKTOP = "DESKTOP"


class _ModuleModeBehavior:
    value: str  # provided by enum subclasses

    @property
    def is_enabled(self) -> bool:
        return self.value != "OFF"

    @property
    def is_observe_only(self) -> bool:
        return self.value == "OBSERVE"

    @property
    def is_shadow(self) -> bool:
        return self.value == "SHADOW"

    @property
    def can_affect_execution(self) -> bool:
        return self.value == "ACTIVE"

    @property
    def can_reduce_risk(self) -> bool:
        return False

    @property
    def can_block(self) -> bool:
        return False

    @property
    def can_repair(self) -> bool:
        return False

    @property
    def affected_result(self) -> bool:
        return self.can_affect_execution


class OperationalModuleMode(_ModuleModeBehavior, str, Enum):
    OFF = "OFF"
    OBSERVE = "OBSERVE"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


class SmartSizingMode(_ModuleModeBehavior, str, Enum):
    OFF = "OFF"
    OBSERVE = "OBSERVE"
    SHADOW = "SHADOW"
    REDUCE_ONLY = "REDUCE_ONLY"
    ACTIVE = "ACTIVE"

    @property
    def can_affect_execution(self) -> bool:
        return self in {SmartSizingMode.REDUCE_ONLY, SmartSizingMode.ACTIVE}

    @property
    def can_reduce_risk(self) -> bool:
        return self in {SmartSizingMode.REDUCE_ONLY, SmartSizingMode.ACTIVE}


class ReconciliationMode(_ModuleModeBehavior, str, Enum):
    OFF = "OFF"
    OBSERVE = "OBSERVE"
    ALERT = "ALERT"
    BLOCK = "BLOCK"
    REPAIR_SAFE = "REPAIR_SAFE"

    @property
    def is_observe_only(self) -> bool:
        return self in {ReconciliationMode.OBSERVE, ReconciliationMode.ALERT}

    @property
    def can_affect_execution(self) -> bool:
        return self in {ReconciliationMode.BLOCK, ReconciliationMode.REPAIR_SAFE}

    @property
    def can_block(self) -> bool:
        return self in {ReconciliationMode.BLOCK, ReconciliationMode.REPAIR_SAFE}

    @property
    def can_repair(self) -> bool:
        return self is ReconciliationMode.REPAIR_SAFE


class LearningAdvisoryMode(_ModuleModeBehavior, str, Enum):
    OFF = "OFF"
    OBSERVE = "OBSERVE"
    SHADOW = "SHADOW"
    REDUCE_PRIORITY = "REDUCE_PRIORITY"
    PAPER_BLOCK = "PAPER_BLOCK"

    @property
    def can_affect_execution(self) -> bool:
        return self in {
            LearningAdvisoryMode.REDUCE_PRIORITY,
            LearningAdvisoryMode.PAPER_BLOCK,
        }

    @property
    def can_reduce_risk(self) -> bool:
        return self in {
            LearningAdvisoryMode.REDUCE_PRIORITY,
            LearningAdvisoryMode.PAPER_BLOCK,
        }

    @property
    def can_block(self) -> bool:
        return self is LearningAdvisoryMode.PAPER_BLOCK


ModuleMode = OperationalModuleMode | SmartSizingMode | ReconciliationMode | LearningAdvisoryMode
ModeEnum = (
    type[OperationalModuleMode]
    | type[SmartSizingMode]
    | type[ReconciliationMode]
    | type[LearningAdvisoryMode]
)
TMode = TypeVar("TMode", bound=Enum)

MODULE_MODE_ATTRS: dict[str, str] = {
    MODULE_DYNAMIC_EXIT: "dynamic_exit_mode",
    MODULE_SMART_SIZING: "smart_sizing_mode",
    MODULE_SMART_EXECUTION: "smart_execution_mode",
    MODULE_STATE_RECONCILIATION: "state_reconciliation_mode",
    MODULE_LEARNING_ADVISORY: "learning_advisory_mode",
    MODULE_PORTFOLIO_ALLOCATOR: "portfolio_allocator_mode",
}

MODULE_ENV_KEYS: dict[str, str] = {
    MODULE_DYNAMIC_EXIT: "ALPHORA_DYNAMIC_EXIT_MODE",
    MODULE_SMART_SIZING: "ALPHORA_SMART_SIZING_MODE",
    MODULE_SMART_EXECUTION: "ALPHORA_SMART_EXECUTION_MODE",
    MODULE_STATE_RECONCILIATION: "ALPHORA_STATE_RECONCILIATION_MODE",
    MODULE_LEARNING_ADVISORY: "ALPHORA_LEARNING_ADVISORY_MODE",
    MODULE_PORTFOLIO_ALLOCATOR: "ALPHORA_PORTFOLIO_ALLOCATOR_MODE",
}

MODULE_ENUMS: dict[str, ModeEnum] = {
    MODULE_DYNAMIC_EXIT: OperationalModuleMode,
    MODULE_SMART_SIZING: SmartSizingMode,
    MODULE_SMART_EXECUTION: OperationalModuleMode,
    MODULE_STATE_RECONCILIATION: ReconciliationMode,
    MODULE_LEARNING_ADVISORY: LearningAdvisoryMode,
    MODULE_PORTFOLIO_ALLOCATOR: OperationalModuleMode,
}

DEFAULT_MODULE_MODES: dict[str, ModuleMode] = {
    MODULE_DYNAMIC_EXIT: OperationalModuleMode.OFF,
    MODULE_SMART_SIZING: SmartSizingMode.OFF,
    MODULE_SMART_EXECUTION: OperationalModuleMode.OFF,
    MODULE_STATE_RECONCILIATION: ReconciliationMode.ALERT,
    MODULE_LEARNING_ADVISORY: LearningAdvisoryMode.OFF,
    MODULE_PORTFOLIO_ALLOCATOR: OperationalModuleMode.OFF,
}

RETIRED_MODULE_ENV_KEYS: dict[str, tuple[str, ...]] = {
    MODULE_DYNAMIC_EXIT: ("ALPHORA_DYNAMIC_EXIT_ENABLED",),
    MODULE_SMART_SIZING: ("ALPHORA_DYNAMIC_SIZING_ENABLED",),
    MODULE_SMART_EXECUTION: (
        "ALPHORA_EXECUTION_PLANNER_ENABLED",
        "ALPHORA_ADAPTIVE_EXECUTION_ENABLED",
        "ALPHORA_ADAPTIVE_MANAGER_ENABLED",
    ),
    MODULE_STATE_RECONCILIATION: ("ALPHORA_USER_DATA_STREAM_CACHE_RECONCILIATION_ENABLED",),
    MODULE_LEARNING_ADVISORY: (
        "ALPHORA_LEARNING_STORE_ENABLED",
        "ALPHORA_LEARNING_PAPER_ACTIVATION_ENABLED",
        "ALPHORA_LEARNING_PAPER_REDUCTION_ENABLED",
        "ALPHORA_LEARNING_PAPER_BLOCK_ENABLED",
        "ALPHORA_LEARNING_TESTNET_OBSERVATION_ENABLED",
    ),
    MODULE_PORTFOLIO_ALLOCATOR: ("ALPHORA_PORTFOLIO_ALLOCATOR_ENABLED",),
}


@dataclass(frozen=True)
class ModuleModeResolution:
    modes: dict[str, ModuleMode]
    sources: dict[str, str]

    def mode_values(self) -> dict[str, str]:
        return module_modes_to_values(self.modes)


def normalize_mode_token(value: Any) -> str:
    return "_".join(str(value or "").strip().upper().replace("-", " ").split())


def parse_module_mode(
    value: Any,
    enum_type: type[TMode],
    *,
    name: str,
) -> TMode:
    if isinstance(value, enum_type):
        return value
    normalized = normalize_mode_token(value)
    try:
        return enum_type(normalized)
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{name} invalido: {value!r}. Valores aceitos: {allowed}.") from error


def default_module_mode(module: str) -> ModuleMode:
    return DEFAULT_MODULE_MODES[module]


def default_operational_module_modes() -> dict[str, ModuleMode]:
    return dict(DEFAULT_MODULE_MODES)


def module_modes_to_values(modes: Mapping[str, Any]) -> dict[str, str]:
    return {
        module: _mode_value(modes.get(module, default_module_mode(module)))
        for module in OPERATIONAL_MODULES
    }


def module_modes_from_mapping(
    values: Mapping[str, Any],
    *,
    source: str = ModuleModeSource.DEFAULT.value,
) -> ModuleModeResolution:
    modes: dict[str, ModuleMode] = {}
    sources: dict[str, str] = {}
    for module in OPERATIONAL_MODULES:
        attr = MODULE_MODE_ATTRS[module]
        raw_value = values.get(module, values.get(attr, default_module_mode(module)))
        modes[module] = parse_module_mode(
            raw_value,
            MODULE_ENUMS[module],
            name=attr,
        )
        sources[module] = source
    return ModuleModeResolution(modes=modes, sources=sources)


def resolve_configured_module_modes(
    *,
    raw_modes: Mapping[str, Any],
    sources: Mapping[str, str] | None,
    execution_mode: str,
) -> ModuleModeResolution:
    del execution_mode
    resolved_modes: dict[str, ModuleMode] = {}
    resolved_sources: dict[str, str] = {}
    source_map = dict(sources or {})
    for module in OPERATIONAL_MODULES:
        source = _normalize_source(source_map.get(module))
        raw_value = raw_modes.get(module)
        if raw_value is None:
            raw_value = raw_modes.get(MODULE_MODE_ATTRS[module])

        if raw_value is not None:
            resolved_modes[module] = parse_module_mode(
                raw_value,
                MODULE_ENUMS[module],
                name=MODULE_MODE_ATTRS[module],
            )
            resolved_sources[module] = source
            continue

        resolved_modes[module] = default_module_mode(module)
        resolved_sources[module] = ModuleModeSource.DEFAULT.value
    return ModuleModeResolution(modes=resolved_modes, sources=resolved_sources)


def load_module_modes_from_env(
    *,
    execution_mode: str,
    getenv: Callable[[str], str | None] = os.getenv,
) -> ModuleModeResolution:
    launched_by_gui = _parse_bool(getenv("ALPHORA_LAUNCHED_BY_GUI"), default=False)
    env_source = ModuleModeSource.DESKTOP.value if launched_by_gui else ModuleModeSource.ENV.value
    modes: dict[str, ModuleMode] = {}
    sources: dict[str, str] = {}
    _reject_retired_module_env_keys(getenv)

    for module in OPERATIONAL_MODULES:
        env_key = MODULE_ENV_KEYS[module]
        raw_value = getenv(env_key)
        if raw_value is not None and raw_value.strip():
            modes[module] = parse_module_mode(
                raw_value,
                MODULE_ENUMS[module],
                name=env_key,
            )
            sources[module] = env_source
            continue

        modes[module] = default_module_mode(module)
        sources[module] = ModuleModeSource.DEFAULT.value

    return ModuleModeResolution(modes=modes, sources=sources)


def modes_for_intelligent_activation(
    *,
    execution_mode: str,
    activation_level: str,
) -> dict[str, ModuleMode]:
    mode = str(execution_mode or "DRY_RUN").upper()
    level = str(activation_level or "SHADOW_ONLY").upper()
    base: dict[str, ModuleMode] = {
        MODULE_DYNAMIC_EXIT: OperationalModuleMode.OFF,
        MODULE_SMART_SIZING: SmartSizingMode.OFF,
        MODULE_SMART_EXECUTION: OperationalModuleMode.OFF,
        MODULE_STATE_RECONCILIATION: ReconciliationMode.ALERT,
        MODULE_LEARNING_ADVISORY: LearningAdvisoryMode.SHADOW,
        MODULE_PORTFOLIO_ALLOCATOR: OperationalModuleMode.OFF,
    }

    if mode == "PAPER_LIVE" and level == "PAPER_ACTIVE":
        base.update(
            {
                MODULE_DYNAMIC_EXIT: OperationalModuleMode.ACTIVE,
                MODULE_SMART_SIZING: SmartSizingMode.ACTIVE,
                MODULE_SMART_EXECUTION: OperationalModuleMode.ACTIVE,
                MODULE_LEARNING_ADVISORY: LearningAdvisoryMode.PAPER_BLOCK,
                MODULE_PORTFOLIO_ALLOCATOR: OperationalModuleMode.ACTIVE,
            }
        )
    elif mode == "TESTNET_LIVE" and level == "TESTNET_OBSERVE":
        base[MODULE_LEARNING_ADVISORY] = LearningAdvisoryMode.OBSERVE
    elif mode == "TESTNET_LIVE" and level == "TESTNET_REDUCE":
        base.update(
            {
                MODULE_SMART_SIZING: SmartSizingMode.REDUCE_ONLY,
                MODULE_LEARNING_ADVISORY: LearningAdvisoryMode.OBSERVE,
                MODULE_PORTFOLIO_ALLOCATOR: OperationalModuleMode.ACTIVE,
            }
        )
    elif mode == "TESTNET_LIVE" and level == "TESTNET_BLOCK":
        base.update(
            {
                MODULE_DYNAMIC_EXIT: OperationalModuleMode.ACTIVE,
                MODULE_SMART_SIZING: SmartSizingMode.ACTIVE,
                MODULE_SMART_EXECUTION: OperationalModuleMode.ACTIVE,
                MODULE_LEARNING_ADVISORY: LearningAdvisoryMode.OBSERVE,
                MODULE_PORTFOLIO_ALLOCATOR: OperationalModuleMode.ACTIVE,
            }
        )
    elif mode == "REAL_LIVE":
        base[MODULE_LEARNING_ADVISORY] = LearningAdvisoryMode.OBSERVE

    return base


def module_runtime_flags_from_modes(
    modes: Mapping[str, Any],
    *,
    execution_mode: str,
) -> dict[str, bool]:
    execution = str(execution_mode or "").upper()
    paper_live = execution == "PAPER_LIVE"
    testnet_live = execution == "TESTNET_LIVE"
    dynamic_exit = _coerce_mode(MODULE_DYNAMIC_EXIT, modes)
    smart_sizing = _coerce_mode(MODULE_SMART_SIZING, modes)
    smart_execution = _coerce_mode(MODULE_SMART_EXECUTION, modes)
    learning = _coerce_mode(MODULE_LEARNING_ADVISORY, modes)
    portfolio = _coerce_mode(MODULE_PORTFOLIO_ALLOCATOR, modes)
    return {
        "dynamic_exit_enabled": dynamic_exit.can_affect_execution,
        "dynamic_sizing_enabled": smart_sizing.can_affect_execution,
        "portfolio_allocator_enabled": portfolio.can_affect_execution,
        "execution_planner_enabled": smart_execution.can_affect_execution,
        "adaptive_manager_enabled": smart_execution.is_enabled,
        "adaptive_execution_enabled": smart_execution.can_affect_execution,
        "learning_store_enabled": learning.is_enabled,
        "learning_paper_activation_enabled": (
            paper_live
            and learning
            in {
                LearningAdvisoryMode.REDUCE_PRIORITY,
                LearningAdvisoryMode.PAPER_BLOCK,
            }
        ),
        "learning_paper_reduction_enabled": (
            paper_live
            and learning
            in {
                LearningAdvisoryMode.REDUCE_PRIORITY,
                LearningAdvisoryMode.PAPER_BLOCK,
            }
        ),
        "learning_paper_block_enabled": (
            paper_live and learning is LearningAdvisoryMode.PAPER_BLOCK
        ),
        "learning_testnet_observation_enabled": testnet_live and learning.is_enabled,
    }


def module_mode_effects(modes: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    effects = {}
    for module in OPERATIONAL_MODULES:
        mode = _coerce_mode(module, modes)
        effects[module] = {
            "mode": mode.value,
            "is_enabled": mode.is_enabled,
            "is_observe_only": mode.is_observe_only,
            "is_shadow": mode.is_shadow,
            "can_affect_execution": mode.can_affect_execution,
            "can_reduce_risk": mode.can_reduce_risk,
            "can_block": mode.can_block,
            "can_repair": mode.can_repair,
            "affected_result": mode.affected_result,
        }
    return effects


def operational_module_modes_from_config(config: Any) -> dict[str, ModuleMode]:
    return {
        module: _coerce_mode(
            module,
            {
                module: getattr(
                    config,
                    MODULE_MODE_ATTRS[module],
                    default_module_mode(module),
                )
            },
        )
        for module in OPERATIONAL_MODULES
    }


def operational_module_mode_snapshot(config: Any) -> dict[str, str]:
    return module_modes_to_values(operational_module_modes_from_config(config))


def operational_module_mode_sources_from_config(config: Any) -> dict[str, str]:
    raw_sources = getattr(config, "operational_module_mode_sources", {}) or {}
    return {module: _normalize_source(raw_sources.get(module)) for module in OPERATIONAL_MODULES}


def operational_module_mode_audit_records(config: Any) -> list[dict[str, Any]]:
    modes = operational_module_modes_from_config(config)
    sources = operational_module_mode_sources_from_config(config)
    records = []
    for module in OPERATIONAL_MODULES:
        mode = modes[module]
        records.append(
            {
                "module": module,
                "mode": mode.value,
                "can_affect_execution": mode.can_affect_execution,
                "can_reduce_risk": mode.can_reduce_risk,
                "can_block": mode.can_block,
                "can_repair": mode.can_repair,
                "source": sources[module],
            }
        )
    return records


def _coerce_mode(module: str, modes: Mapping[str, Any]) -> ModuleMode:
    raw_value = modes.get(module, modes.get(MODULE_MODE_ATTRS[module]))
    if raw_value is None:
        raw_value = default_module_mode(module)
    return parse_module_mode(
        raw_value,
        MODULE_ENUMS[module],
        name=MODULE_MODE_ATTRS[module],
    )


def _mode_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _normalize_source(value: Any) -> str:
    if value is None or value == "":
        return ModuleModeSource.DEFAULT.value
    normalized = normalize_mode_token(value)
    if normalized in {item.value for item in ModuleModeSource}:
        return normalized
    allowed = ", ".join(item.value for item in ModuleModeSource)
    raise ValueError(f"Fonte de modo operacional invalida: {value!r}. Valores aceitos: {allowed}.")


def _reject_retired_module_env_keys(getenv: Callable[[str], str | None]) -> None:
    retired = [
        key
        for keys in RETIRED_MODULE_ENV_KEYS.values()
        for key in keys
        if _env_present(getenv, key)
    ]
    if retired:
        replacements = ", ".join(MODULE_ENV_KEYS[module] for module in OPERATIONAL_MODULES)
        raise ValueError(
            "Flags booleanas de modulos operacionais foram removidas: "
            f"{', '.join(sorted(retired))}. Configure os modos atuais: {replacements}."
        )


def _env_present(getenv: Callable[[str], str | None], key: str) -> bool:
    value = getenv(key)
    return value is not None and bool(value.strip())


def _parse_bool(raw_value: Any, *, default: bool, name: str = "value") -> bool:
    if raw_value is None:
        return default
    value = str(raw_value).strip().lower()
    if not value:
        return default
    if value in {"true", "1", "yes", "y", "sim"}:
        return True
    if value in {"false", "0", "no", "n", "nao"}:
        return False
    raise ValueError(f"{name} precisa ser true ou false")


__all__ = [
    "DEFAULT_MODULE_MODES",
    "MODULE_DYNAMIC_EXIT",
    "MODULE_ENV_KEYS",
    "MODULE_LEARNING_ADVISORY",
    "MODULE_MODE_ATTRS",
    "MODULE_PORTFOLIO_ALLOCATOR",
    "MODULE_SMART_EXECUTION",
    "MODULE_SMART_SIZING",
    "MODULE_STATE_RECONCILIATION",
    "ModuleMode",
    "ModuleModeResolution",
    "ModuleModeSource",
    "OperationalModuleMode",
    "SmartSizingMode",
    "ReconciliationMode",
    "LearningAdvisoryMode",
    "default_operational_module_modes",
    "load_module_modes_from_env",
    "module_runtime_flags_from_modes",
    "modes_for_intelligent_activation",
    "module_mode_effects",
    "module_modes_from_mapping",
    "module_modes_to_values",
    "operational_module_mode_audit_records",
    "operational_module_mode_snapshot",
    "operational_module_mode_sources_from_config",
    "operational_module_modes_from_config",
    "parse_module_mode",
    "resolve_configured_module_modes",
]
