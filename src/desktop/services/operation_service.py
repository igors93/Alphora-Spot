from __future__ import annotations

import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from config.state_paths import default_journal_path, default_state_path, financial_path_key
from config.symbol_profile_schema import (
    SymbolProfileStrategyConflict,
    canonicalize_symbol_profile_payload,
)
from desktop.services.operation_models import (
    ConfirmationKind,
    ConfirmationRequest,
    MessageSeverity,
    OperationLaunchPlan,
    OperationMessage,
    OperationMode,
    OperationPreparationResult,
    OperationPreparationStatus,
)

ENGINE_MODULE = "engine"

REAL_LIVE_CHECKLIST = (
    "Rodei DRY_RUN hoje.",
    "Rodei TESTNET_LIVE com esta configuração.",
    "Verifiquei que não há posição inesperada.",
    "Verifiquei que não há ordens inesperadas.",
    "Estou usando notional mínimo.",
    "Entendo que isso opera conta real.",
)


@dataclass(frozen=True, slots=True)
class OperationDependencies:
    normalize_environment: Callable[[dict[str, Any]], str]
    validate_ai: Callable[[dict[str, Any], list[dict[str, Any]]], Any]
    build_intelligent_review: Callable[..., Any]
    format_intelligent_review: Callable[[Any], str]
    select_credentials: Callable[[dict[str, Any]], tuple[str, str]]
    load_strategy: Callable[[str, str, dict[str, Any]], Any]
    build_governor_environment: Callable[..., dict[str, str]]
    build_robot_command: Callable[[], list[str]]
    single_symbol_profiles_path: str
    sanitize: Callable[[Any], Any]
    apply_architecture_preset: Callable[[dict[str, Any]], dict[str, Any]]

    @classmethod
    def project_defaults(cls) -> OperationDependencies:
        from desktop.ai_validation import validate_probabilistic_ai_start
        from desktop.intelligent_operation_review import (
            build_intelligent_operation_review,
            format_operation_review_for_display,
        )
        from desktop.settings_store import (
            SINGLE_SYMBOL_PROFILES_PATH,
            apply_architecture_preset,
            normalize_environment,
            selected_api_credentials,
            settings_to_governor_env,
        )
        from strategies.loader import load_strategy
        from utils.privacy import sanitize_for_audit

        return cls(
            normalize_environment=normalize_environment,
            validate_ai=validate_probabilistic_ai_start,
            build_intelligent_review=build_intelligent_operation_review,
            format_intelligent_review=format_operation_review_for_display,
            select_credentials=selected_api_credentials,
            load_strategy=load_strategy,
            build_governor_environment=settings_to_governor_env,
            build_robot_command=build_engine_command,
            single_symbol_profiles_path=SINGLE_SYMBOL_PROFILES_PATH,
            sanitize=sanitize_for_audit,
            apply_architecture_preset=apply_architecture_preset,
        )


def build_engine_command(python_executable: str | None = None) -> list[str]:
    """Command that starts the Governor engine with unbuffered output for live logs."""
    return [python_executable or sys.executable, "-u", "-m", ENGINE_MODULE]


