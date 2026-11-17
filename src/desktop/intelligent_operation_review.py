from dataclasses import dataclass
from typing import Any

from desktop.settings_store import (
    ARCHITECTURE_INTELLIGENT_FLOW,
    INTELLIGENT_LEVEL_PAPER_ACTIVE,
    INTELLIGENT_LEVEL_SHADOW_ONLY,
    INTELLIGENT_LEVEL_TESTNET_BLOCK,
    INTELLIGENT_LEVEL_TESTNET_REDUCE,
    allowed_intelligent_activation_levels,
    apply_architecture_preset,
    normalize_environment,
)
from desktop.user_data_cache_presets import (
    apply_user_data_cache_preset,
    resolve_user_data_cache_preset,
)

REVIEW_STATUS_OK = "OK"
REVIEW_STATUS_WARN = "WARN"
REVIEW_STATUS_BLOCK = "BLOCK"

ACTIVE_EXECUTION_FLAGS = (
    "dynamic_sizing_enabled",
    "dynamic_exit_enabled",
    "execution_planner_enabled",
    "adaptive_execution_enabled",
)

PAPER_REQUIRED_FLAGS = (
    "dynamic_sizing_enabled",
    "dynamic_exit_enabled",
    "execution_planner_enabled",
    "adaptive_manager_enabled",
    "adaptive_execution_enabled",
)

TESTNET_BLOCK_REQUIRED_FLAGS = (
    "dynamic_sizing_enabled",
    "dynamic_exit_enabled",
    "execution_planner_enabled",
)

TESTNET_REDUCE_REQUIRED_FLAGS = ("dynamic_sizing_enabled",)


@dataclass(frozen=True)
class IntelligentOperationReviewItem:
    status: str
    title: str
    message: str


@dataclass(frozen=True)
class IntelligentOperationReview:
    status: str
    can_start: bool
    requires_confirmation: bool
    architecture_mode: str
    intelligent_activation_level: str
    execution_mode: str
    environment: str
    enabled_symbols_count: int
    items: list[IntelligentOperationReviewItem]


def build_intelligent_operation_review(
    *,
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
) -> IntelligentOperationReview:
    raw_prepared = apply_architecture_preset(settings)
    prepared = apply_user_data_cache_preset(raw_prepared)

    architecture = str(prepared.get("architecture_mode", ARCHITECTURE_INTELLIGENT_FLOW)).upper()
    level = str(prepared.get("intelligent_activation_level", INTELLIGENT_LEVEL_SHADOW_ONLY)).upper()
    execution_mode = str(prepared.get("execution_mode", "DRY_RUN")).upper()
    environment = normalize_environment(prepared)
    enabled_symbols = _enabled_symbols(symbol_profiles)

    items: list[IntelligentOperationReviewItem] = []

    _review_symbol_count(items, enabled_symbols)
    _review_level_allowed(items, level=level, execution_mode=execution_mode)
    _review_paper_active(items, prepared=prepared, level=level, execution_mode=execution_mode)
    _review_testnet_reduce(items, prepared=prepared, level=level, execution_mode=execution_mode)
    _review_testnet_block(items, prepared=prepared, level=level, execution_mode=execution_mode)
    _review_real_live(items, prepared=prepared, execution_mode=execution_mode)
    _review_adaptive_execution_scope(
        items, prepared=prepared, level=level, execution_mode=execution_mode
    )
    _review_user_data_cache_preset(
        items,
        raw_prepared=raw_prepared,
        prepared=prepared,
        execution_mode=execution_mode,
        environment=environment,
    )
    _review_environment(items, execution_mode=execution_mode, environment=environment)

    if not items:
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_OK,
                title="Revisão operacional",
                message="Nenhum problema encontrado na configuração atual.",
            )
        )

    final_status = _final_status(items)

    return IntelligentOperationReview(
        status=final_status,
        can_start=final_status != REVIEW_STATUS_BLOCK,
        requires_confirmation=final_status == REVIEW_STATUS_WARN,
        architecture_mode=architecture,
        intelligent_activation_level=level,
        execution_mode=execution_mode,
        environment=environment,
        enabled_symbols_count=len(enabled_symbols),
        items=items,
    )


def format_operation_review_for_display(review: IntelligentOperationReview) -> str:
    lines = [
        "Revisão operacional inteligente",
        "",
        f"Status: {review.status}",
        f"Pode iniciar: {'sim' if review.can_start else 'não'}",
        f"Requer confirmação: {'sim' if review.requires_confirmation else 'não'}",
        f"Arquitetura: {review.architecture_mode}",
        f"Nível inteligente: {review.intelligent_activation_level}",
        f"Execution mode: {review.execution_mode}",
        f"Ambiente efetivo: {review.environment}",
        f"Moedas habilitadas: {review.enabled_symbols_count}",
        "",
        "Checagens:",
    ]

    for item in review.items:
        lines.append(f"- [{item.status}] {item.title}: {item.message}")

    return "\n".join(lines)


