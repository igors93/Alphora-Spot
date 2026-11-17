from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class StrategyDescriptor:
    qualified_name: str
    module_name: str
    class_name: str
    valid: bool
    import_error: str
    has_score_symbol: bool
    has_decide_symbol: bool
    has_intent_symbol: bool
    has_from_env: bool
    has_from_params: bool
    required_history: str
    supports_short: str
    required_timeframes: tuple[str, ...]
    required_indicators: tuple[str, ...]
    docstring: str

    @property
    def contract_label(self) -> str:
        if not self.valid:
            return "Inválida"
        if self.has_score_symbol and (self.has_intent_symbol or self.has_decide_symbol):
            return "Contrato completo"
        return "Contrato incompleto"

    @property
    def details(self) -> str:
        if self.import_error:
            return "\n".join(
                (
                    f"Módulo: {self.module_name}",
                    "Erro de importação:",
                    self.import_error,
                )
            )
        return "\n".join(
            (
                "Estratégia disponível para o Governor.",
                "A memória operacional permanece isolada por símbolo.",
                "",
                f"Módulo: {self.module_name}",
                f"Classe: {self.class_name}",
                f"score_symbol: {_yes_no(self.has_score_symbol)}",
                f"intent_symbol: {_yes_no(self.has_intent_symbol)}",
                f"decide_symbol: {_yes_no(self.has_decide_symbol)}",
                f"from_env: {_yes_no(self.has_from_env)}",
                f"from_params: {_yes_no(self.has_from_params)}",
                f"required_history: {self.required_history}",
                f"supports_short: {self.supports_short}",
                "required_timeframes: " + (", ".join(self.required_timeframes) or "n/a"),
                "required_indicators: " + (", ".join(self.required_indicators) or "n/a"),
                "",
                self.docstring or "Sem documentação.",
            )
        )


@dataclass(frozen=True, slots=True)
class ValidationMessage:
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class StrategySaveResult:
    settings: dict[str, Any]
    profiles: tuple[dict[str, Any], ...]
    applied_to_profiles: int


@dataclass(frozen=True, slots=True)
class IntelligenceDiagnostics:
    layers: str
    operation_review: str
    process_review: str
    cache_health: str


StrategyDiscovery = Callable[[], Sequence[Any]]
ModelValidator = Callable[[str | Path], Mapping[str, Any]]


_STRATEGY_MODES = ("FIXED", "PROBABILISTIC_AI")
_PROBABILITY_FIELDS = (
    "ai_min_long_probability",
    "ai_min_short_probability",
    "ai_min_direction_gap",
    "ai_close_long_min_short_probability",
    "ai_close_short_min_long_probability",
)
_NON_NEGATIVE_FIELDS = ("ai_min_expected_value_bps",)

_MODULE_FIELDS = (
    "dynamic_exit_mode",
    "smart_sizing_mode",
    "smart_execution_mode",
    "state_reconciliation_mode",
    "learning_advisory_mode",
    "portfolio_allocator_mode",
)


def discover_strategy_descriptors(
    discovery: StrategyDiscovery | None = None,
) -> tuple[StrategyDescriptor, ...]:
    discover = discovery or _default_strategy_discovery()
    descriptors = tuple(_descriptor_from_info(item) for item in discover())
    return tuple(sorted(descriptors, key=lambda item: item.qualified_name.lower()))


def parse_strategy_params(value: str | Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    text = str(value).strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Parâmetros JSON inválidos: {error.msg} na posição {error.pos}."
        ) from error
    if not isinstance(payload, dict):
        raise ValueError("Os parâmetros da estratégia precisam formar um objeto JSON.")
    return payload


def format_strategy_params(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=False, indent=2, sort_keys=True)


def validate_strategy_configuration(
    descriptor: StrategyDescriptor | None,
    selection_mode: str,
    params: str | Mapping[str, Any] | None,
) -> tuple[ValidationMessage, ...]:
    messages: list[ValidationMessage] = []
    mode = str(selection_mode or "").upper().strip()
    if mode not in _STRATEGY_MODES:
        messages.append(
            ValidationMessage("selection_mode", "Modo de seleção de estratégia inválido.")
        )
    if descriptor is None:
        messages.append(ValidationMessage("strategy", "Selecione uma estratégia disponível."))
    elif not descriptor.valid:
        messages.append(
            ValidationMessage(
                "strategy",
                descriptor.import_error or "A estratégia selecionada não pôde ser importada.",
            )
        )
    elif not descriptor.has_score_symbol or not (
        descriptor.has_intent_symbol or descriptor.has_decide_symbol
    ):
        messages.append(
            ValidationMessage(
                "strategy",
                "A estratégia precisa implementar score_symbol e intent_symbol ou decide_symbol.",
            )
        )
    try:
        parse_strategy_params(params)
    except ValueError as error:
        messages.append(ValidationMessage("strategy_params", str(error)))
    return tuple(messages)