class OperationPreparationService:
    def __init__(self, dependencies: OperationDependencies | None = None) -> None:
        self._dependencies = dependencies or OperationDependencies.project_defaults()

    def prepare(
        self,
        settings: dict[str, Any],
        symbol_profiles: list[dict[str, Any]],
    ) -> OperationPreparationResult:
        try:
            return self._prepare(settings, symbol_profiles)
        except Exception as error:
            return _internal_error_result(self._dependencies.sanitize, error)

    def _prepare(
        self,
        settings: dict[str, Any],
        symbol_profiles: list[dict[str, Any]],
    ) -> OperationPreparationResult:
        prepared_settings = self._dependencies.apply_architecture_preset(deepcopy(settings))
        prepared_profiles = deepcopy(symbol_profiles)
        messages: list[OperationMessage] = []
        confirmations: list[ConfirmationRequest] = []

        prepared_settings["environment"] = self._dependencies.normalize_environment(
            prepared_settings
        )

        prepared_profiles, profile_messages = _canonicalize_profiles(
            prepared_profiles,
            global_strategy_module=str(prepared_settings.get("strategy_module", "")),
            global_strategy_class=str(prepared_settings.get("strategy_class", "")),
        )
        messages.extend(profile_messages)
        profile_errors = [m for m in profile_messages if m.severity is MessageSeverity.ERROR]
        if profile_errors:
            return OperationPreparationResult(
                status=OperationPreparationStatus.BLOCKED,
                messages=tuple(messages),
            )

        enabled_profiles = _enabled_profiles(prepared_profiles)

        if not enabled_profiles:
            return _blocked_result(
                messages,
                code="symbols_missing",
                title="Nenhuma moeda habilitada",
                message=(
                    "Habilite exatamente uma moeda para Governor single-symbol "
                    "ou duas ou mais para Governor multi-symbol."
                ),
            )

        ai_validation = self._dependencies.validate_ai(prepared_settings, enabled_profiles)
        ai_message = str(ai_validation.message())
        messages.append(
            OperationMessage(
                code="ai_validation",
                title="Validação IA",
                message=ai_message,
            )
        )
        if not bool(ai_validation.can_start):
            return _blocked_result(
                messages,
                code="ai_validation_blocked",
                title="Validação IA bloqueou o início",
                message=ai_message,
            )
        if getattr(ai_validation, "warnings", None):
            confirmations.append(
                ConfirmationRequest(
                    key="ai_validation_warnings",
                    kind=ConfirmationKind.ACKNOWLEDGEMENT,
                    title="Validação IA com avisos",
                    message=ai_message + "\n\nDeseja iniciar mesmo assim?",
                )
            )

        review = self._dependencies.build_intelligent_review(
            settings=prepared_settings,
            symbol_profiles=enabled_profiles,
        )
        review_message = self._dependencies.format_intelligent_review(review)
        messages.append(
            OperationMessage(
                code="intelligent_operation_review",
                title="Revisão operacional inteligente",
                message=review_message,
            )
        )
        if not bool(review.can_start):
            return _blocked_result(
                messages,
                code="intelligent_operation_blocked",
                title="Início bloqueado pela revisão inteligente",
                message=review_message,
            )
        if bool(review.requires_confirmation):
            confirmations.append(
                ConfirmationRequest(
                    key="intelligent_operation_warnings",
                    kind=ConfirmationKind.ACKNOWLEDGEMENT,
                    title="Revisão inteligente com avisos",
                    message=review_message + "\n\nDeseja iniciar mesmo assim?",
                )
            )

        strategy_messages = self._validate_strategy(prepared_settings, enabled_profiles)
        messages.extend(strategy_messages)
        strategy_errors = [
            item for item in strategy_messages if item.severity is MessageSeverity.ERROR
        ]
        if strategy_errors:
            return OperationPreparationResult(
                status=OperationPreparationStatus.BLOCKED,
                messages=tuple(messages),
            )

        strategy_warnings = [
            item for item in strategy_messages if item.severity is MessageSeverity.WARNING
        ]
        if strategy_warnings:
            warning_text = "\n".join(item.message for item in strategy_warnings)
            confirmations.append(
                ConfirmationRequest(
                    key="strategy_warnings",
                    kind=ConfirmationKind.ACKNOWLEDGEMENT,
                    title="Estratégia global",
                    message=warning_text + "\n\nDeseja iniciar mesmo assim?",
                )
            )

        api_key, api_secret = self._dependencies.select_credentials(prepared_settings)
        if not api_key or not api_secret:
            return _blocked_result(
                messages,
                code="credentials_missing",
                title="Chaves ausentes",
                message="Configure API key e secret do ambiente selecionado.",
            )

        if len(enabled_profiles) == 1:
            mode = OperationMode.GOVERNOR_SINGLE_SYMBOL
            prepared_settings["symbol"] = str(
                enabled_profiles[0].get("symbol", prepared_settings.get("symbol", "BTCUSDT"))
            ).upper()
            prepared_settings["operation_mode"] = "SIMPLE"
            runtime_profiles: tuple[dict[str, Any], ...] = (
                _single_runtime_profile(
                    prepared_settings,
                    enabled_profiles[0],
                    self._dependencies.normalize_environment,
                ),
            )
            symbol_profiles_path = self._dependencies.single_symbol_profiles_path
            environment = self._dependencies.build_governor_environment(
                prepared_settings,
                symbol_profiles_path=symbol_profiles_path,
                multi_symbol=False,
            )
        else:
            mode = OperationMode.GOVERNOR_MULTI_SYMBOL
            prepared_settings["operation_mode"] = "MULTI_SYMBOL"
            runtime_profiles = tuple(deepcopy(enabled_profiles))
            symbol_profiles_path = None
            environment = self._dependencies.build_governor_environment(
                prepared_settings,
                multi_symbol=True,
            )

        execution_mode = str(prepared_settings.get("execution_mode", "DRY_RUN")).upper()
        if execution_mode == "REAL_LIVE":
            confirmations.extend(
                _real_live_confirmations(multi_symbol=mode is OperationMode.GOVERNOR_MULTI_SYMBOL)
            )

        symbols = tuple(str(profile.get("symbol", "")).upper() for profile in runtime_profiles)
        summary = format_effective_operation_summary(
            settings=prepared_settings,
            environment=environment,
            mode=mode,
            symbols=symbols,
        )
        plan = OperationLaunchPlan(
            mode=mode,
            command=tuple(self._dependencies.build_robot_command()),
            symbol_profiles_path=symbol_profiles_path,
            summary=summary,
            confirmations=tuple(confirmations),
            settings=deepcopy(prepared_settings),
            enabled_profiles=tuple(deepcopy(enabled_profiles)),
            runtime_profiles=tuple(deepcopy(runtime_profiles)),
            environment=dict(environment),
        )
        status = (
            OperationPreparationStatus.REQUIRES_CONFIRMATION
            if confirmations
            else OperationPreparationStatus.READY
        )
        return OperationPreparationResult(
            status=status,
            messages=tuple(messages),
            plan=plan,
        )

    def _validate_strategy(
        self,
        settings: dict[str, Any],
        profiles: list[dict[str, Any]],
    ) -> list[OperationMessage]:
        strategy_module = str(settings.get("strategy_module", ""))
        strategy_class = str(settings.get("strategy_class", ""))
        strategy_params = settings.get("strategy_params", {})
        if not isinstance(strategy_params, dict):
            strategy_params = {}
        global_name = f"{strategy_module}.{strategy_class}"

        try:
            strategy = self._dependencies.load_strategy(
                strategy_module,
                strategy_class,
                strategy_params,
            )
        except Exception as error:
            return [
                OperationMessage(
                    code="strategy_invalid",
                    title="Estratégia global inválida",
                    message=_safe_text(self._dependencies.sanitize, error),
                    severity=MessageSeverity.ERROR,
                )
            ]

        messages: list[OperationMessage] = []
        has_score_symbol = callable(getattr(strategy, "score_symbol", None))
        has_intent_symbol = callable(getattr(strategy, "intent_symbol", None))
        has_decide_symbol = callable(getattr(strategy, "decide_symbol", None))
        if not (has_score_symbol and (has_intent_symbol or has_decide_symbol)):
            messages.append(
                OperationMessage(
                    code="strategy_contract_warning",
                    title="Contrato da estratégia",
                    message=(
                        f"{global_name}: sem score_symbol + intent_symbol/decide_symbol; "
                        "o Governor exige contrato canonico por símbolo."
                    ),
                    severity=MessageSeverity.WARNING,
                )
            )

        for profile in profiles:
            symbol = str(profile.get("symbol", "")).upper()
            if profile.get("strategy_params"):
                messages.append(
                    OperationMessage(
                        code="profile_strategy_params_context",
                        title="Parâmetros locais da estratégia",
                        message=(
                            f"{symbol}: strategy_params do perfil serão usados apenas como "
                            "contexto local da estratégia global."
                        ),
                        severity=MessageSeverity.WARNING,
                    )
                )

        if not messages:
            messages.append(
                OperationMessage(
                    code="strategy_valid",
                    title="Estratégia global",
                    message=f"Estratégia {global_name} validada para o Governor.",
                )
            )
        return messages


