from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.settings_store import (
    ARCHITECTURE_INTELLIGENT_FLOW,
    INTELLIGENT_LEVEL_SHADOW_ONLY,
    allowed_intelligent_activation_levels,
    apply_architecture_preset,
    normalize_environment,
)

INTERFACE_PROCESS_STATUS_READY = "READY"
INTERFACE_PROCESS_STATUS_WARNING = "WARNING"
INTERFACE_PROCESS_STATUS_BLOCKED = "BLOCKED"
INTERFACE_PROCESS_STATUS_INFO = "INFO"


@dataclass(frozen=True)
class InterfaceProcessReviewItem:
    process: str
    status: str
    message: str


@dataclass(frozen=True)
class InterfaceProcessReview:
    status: str
    enabled_symbols_count: int
    effective_operation_mode: str
    items: list[InterfaceProcessReviewItem]


def build_interface_process_review(
    *,
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
    root_path: Path | str = ".",
) -> InterfaceProcessReview:
    prepared = apply_architecture_preset(settings)
    root = Path(root_path)
    enabled_symbols = _enabled_symbols(symbol_profiles)
    enabled_count = len(enabled_symbols)
    effective_mode = _effective_operation_mode(enabled_count)

    items = [
        _review_start_single(enabled_count),
        _review_start_multi(enabled_count),
        _review_save_settings(prepared),
        _review_execution_mode_change(prepared),
        _review_intelligent_level_change(prepared),
        _review_reload_journal(prepared, symbol_profiles, root),
        _review_reload_intelligent_events(prepared, symbol_profiles, root),
        _review_open_runtime(root),
        _review_open_state(prepared, symbol_profiles, root),
        _review_open_journal(prepared, symbol_profiles, root),
    ]

    return InterfaceProcessReview(
        status=_final_status(items),
        enabled_symbols_count=enabled_count,
        effective_operation_mode=effective_mode,
        items=items,
    )


def format_interface_process_review(review: InterfaceProcessReview) -> str:
    lines = [
        "Revisão dos processos da interface",
        "",
        f"Status geral: {review.status}",
        f"Modo efetivo: {review.effective_operation_mode}",
        f"Moedas habilitadas: {review.enabled_symbols_count}",
        "",
        "Fluxos:",
    ]

    for item in review.items:
        lines.append(f"- [{item.status}] {item.process}: {item.message}")

    return "\n".join(lines)


def interface_journal_paths(
    *,
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
) -> list[Path]:
    prepared = apply_architecture_preset(settings)
    environment = normalize_environment(prepared).lower()

    paths = [
        Path("data/governor_events.jsonl"),
        Path("data/scanner_scores.jsonl"),
        Path("data/trades.jsonl"),
    ]

    for profile in _enabled_profiles(symbol_profiles):
        symbol = str(profile.get("symbol", "")).lower().strip()
        if not symbol:
            continue
        paths.append(Path("data") / f"trades_{symbol}_{environment}.jsonl")
        paths.append(Path("data") / f"paper_trades_{symbol}_{environment}.jsonl")

    return _unique_paths(paths)


def interface_state_paths(
    *,
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
) -> list[Path]:
    prepared = apply_architecture_preset(settings)
    environment = normalize_environment(prepared).lower()

    paths = [
        Path("runtime/governor_state.json"),
        Path("runtime/state.json"),
    ]

    for profile in _enabled_profiles(symbol_profiles):
        symbol = str(profile.get("symbol", "")).lower().strip()
        if symbol:
            paths.append(Path("runtime") / f"state_{symbol}_{environment}.json")

    return _unique_paths(paths)


def first_existing_path(paths: list[Path], *, root_path: Path | str = ".") -> Path | None:
    root = Path(root_path)

    for path in paths:
        candidate = path if path.is_absolute() else root / path
        if candidate.exists():
            return path

    return None