def prepare_strategy_save(
    *,
    current_settings: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    descriptor: StrategyDescriptor,
    selection_mode: str,
    params: str | Mapping[str, Any] | None,
    apply_to_profiles: bool,
) -> StrategySaveResult:
    validation = validate_strategy_configuration(descriptor, selection_mode, params)
    if validation:
        raise ValueError(validation[0].message)

    settings = deepcopy(dict(current_settings))
    settings["strategy_module"] = descriptor.module_name
    settings["strategy_class"] = descriptor.class_name
    settings["strategy_selection_mode"] = str(selection_mode).upper().strip()
    settings["strategy_params"] = parse_strategy_params(params)

    prepared_profiles = [deepcopy(dict(profile)) for profile in profiles]
    applied = 0
    if apply_to_profiles:
        for profile in prepared_profiles:
            profile["strategy_params"] = deepcopy(settings["strategy_params"])
            applied += 1

    return StrategySaveResult(
        settings=settings,
        profiles=tuple(prepared_profiles),
        applied_to_profiles=applied,
    )


def validate_intelligence_settings(
    settings: Mapping[str, Any],
) -> tuple[ValidationMessage, ...]:
    messages: list[ValidationMessage] = []
    for key in _PROBABILITY_FIELDS:
        value = _decimal(settings.get(key), key, messages)
        if value is not None and not 0 <= value <= 1:
            messages.append(ValidationMessage(key, f"{key} precisa estar entre 0 e 1."))
    for key in _NON_NEGATIVE_FIELDS:
        value = _decimal(settings.get(key), key, messages)
        if value is not None and value < 0:
            messages.append(ValidationMessage(key, f"{key} não pode ser negativo."))

    activation = str(settings.get("intelligent_activation_level", "")).upper().strip()
    allowed = allowed_activation_levels(str(settings.get("execution_mode", "DRY_RUN")))
    if activation not in allowed:
        messages.append(
            ValidationMessage(
                "intelligent_activation_level",
                "Nível de ativação incompatível com o modo de execução atual.",
            )
        )
    return tuple(messages)


def allowed_activation_levels(execution_mode: str) -> tuple[str, ...]:
    try:
        from desktop.settings_store import allowed_intelligent_activation_levels

        return tuple(allowed_intelligent_activation_levels(execution_mode))
    except Exception:
        mode = str(execution_mode).upper().strip()
        if mode == "PAPER_LIVE":
            return ("SHADOW_ONLY", "PAPER_ACTIVE")
        if mode == "TESTNET_LIVE":
            return (
                "SHADOW_ONLY",
                "TESTNET_OBSERVE",
                "TESTNET_REDUCE",
                "TESTNET_BLOCK",
            )
        if mode == "REAL_LIVE":
            return (
                "SHADOW_ONLY",
                "REAL_OBSERVE",
                "REAL_REDUCE",
                "REAL_BLOCK",
            )
        return ("SHADOW_ONLY",)