def format_effective_operation_summary(
    *,
    settings: dict[str, Any],
    environment: dict[str, str],
    mode: OperationMode,
    symbols: tuple[str, ...],
) -> str:
    global_strategy = f"{settings.get('strategy_module')}.{settings.get('strategy_class')}"
    primary_symbol = symbols[0]
    environment_key = financial_path_key(
        str(environment.get("ALPHORA_ENVIRONMENT", "TESTNET")),
        str(settings.get("execution_mode", "DRY_RUN")),
    )
    runtime_dir = environment.get("ALPHORA_RUNTIME_DIR", "runtime")
    return "\n".join(
        [
            "Configuração efetiva enviada ao processo:",
            "- origem: GUI",
            f"- arquitetura: {settings.get('architecture_mode', 'INTELLIGENT_FLOW')}",
            f"- nível inteligente: {settings.get('intelligent_activation_level', 'SHADOW_ONLY')}",
            f"- execution_mode: {settings.get('execution_mode')}",
            f"- environment: {settings.get('environment')}",
            f"- trade_planning: {environment.get('ALPHORA_TRADE_PLANNING_ENABLED')}",
            f"- dynamic_exit_mode: {environment.get('ALPHORA_DYNAMIC_EXIT_MODE')}",
            f"- smart_sizing_mode: {environment.get('ALPHORA_SMART_SIZING_MODE')}",
            f"- smart_execution_mode: {environment.get('ALPHORA_SMART_EXECUTION_MODE')}",
            f"- state_reconciliation_mode: {environment.get('ALPHORA_STATE_RECONCILIATION_MODE')}",
            f"- learning_advisory_mode: {environment.get('ALPHORA_LEARNING_ADVISORY_MODE')}",
            f"- portfolio_allocator_mode: {environment.get('ALPHORA_PORTFOLIO_ALLOCATOR_MODE')}",
            f"- modo operacional: {mode.value}",
            f"- símbolos efetivos: {', '.join(symbols)}",
            f"- estratégia global: {global_strategy}",
            "- strategy_state_mode: PER_SYMBOL_STATE",
            f"- runtime_dir: {environment.get('ALPHORA_RUNTIME_DIR', 'runtime')}",
            f"- state_path: {environment.get('ALPHORA_STATE_PATH', default_state_path(runtime_dir, primary_symbol, environment_key))}",
            f"- trade_journal_path: {environment.get('ALPHORA_TRADE_JOURNAL_PATH', default_journal_path(primary_symbol, environment_key))}",
            "- user_data_cache_operational_preset: "
            f"{environment.get('ALPHORA_USER_DATA_CACHE_OPERATIONAL_PRESET', '-')}",
            "- user_data_cache_effective_preset: "
            f"{environment.get('ALPHORA_USER_DATA_CACHE_EFFECTIVE_PRESET', '-')}",
            f"- user_data_stream_enabled: {environment.get('ALPHORA_USER_DATA_STREAM_ENABLED')}",
            "- user_data_cache_position_enabled: "
            f"{environment.get('ALPHORA_USER_DATA_STREAM_CACHE_POSITION_ENABLED')}",
            "- user_data_cache_account_enabled: "
            f"{environment.get('ALPHORA_USER_DATA_STREAM_CACHE_ACCOUNT_ENABLED')}",
            "- user_data_cache_open_orders_enabled: "
            f"{environment.get('ALPHORA_USER_DATA_STREAM_CACHE_OPEN_ORDERS_ENABLED')}",
            "- allow_real_user_data_cache_assisted: "
            f"{environment.get('ALPHORA_ALLOW_REAL_USER_DATA_CACHE_ASSISTED')}",
            "- mutations_enabled: "
            f"{settings.get('execution_mode') in {'TESTNET_LIVE', 'REAL_LIVE'}}",
        ]
    )