def _review_symbol_count(
    items: list[IntelligentOperationReviewItem],
    enabled_symbols: list[str],
) -> None:
    if not enabled_symbols:
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="Moedas habilitadas",
                message="Nenhuma moeda habilitada. O Alphora não deve iniciar.",
            )
        )
        return

    items.append(
        IntelligentOperationReviewItem(
            status=REVIEW_STATUS_OK,
            title="Moedas habilitadas",
            message=f"{len(enabled_symbols)} moeda(s) habilitada(s): {', '.join(enabled_symbols[:8])}",
        )
    )


def _review_level_allowed(
    items: list[IntelligentOperationReviewItem],
    *,
    level: str,
    execution_mode: str,
) -> None:
    allowed = allowed_intelligent_activation_levels(execution_mode)

    if level not in allowed:
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="Nível inteligente incompatível",
                message=(
                    f"O nível {level} não é permitido em {execution_mode}. "
                    f"Permitidos: {', '.join(allowed)}."
                ),
            )
        )
        return

    items.append(
        IntelligentOperationReviewItem(
            status=REVIEW_STATUS_OK,
            title="Nível inteligente",
            message=f"{level} é compatível com {execution_mode}.",
        )
    )


def _review_paper_active(
    items: list[IntelligentOperationReviewItem],
    *,
    prepared: dict[str, Any],
    level: str,
    execution_mode: str,
) -> None:
    if execution_mode != "PAPER_LIVE" or level != INTELLIGENT_LEVEL_PAPER_ACTIVE:
        return

    missing = [key for key in PAPER_REQUIRED_FLAGS if not _as_bool(prepared.get(key))]
    if missing:
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="Paper active incompleto",
                message=(
                    "PAPER_ACTIVE precisa ligar todas as camadas seguras de paper: "
                    + ", ".join(missing)
                ),
            )
        )
        return

    items.append(
        IntelligentOperationReviewItem(
            status=REVIEW_STATUS_OK,
            title="Paper active",
            message="Sizing, stop/take, execution planner e adaptive execution estão ligados em paper.",
        )
    )


def _review_testnet_reduce(
    items: list[IntelligentOperationReviewItem],
    *,
    prepared: dict[str, Any],
    level: str,
    execution_mode: str,
) -> None:
    if execution_mode != "TESTNET_LIVE" or level != INTELLIGENT_LEVEL_TESTNET_REDUCE:
        return

    missing = [key for key in TESTNET_REDUCE_REQUIRED_FLAGS if not _as_bool(prepared.get(key))]
    forbidden = [
        key
        for key in (
            "dynamic_exit_enabled",
            "execution_planner_enabled",
            "adaptive_execution_enabled",
        )
        if _as_bool(prepared.get(key))
    ]

    if missing or forbidden:
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="Testnet reduce inválido",
                message=(
                    f"Faltando: {', '.join(missing) or 'nenhum'}; "
                    f"não deveria estar ligado: {', '.join(forbidden) or 'nenhum'}."
                ),
            )
        )
        return

    items.append(
        IntelligentOperationReviewItem(
            status=REVIEW_STATUS_OK,
            title="Testnet reduce",
            message="Apenas sizing/redução está ativo. Stop/take adaptativo e execution planner estão desligados.",
        )
    )


def _review_testnet_block(
    items: list[IntelligentOperationReviewItem],
    *,
    prepared: dict[str, Any],
    level: str,
    execution_mode: str,
) -> None:
    if execution_mode != "TESTNET_LIVE" or level != INTELLIGENT_LEVEL_TESTNET_BLOCK:
        return

    missing = [key for key in TESTNET_BLOCK_REQUIRED_FLAGS if not _as_bool(prepared.get(key))]
    if _as_bool(prepared.get("adaptive_execution_enabled")):
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="Adaptive execution em testnet",
                message="Adaptive execution automático ainda deve ficar desligado em TESTNET_BLOCK.",
            )
        )
        return

    if missing:
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="Testnet block incompleto",
                message="TESTNET_BLOCK precisa ligar: " + ", ".join(missing),
            )
        )
        return

    items.append(
        IntelligentOperationReviewItem(
            status=REVIEW_STATUS_OK,
            title="Testnet block",
            message="Sizing, stop/take e execution planner estão ativos em testnet. Adaptive execution continua desligado.",
        )
    )