def normalize_intelligence_settings(
    current_settings: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    settings = deepcopy(dict(current_settings))
    settings.update(deepcopy(dict(patch)))
    settings["architecture_mode"] = "INTELLIGENT_FLOW"
    settings["intelligent_activation_level"] = str(
        settings.get("intelligent_activation_level", "SHADOW_ONLY")
    ).upper()
    settings["strategy_selection_mode"] = str(
        settings.get("strategy_selection_mode", "FIXED")
    ).upper()
    settings["operational_module_modes_customized"] = _as_bool(
        settings.get("operational_module_modes_customized", False)
    )
    for key in _MODULE_FIELDS:
        settings[key] = str(settings.get(key, "OFF")).upper().strip()

    validation = validate_intelligence_settings(settings)
    if validation:
        raise ValueError(validation[0].message)

    if not settings["operational_module_modes_customized"]:
        try:
            from desktop.settings_store import apply_architecture_preset

            settings = apply_architecture_preset(settings)
        except Exception:
            pass
    return settings


def validate_global_ai_model(
    path: str | Path,
    *,
    require_approved: bool,
    validator: ModelValidator | None = None,
) -> dict[str, Any]:
    normalized_path = str(path or "").strip()
    if not normalized_path:
        raise ValueError("Informe o caminho do modelo global de IA.")
    validate = validator or _default_model_validator()
    metadata = dict(validate(normalized_path))
    if metadata.get("model_type") != "probabilistic_linear_ai_v1":
        raise ValueError("O arquivo não é um modelo probabilístico compatível.")
    if str(metadata.get("scope", "")).upper() != "GLOBAL":
        raise ValueError("O modelo global precisa possuir scope GLOBAL.")
    if require_approved and not _model_is_approved(metadata):
        raise ValueError("O modelo não está aprovado para uso operacional.")
    return metadata


def build_intelligence_diagnostics(
    settings: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> IntelligenceDiagnostics:
    layers = _diagnostic_text(
        lambda: _format_intelligent_layers(settings),
        "Não foi possível calcular as camadas inteligentes.",
    )
    operation_review = _diagnostic_text(
        lambda: _format_operation_review(settings, profiles),
        "Não foi possível executar a revisão operacional.",
    )
    process_review = _diagnostic_text(
        lambda: _format_process_review(settings, profiles),
        "Não foi possível revisar os processos da interface.",
    )
    cache_health = _diagnostic_text(
        lambda: _format_cache_health(settings, profiles),
        "Ainda não há dados suficientes sobre stream e cache privado.",
    )
    return IntelligenceDiagnostics(
        layers=layers,
        operation_review=operation_review,
        process_review=process_review,
        cache_health=cache_health,
    )


def _descriptor_from_info(info: Any) -> StrategyDescriptor:
    module_name = str(getattr(info, "module_name", ""))
    class_name = str(getattr(info, "class_name", ""))
    qualified_name = str(getattr(info, "qualified_name", "") or f"{module_name}.{class_name}")
    import_error = str(getattr(info, "import_error", "") or "")
    has_score = bool(getattr(info, "has_score_symbol", False))
    has_intent = bool(getattr(info, "has_intent_symbol", False))
    has_decide = bool(getattr(info, "has_decide_symbol", False))
    return StrategyDescriptor(
        qualified_name=qualified_name,
        module_name=module_name,
        class_name=class_name,
        valid=not import_error and has_score and (has_intent or has_decide),
        import_error=import_error,
        has_score_symbol=has_score,
        has_decide_symbol=has_decide,
        has_intent_symbol=has_intent,
        has_from_env=bool(getattr(info, "has_from_env", False)),
        has_from_params=bool(getattr(info, "has_from_params", False)),
        required_history=_optional_text(getattr(info, "required_history", None)),
        supports_short=_optional_text(getattr(info, "supports_short", None)),
        required_timeframes=tuple(
            str(value) for value in (getattr(info, "required_timeframes", None) or ())
        ),
        required_indicators=tuple(
            str(value) for value in (getattr(info, "required_indicators", None) or ())
        ),
        docstring=str(getattr(info, "docstring", "") or ""),
    )


def _default_strategy_discovery() -> StrategyDiscovery:
    from desktop.strategy_discovery import discover_futures_strategies

    return discover_futures_strategies


def _default_model_validator() -> ModelValidator:
    from research.ai.registry import validate_ai_model_file

    return validate_ai_model_file


def _decimal(
    value: Any,
    field: str,
    messages: list[ValidationMessage],
) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        messages.append(ValidationMessage(field, f"{field} precisa ser numérico."))
        return None


def _model_is_approved(metadata: Mapping[str, Any]) -> bool:
    if _as_bool(metadata.get("approved", False)):
        return True
    return str(metadata.get("status", "")).upper() in {"APPROVED", "ACTIVE"}


def _diagnostic_text(builder: Callable[[], str], fallback: str) -> str:
    try:
        value = str(builder() or "").strip()
        return value or fallback
    except Exception:
        return fallback


def _format_intelligent_layers(settings: Mapping[str, Any]) -> str:
    from desktop.intelligent_status import format_intelligent_layers_for_display

    return format_intelligent_layers_for_display(dict(settings))


def _format_operation_review(
    settings: Mapping[str, Any], profiles: Sequence[Mapping[str, Any]]
) -> str:
    from desktop.intelligent_operation_review import (
        build_intelligent_operation_review,
        format_operation_review_for_display,
    )

    review = build_intelligent_operation_review(
        settings=dict(settings),
        symbol_profiles=[dict(item) for item in profiles],
    )
    return format_operation_review_for_display(review)


def _format_process_review(
    settings: Mapping[str, Any], profiles: Sequence[Mapping[str, Any]]
) -> str:
    from desktop.interface_process_review import (
        build_interface_process_review,
        format_interface_process_review,
    )

    review = build_interface_process_review(
        settings=dict(settings),
        symbol_profiles=[dict(item) for item in profiles],
    )
    return format_interface_process_review(review)


def _format_cache_health(settings: Mapping[str, Any], profiles: Sequence[Mapping[str, Any]]) -> str:
    from desktop.intelligent_events import build_intelligent_event_paths
    from desktop.user_data_cache_health import (
        format_user_data_cache_health_for_display,
        load_user_data_cache_health_from_paths,
    )

    paths = build_intelligent_event_paths(
        settings=dict(settings),
        symbol_profiles=[dict(item) for item in profiles],
    )
    summary = load_user_data_cache_health_from_paths(paths, limit=20)
    return format_user_data_cache_health_for_display(summary)


def _optional_text(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return _yes_no(value)
    return str(value)


def _yes_no(value: bool) -> str:
    return "sim" if value else "não"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "sim"}