def _canonicalize_profiles(
    profiles: list[dict[str, Any]],
    *,
    global_strategy_module: str,
    global_strategy_class: str,
) -> tuple[list[dict[str, Any]], list[OperationMessage]]:
    result: list[dict[str, Any]] = []
    messages: list[OperationMessage] = []
    for profile in profiles:
        try:
            canonical = canonicalize_symbol_profile_payload(
                profile,
                global_strategy_module=global_strategy_module or None,
                global_strategy_class=global_strategy_class or None,
                reject_strategy_conflicts=True,
            )
        except SymbolProfileStrategyConflict as error:
            messages.append(
                OperationMessage(
                    code="profile_strategy_conflict",
                    title="Conflito de estratégia no perfil",
                    message=str(error),
                    severity=MessageSeverity.ERROR,
                )
            )
            result.append(profile)
            continue
        if canonical.warnings:
            for warning in canonical.warnings:
                messages.append(
                    OperationMessage(
                        code="profile_legacy_migration",
                        title="Migração de perfil legado",
                        message=warning,
                        severity=MessageSeverity.WARNING,
                    )
                )
        result.append(canonical.payload)
    return result, messages


def _enabled_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enabled: list[dict[str, Any]] = []
    for profile in profiles:
        if not _as_bool(profile.get("enabled", True)):
            continue
        symbol = str(profile.get("symbol", "")).upper().strip()
        if not symbol:
            continue
        normalized = deepcopy(profile)
        normalized["symbol"] = symbol
        enabled.append(normalized)
    return enabled