def _review_start_single(enabled_count: int) -> InterfaceProcessReviewItem:
    if enabled_count == 0:
        return InterfaceProcessReviewItem(
            process="Iniciar Alphora single-symbol",
            status=INTERFACE_PROCESS_STATUS_BLOCKED,
            message="Nenhuma moeda habilitada. Single-symbol não pode iniciar.",
        )

    if enabled_count == 1:
        return InterfaceProcessReviewItem(
            process="Iniciar Alphora single-symbol",
            status=INTERFACE_PROCESS_STATUS_READY,
            message="Uma moeda habilitada. A interface iniciará Governor single-symbol.",
        )

    return InterfaceProcessReviewItem(
        process="Iniciar Alphora single-symbol",
        status=INTERFACE_PROCESS_STATUS_INFO,
        message="Mais de uma moeda habilitada. A interface usará multi-symbol.",
    )


def _review_start_multi(enabled_count: int) -> InterfaceProcessReviewItem:
    if enabled_count == 0:
        return InterfaceProcessReviewItem(
            process="Iniciar Alphora multi-symbol",
            status=INTERFACE_PROCESS_STATUS_BLOCKED,
            message="Nenhuma moeda habilitada. Multi-symbol não pode iniciar.",
        )

    if enabled_count >= 2:
        return InterfaceProcessReviewItem(
            process="Iniciar Alphora multi-symbol",
            status=INTERFACE_PROCESS_STATUS_READY,
            message=f"{enabled_count} moedas habilitadas. A interface iniciará Governor multi-symbol.",
        )

    return InterfaceProcessReviewItem(
        process="Iniciar Alphora multi-symbol",
        status=INTERFACE_PROCESS_STATUS_INFO,
        message="Apenas uma moeda habilitada. A interface usará single-symbol.",
    )


def _review_save_settings(settings: dict[str, Any]) -> InterfaceProcessReviewItem:
    architecture = str(settings.get("architecture_mode", ARCHITECTURE_INTELLIGENT_FLOW)).upper()
    level = str(settings.get("intelligent_activation_level", INTELLIGENT_LEVEL_SHADOW_ONLY))
    environment = normalize_environment(settings)

    return InterfaceProcessReviewItem(
        process="Salvar configurações",
        status=INTERFACE_PROCESS_STATUS_READY,
        message=(
            f"Salvar aplica preset efetivo: arquitetura={architecture}, "
            f"nível={level}, ambiente={environment}."
        ),
    )


def _review_execution_mode_change(settings: dict[str, Any]) -> InterfaceProcessReviewItem:
    execution_mode = str(settings.get("execution_mode", "DRY_RUN")).upper()
    allowed = allowed_intelligent_activation_levels(execution_mode)

    return InterfaceProcessReviewItem(
        process="Trocar execution mode",
        status=INTERFACE_PROCESS_STATUS_READY,
        message=(
            f"Execution mode atual: {execution_mode}. "
            f"Níveis inteligentes permitidos: {', '.join(allowed)}."
        ),
    )


def _review_intelligent_level_change(settings: dict[str, Any]) -> InterfaceProcessReviewItem:
    execution_mode = str(settings.get("execution_mode", "DRY_RUN")).upper()
    level = str(settings.get("intelligent_activation_level", INTELLIGENT_LEVEL_SHADOW_ONLY))
    allowed = allowed_intelligent_activation_levels(execution_mode)

    if level not in allowed:
        return InterfaceProcessReviewItem(
            process="Trocar nível inteligente",
            status=INTERFACE_PROCESS_STATUS_BLOCKED,
            message=(
                f"Nível {level} não é permitido em {execution_mode}. "
                f"Permitidos: {', '.join(allowed)}."
            ),
        )

    return InterfaceProcessReviewItem(
        process="Trocar nível inteligente",
        status=INTERFACE_PROCESS_STATUS_READY,
        message=f"Nível {level} compatível com {execution_mode}.",
    )