def _review_real_live(
    items: list[IntelligentOperationReviewItem],
    *,
    prepared: dict[str, Any],
    execution_mode: str,
) -> None:
    if execution_mode != "REAL_LIVE":
        return

    dangerous = [key for key in ACTIVE_EXECUTION_FLAGS if _as_bool(prepared.get(key))]
    if dangerous:
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="REAL_LIVE protegido",
                message=(
                    "REAL_LIVE não pode iniciar com camadas automáticas ativas: "
                    + ", ".join(dangerous)
                ),
            )
        )
        return

    items.append(
        IntelligentOperationReviewItem(
            status=REVIEW_STATUS_WARN,
            title="REAL_LIVE",
            message=(
                "Modo real detectado. Camadas automáticas estão bloqueadas, "
                "mas ainda exige checklist e confirmação manual."
            ),
        )
    )


def _review_adaptive_execution_scope(
    items: list[IntelligentOperationReviewItem],
    *,
    prepared: dict[str, Any],
    level: str,
    execution_mode: str,
) -> None:
    if not _as_bool(prepared.get("adaptive_execution_enabled")):
        return

    if execution_mode == "PAPER_LIVE" and level == INTELLIGENT_LEVEL_PAPER_ACTIVE:
        return

    items.append(
        IntelligentOperationReviewItem(
            status=REVIEW_STATUS_BLOCK,
            title="Adaptive execution fora de paper",
            message="Adaptive execution automático só é permitido em PAPER_ACTIVE.",
        )
    )


def _review_environment(
    items: list[IntelligentOperationReviewItem],
    *,
    execution_mode: str,
    environment: str,
) -> None:
    if execution_mode == "REAL_LIVE" and environment != "REAL":
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="Ambiente real inválido",
                message="REAL_LIVE precisa resolver ambiente efetivo REAL.",
            )
        )
        return

    if execution_mode == "TESTNET_LIVE" and environment != "TESTNET":
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="Ambiente testnet inválido",
                message="TESTNET_LIVE precisa resolver ambiente efetivo TESTNET.",
            )
        )
        return

    items.append(
        IntelligentOperationReviewItem(
            status=REVIEW_STATUS_OK,
            title="Ambiente efetivo",
            message=f"{execution_mode} usando ambiente {environment}.",
        )
    )


def _review_user_data_cache_preset(
    items: list[IntelligentOperationReviewItem],
    *,
    raw_prepared: dict[str, Any],
    prepared: dict[str, Any],
    execution_mode: str,
    environment: str,
) -> None:
    resolution = resolve_user_data_cache_preset(raw_prepared)
    active_real_cache_components = _active_user_data_cache_components(prepared)
    if execution_mode == "REAL_LIVE" and active_real_cache_components:
        items.append(
            IntelligentOperationReviewItem(
                status=REVIEW_STATUS_BLOCK,
                title="User Data Stream/cache privado",
                message=(
                    "REAL_LIVE não pode iniciar com cache-assisted real ativo: "
                    + ", ".join(active_real_cache_components)
                ),
            )
        )
        return

    reason = f" Motivo: {resolution.degrade_reason}" if resolution.degrade_reason else ""
    items.append(
        IntelligentOperationReviewItem(
            status=REVIEW_STATUS_OK,
            title="User Data Stream/cache privado",
            message=(
                f"Preset selecionado={resolution.selected_preset}; "
                f"efetivo={resolution.effective_preset}; "
                f"cache real={_format_components(resolution.cache_components)}; "
                f"REST={_format_components(resolution.rest_components)}; "
                f"execution_mode={execution_mode}; ambiente={environment}."
                f"{reason}"
            ),
        )
    )


def _active_user_data_cache_components(settings: dict[str, Any]) -> list[str]:
    components = []
    if _as_bool(settings.get("user_data_stream_cache_position_enabled")):
        components.append("position")
    if _as_bool(settings.get("user_data_stream_cache_account_enabled")):
        components.append("account_info")
    if _as_bool(settings.get("user_data_stream_cache_open_orders_enabled")):
        components.append("open_orders")
    return components


def _format_components(components: tuple[str, ...] | list[str]) -> str:
    return "/".join(components) if components else "nenhum"


def _final_status(items: list[IntelligentOperationReviewItem]) -> str:
    if any(item.status == REVIEW_STATUS_BLOCK for item in items):
        return REVIEW_STATUS_BLOCK

    if any(item.status == REVIEW_STATUS_WARN for item in items):
        return REVIEW_STATUS_WARN

    return REVIEW_STATUS_OK


def _enabled_symbols(symbol_profiles: list[dict[str, Any]]) -> list[str]:
    return [
        str(profile.get("symbol", "")).upper()
        for profile in symbol_profiles
        if bool(profile.get("enabled", True)) and str(profile.get("symbol", "")).strip()
    ]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    return str(value).strip().lower() in {"true", "1", "yes", "y", "sim"}