def _single_runtime_profile(
    settings: dict[str, Any],
    profile: dict[str, Any],
    normalize_environment: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    from config.symbol_profile_schema import RETIRED_SYMBOL_PROFILE_STRATEGY_KEYS

    single_profile = deepcopy(profile)
    for key in RETIRED_SYMBOL_PROFILE_STRATEGY_KEYS:
        single_profile.pop(key, None)
    environment_key = financial_path_key(
        normalize_environment(settings),
        str(settings.get("execution_mode", "DRY_RUN")),
    )
    symbol = str(single_profile.get("symbol", settings.get("symbol", "BTCUSDT"))).lower()
    single_profile.update(
        {
            "enabled": True,
            "symbol": symbol.upper(),
            "state_path": default_state_path("runtime", symbol, environment_key),
            "journal_path": default_journal_path(symbol, environment_key),
            "lock_path": f"runtime/alphora-{symbol}-{environment_key}.lock",
            "runtime_dir": "runtime",
        }
    )
    return single_profile


def _real_live_confirmations(*, multi_symbol: bool) -> list[ConfirmationRequest]:
    requests = [
        ConfirmationRequest(
            key="real_live_checklist",
            kind=ConfirmationKind.CHECKLIST,
            title="Checklist REAL_LIVE",
            message="Confirme todos os itens antes de REAL_LIVE.",
            severity=MessageSeverity.ERROR,
            checklist_items=REAL_LIVE_CHECKLIST,
        ),
        ConfirmationRequest(
            key="real_live_typed_confirmation",
            kind=ConfirmationKind.TYPED,
            title="Confirmar REAL_LIVE",
            message="Digite REAL_LIVE para confirmar:",
            severity=MessageSeverity.ERROR,
            required_tokens=("REAL_LIVE",),
        ),
    ]
    if multi_symbol:
        requests.append(
            ConfirmationRequest(
                key="multi_real_live_typed_confirmation",
                kind=ConfirmationKind.TYPED,
                title="Confirmar MULTI REAL LIVE",
                message=("Digite MULTI_REAL_LIVE para confirmar multi-moedas em conta real:"),
                severity=MessageSeverity.ERROR,
                required_tokens=("MULTI_REAL_LIVE",),
            )
        )
    return requests


def _blocked_result(
    messages: list[OperationMessage],
    *,
    code: str,
    title: str,
    message: str,
) -> OperationPreparationResult:
    messages.append(
        OperationMessage(
            code=code,
            title=title,
            message=message,
            severity=MessageSeverity.ERROR,
        )
    )
    return OperationPreparationResult(
        status=OperationPreparationStatus.BLOCKED,
        messages=tuple(messages),
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "sim"}


def _safe_text(sanitizer: Callable[[Any], Any], value: Any) -> str:
    try:
        return str(sanitizer(value))
    except Exception:
        return "[SANITIZATION_ERROR]"


def _internal_error_result(
    sanitizer: Callable[[Any], Any],
    error: Exception,
) -> OperationPreparationResult:
    return OperationPreparationResult(
        status=OperationPreparationStatus.BLOCKED,
        messages=(
            OperationMessage(
                code="operation_preparation_error",
                title="Não foi possível preparar a operação",
                message=_safe_text(sanitizer, error),
                severity=MessageSeverity.ERROR,
            ),
        ),
    )