def _review_reload_journal(
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
    root: Path,
) -> InterfaceProcessReviewItem:
    paths = interface_journal_paths(settings=settings, symbol_profiles=symbol_profiles)
    existing = _existing_count(paths, root)

    status = INTERFACE_PROCESS_STATUS_READY if existing else INTERFACE_PROCESS_STATUS_WARNING
    return InterfaceProcessReviewItem(
        process="Recarregar journal",
        status=status,
        message=f"{existing}/{len(paths)} arquivo(s) de journal encontrados.",
    )


def _review_reload_intelligent_events(
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
    root: Path,
) -> InterfaceProcessReviewItem:
    paths = interface_journal_paths(settings=settings, symbol_profiles=symbol_profiles)
    existing = _existing_count(paths, root)

    status = INTERFACE_PROCESS_STATUS_READY if existing else INTERFACE_PROCESS_STATUS_WARNING
    return InterfaceProcessReviewItem(
        process="Recarregar decisões inteligentes",
        status=status,
        message=(
            f"{existing}/{len(paths)} arquivo(s) disponíveis para extrair decisões "
            "inteligentes recentes."
        ),
    )


def _review_open_runtime(root: Path) -> InterfaceProcessReviewItem:
    path = root / "runtime"
    if path.exists():
        return InterfaceProcessReviewItem(
            process="Abrir runtime",
            status=INTERFACE_PROCESS_STATUS_READY,
            message="Pasta runtime encontrada.",
        )

    return InterfaceProcessReviewItem(
        process="Abrir runtime",
        status=INTERFACE_PROCESS_STATUS_WARNING,
        message="Pasta runtime ainda não existe. Ela será criada quando o Alphora rodar.",
    )


def _review_open_state(
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
    root: Path,
) -> InterfaceProcessReviewItem:
    paths = interface_state_paths(settings=settings, symbol_profiles=symbol_profiles)
    existing = _existing_count(paths, root)

    status = INTERFACE_PROCESS_STATUS_READY if existing else INTERFACE_PROCESS_STATUS_WARNING
    return InterfaceProcessReviewItem(
        process="Abrir state",
        status=status,
        message=f"{existing}/{len(paths)} arquivo(s) de estado encontrados.",
    )


def _review_open_journal(
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
    root: Path,
) -> InterfaceProcessReviewItem:
    paths = interface_journal_paths(settings=settings, symbol_profiles=symbol_profiles)
    existing = _existing_count(paths, root)

    status = INTERFACE_PROCESS_STATUS_READY if existing else INTERFACE_PROCESS_STATUS_WARNING
    return InterfaceProcessReviewItem(
        process="Abrir journal",
        status=status,
        message=f"{existing}/{len(paths)} arquivo(s) de journal encontrados.",
    )


def _enabled_profiles(symbol_profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        profile
        for profile in symbol_profiles
        if bool(profile.get("enabled", True)) and str(profile.get("symbol", "")).strip()
    ]


def _enabled_symbols(symbol_profiles: list[dict[str, Any]]) -> list[str]:
    return [
        str(profile.get("symbol", "")).upper() for profile in _enabled_profiles(symbol_profiles)
    ]


def _effective_operation_mode(enabled_count: int) -> str:
    if enabled_count == 0:
        return "NO_ENABLED_SYMBOLS"
    if enabled_count == 1:
        return "GOVERNOR_SINGLE_SYMBOL"
    return "GOVERNOR_MULTI_SYMBOL"


def _existing_count(paths: list[Path], root: Path) -> int:
    return sum(1 for path in paths if (path if path.is_absolute() else root / path).exists())


def _final_status(items: list[InterfaceProcessReviewItem]) -> str:
    if any(item.status == INTERFACE_PROCESS_STATUS_BLOCKED for item in items):
        return INTERFACE_PROCESS_STATUS_BLOCKED

    if any(item.status == INTERFACE_PROCESS_STATUS_WARNING for item in items):
        return INTERFACE_PROCESS_STATUS_WARNING

    return INTERFACE_PROCESS_STATUS_READY


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique = []
    seen = set()

    for path in paths:
        key = path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)

    return unique
