from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.subprocess_protocol import decode_task_event
from research.ai.registry import validate_ai_model_file
from research.ai.training_defaults import DEFAULT_ESTIMATED_ROUND_TRIP_COST_BPS

TRAINING_GLOBAL = "GLOBAL"
TRAINING_REFINED = "REFINED"
TRAINING_ALL = "ALL"
TRAINING_KINDS = (TRAINING_GLOBAL, TRAINING_REFINED, TRAINING_ALL)


@dataclass(frozen=True, slots=True)
class AITrainingPlan:
    kind: str
    command: tuple[str, ...]
    symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AITrainingResult:
    global_model_path: str = ""
    symbol_model_paths: tuple[tuple[str, str], ...] = ()
    summary: str = ""

    def symbol_models(self) -> dict[str, str]:
        return dict(self.symbol_model_paths)


_TRAINING_KEYS = (
    "ai_training_global_symbols",
    "ai_training_refined_symbol",
    "ai_training_interval",
    "ai_training_start",
    "ai_training_end",
    "ai_training_horizon_candles",
    "ai_training_threshold_bps",
    "ai_training_min_training_rows",
    "ai_training_csv_path",
    "ai_training_csv_map",
    "ai_training_global_output_dir",
    "ai_training_refined_output_dir",
    "ai_training_download_if_missing",
)


def normalize_ai_training_settings(
    settings: Mapping[str, Any], patch: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    values = dict(settings)
    if patch:
        values.update(dict(patch))

    normalized: dict[str, Any] = {}
    for key in _TRAINING_KEYS:
        value = values.get(key, "")
        normalized[key] = (
            _as_bool(value) if key == "ai_training_download_if_missing" else str(value)
        )

    normalized["ai_training_global_symbols"] = ",".join(
        parse_symbol_text(normalized["ai_training_global_symbols"])
    )
    refined = parse_symbol_text(normalized["ai_training_refined_symbol"])
    normalized["ai_training_refined_symbol"] = refined[0] if refined else ""
    normalized["ai_training_interval"] = normalized["ai_training_interval"].strip()
    normalized["ai_training_start"] = normalized["ai_training_start"].strip()
    normalized["ai_training_end"] = normalized["ai_training_end"].strip()
    normalized["ai_training_csv_path"] = normalized["ai_training_csv_path"].strip()
    normalized["ai_training_csv_map"] = normalized["ai_training_csv_map"].strip()
    normalized["ai_training_global_output_dir"] = normalized[
        "ai_training_global_output_dir"
    ].strip()
    normalized["ai_training_refined_output_dir"] = normalized[
        "ai_training_refined_output_dir"
    ].strip()
    return normalized


def parse_symbol_text(value: str) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for item in str(value or "").replace(";", ",").split(","):
        symbol = item.upper().strip()
        if not symbol or symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
    return symbols


def enabled_training_symbols(
    settings: Mapping[str, Any], profiles: Sequence[Mapping[str, Any]]
) -> list[str]:
    configured = parse_symbol_text(str(settings.get("ai_training_global_symbols", "")))
    if configured:
        return configured

    symbols = [
        str(profile.get("symbol", "")).upper().strip()
        for profile in profiles
        if _as_bool(profile.get("enabled", True)) and str(profile.get("symbol", "")).strip()
    ]
    if symbols:
        return list(dict.fromkeys(symbols))

    fallback = str(settings.get("ai_training_symbol", settings.get("symbol", ""))).upper().strip()
    return [fallback] if fallback else []


def validate_ai_training_settings(
    settings: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    project_root: str | Path,
) -> list[str]:
    normalized_kind = _normalize_kind(kind)
    values = normalize_ai_training_settings(settings)
    root = Path(project_root)
    symbols = _symbols_for_kind(values, profiles, normalized_kind)
    errors: list[str] = []

    if not symbols:
        errors.append("Informe ao menos um símbolo para o treinamento.")
    if normalized_kind == TRAINING_REFINED and len(symbols) != 1:
        errors.append("O treinamento refinado exige exatamente um símbolo.")
    if not values["ai_training_interval"]:
        errors.append("Informe o intervalo dos candles.")

    _validate_positive_integer(values["ai_training_horizon_candles"], "Horizon candles", errors)
    _validate_non_negative_number(values["ai_training_threshold_bps"], "Threshold bps", errors)
    _validate_threshold_above_cost(values["ai_training_threshold_bps"], errors)
    _validate_positive_integer(values["ai_training_min_training_rows"], "Mínimo de linhas", errors)

    if normalized_kind == TRAINING_REFINED:
        global_model = str(settings.get("ai_global_model_path", "")).strip()
        if not global_model:
            errors.append("Selecione ou treine primeiro um modelo global de IA.")
        else:
            model_path = _resolve_path(root, global_model)
            if not model_path.is_file():
                errors.append(f"Modelo global não encontrado: {global_model}")
            else:
                try:
                    metadata = validate_ai_model_file(model_path)
                except Exception as error:
                    errors.append(f"Modelo global inválido: {_safe_text(error)}")
                else:
                    if str(metadata.get("scope", "")).upper() != "GLOBAL":
                        errors.append("O modelo selecionado não possui escopo GLOBAL.")

    csv_map = values["ai_training_csv_map"]
    csv_path = values["ai_training_csv_path"]
    download = _as_bool(values["ai_training_download_if_missing"])
    if csv_map:
        errors.extend(_validate_csv_map(csv_map, root, symbols))
    elif csv_path:
        if len(symbols) != 1:
            errors.append("Um CSV único só pode ser usado com exatamente um símbolo.")
        elif not _resolve_path(root, csv_path).is_file():
            errors.append(f"CSV de treinamento não encontrado: {csv_path}")
    elif not download:
        errors.append("Informe um CSV/mapa de CSV ou habilite o download automático.")
    elif not values["ai_training_start"] or not values["ai_training_end"]:
        errors.append("Informe as datas inicial e final para o download automático.")

    return errors


def prepare_ai_training_plan(
    settings: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    project_root: str | Path,
    python_executable: str | None = None,
) -> AITrainingPlan:
    normalized_kind = _normalize_kind(kind)
    errors = validate_ai_training_settings(
        settings,
        profiles,
        kind=normalized_kind,
        project_root=project_root,
    )
    if errors:
        raise ValueError(errors[0])

    values = normalize_ai_training_settings(settings)
    symbols = _symbols_for_kind(values, profiles, normalized_kind)
    command_name = {
        TRAINING_GLOBAL: "train-ai-probabilistic-global",
        TRAINING_REFINED: "train-ai-probabilistic-refined",
        TRAINING_ALL: "train-ai-probabilistic-all",
    }[normalized_kind]
    command = [
        python_executable or sys.executable,
        "-u",
        "-m",
        "research.cli",
        command_name,
        "--symbols",
        ",".join(symbols),
        "--interval",
        values["ai_training_interval"],
        "--horizon-candles",
        values["ai_training_horizon_candles"],
        "--threshold-bps",
        values["ai_training_threshold_bps"],
        "--min-training-rows",
        values["ai_training_min_training_rows"],
    ]

    if normalized_kind == TRAINING_REFINED:
        command.extend(["--global-model", str(settings.get("ai_global_model_path", "")).strip()])
        output_dir = values["ai_training_refined_output_dir"]
        if output_dir:
            command.extend(["--output-dir", output_dir])
    elif normalized_kind == TRAINING_GLOBAL:
        output_dir = values["ai_training_global_output_dir"]
        if output_dir:
            command.extend(["--output-dir", output_dir])
    else:
        global_output = values["ai_training_global_output_dir"]
        refined_output = values["ai_training_refined_output_dir"]
        if global_output:
            command.extend(["--global-output-dir", global_output])
        if refined_output:
            command.extend(["--output-dir", refined_output])

    _append_training_data_args(command, values, symbols)
    return AITrainingPlan(
        kind=normalized_kind,
        command=tuple(command),
        symbols=tuple(symbols),
    )


def parse_ai_training_result(
    output_lines: Sequence[str], *, project_root: str | Path
) -> AITrainingResult:
    root = Path(project_root)
    global_model = ""
    symbol_models: dict[str, str] = {}

    for raw_line in output_lines:
        event = decode_task_event(str(raw_line))
        if event is None:
            continue
        if event["type"] == "ai_model":
            path = event.get("path")
            if isinstance(path, str) and path.strip():
                global_model = _portable_path(root, path.strip())
            continue
        if event["type"] == "ai_symbol_model":
            symbol = str(event.get("symbol", "")).upper().strip()
            if symbol and event.get("status") == "OK" and event.get("path"):
                symbol_models[symbol] = _portable_path(root, str(event["path"]))

    if global_model:
        model_path = _resolve_path(root, global_model)
        if not model_path.is_file():
            raise ValueError(
                f"O modelo global informado pela tarefa não foi encontrado: {global_model}"
            )
        validate_ai_model_file(model_path)

    if not global_model and not symbol_models:
        raise ValueError("A tarefa terminou sem informar modelos gerados.")

    lines = ["Treinamento concluído."]
    if global_model:
        lines.append(f"Modelo global: {global_model}")
    if symbol_models:
        lines.append("Modelos refinados:")
        lines.extend(f"- {symbol}: {path}" for symbol, path in sorted(symbol_models.items()))
    return AITrainingResult(
        global_model_path=global_model,
        symbol_model_paths=tuple(sorted(symbol_models.items())),
        summary="\n".join(lines),
    )


def apply_ai_training_result(
    settings: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    result: AITrainingResult,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    updated_settings = dict(settings)
    updated_profiles = [dict(profile) for profile in profiles]

    if result.global_model_path:
        updated_settings["ai_global_model_path"] = result.global_model_path
        updated_settings["ai_probabilistic_enabled"] = True
        updated_settings["strategy_selection_mode"] = "PROBABILISTIC_AI"

    symbol_models = result.symbol_models()
    if symbol_models:
        configured = dict(updated_settings.get("ai_symbol_model_paths", {}))
        configured.update(symbol_models)
        updated_settings["ai_symbol_model_paths"] = configured
        for profile in updated_profiles:
            symbol = str(profile.get("symbol", "")).upper().strip()
            if symbol not in symbol_models:
                continue
            profile["ai_use_symbol_refined_model"] = True
            profile["ai_refined_model_path"] = symbol_models[symbol]

    return updated_settings, updated_profiles


def summarize_selected_model(
    settings: Mapping[str, Any], *, project_root: str | Path
) -> dict[str, Any]:
    configured = str(settings.get("ai_global_model_path", "")).strip()
    if not configured:
        return {
            "configured": False,
            "path": "",
            "status": "Nenhum modelo global selecionado",
            "tone": "neutral",
            "details": "",
        }

    path = _resolve_path(Path(project_root), configured)
    try:
        metadata = validate_ai_model_file(path)
    except Exception as error:
        return {
            "configured": True,
            "path": configured,
            "status": "Modelo inválido",
            "tone": "danger",
            "details": _safe_text(error),
        }

    metrics = metadata.get("metrics", {}) if isinstance(metadata, Mapping) else {}
    details = " | ".join(
        [
            f"scope={metadata.get('scope', '-')}",
            f"interval={metadata.get('interval', '-')}",
            f"rows={metadata.get('training_rows', 0)}",
            f"accuracy={metrics.get('accuracy', 'n/a')}",
        ]
    )
    return {
        "configured": True,
        "path": configured,
        "status": "Modelo global válido",
        "tone": "success",
        "details": details,
    }


def _normalize_kind(kind: str) -> str:
    normalized = str(kind or "").upper().strip()
    if normalized not in TRAINING_KINDS:
        raise ValueError(f"Tipo de treinamento inválido: {kind}")
    return normalized


def _symbols_for_kind(
    settings: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    kind: str,
) -> list[str]:
    if kind == TRAINING_REFINED:
        return parse_symbol_text(str(settings.get("ai_training_refined_symbol", "")))
    return enabled_training_symbols(settings, profiles)


def _append_training_data_args(
    command: list[str], settings: Mapping[str, Any], symbols: Sequence[str]
) -> None:
    csv_map = str(settings.get("ai_training_csv_map", "")).strip()
    csv_path = str(settings.get("ai_training_csv_path", "")).strip()
    if csv_map:
        command.extend(["--csv-map", csv_map])
        return
    if csv_path:
        command.extend(["--csv-map", f"{symbols[0]}={csv_path}"])
        return
    command.extend(
        [
            "--start",
            str(settings.get("ai_training_start", "")).strip(),
            "--end",
            str(settings.get("ai_training_end", "")).strip(),
        ]
    )


def _validate_csv_map(csv_map: str, root: Path, symbols: Sequence[str]) -> list[str]:
    errors: list[str] = []
    mapped: dict[str, str] = {}
    for item in str(csv_map).replace(";", ",").split(","):
        value = item.strip()
        if not value:
            continue
        if "=" not in value:
            errors.append(f"Entrada inválida no mapa de CSV: {value}")
            continue
        symbol, path = value.split("=", 1)
        normalized_symbol = symbol.upper().strip()
        normalized_path = path.strip()
        if not normalized_symbol or not normalized_path:
            errors.append(f"Entrada inválida no mapa de CSV: {value}")
            continue
        mapped[normalized_symbol] = normalized_path
        if not _resolve_path(root, normalized_path).is_file():
            errors.append(f"CSV não encontrado para {normalized_symbol}: {normalized_path}")
    for symbol in symbols:
        if symbol not in mapped:
            errors.append(f"O mapa de CSV não contém o símbolo {symbol}.")
    return errors


def _validate_positive_integer(value: Any, label: str, errors: list[str]) -> None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        errors.append(f"{label} deve ser um número inteiro.")
        return
    if number <= 0:
        errors.append(f"{label} deve ser maior que zero.")


def _validate_non_negative_number(value: Any, label: str, errors: list[str]) -> None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        errors.append(f"{label} deve ser numérico.")
        return
    if number < 0:
        errors.append(f"{label} não pode ser negativo.")


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _portable_path(root: Path, value: str) -> str:
    path = _resolve_path(root, value).resolve()
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "sim"}


def _safe_text(error: BaseException) -> str:
    text = str(error).strip()
    return text or error.__class__.__name__


def _validate_threshold_above_cost(raw_threshold: str, errors: list[str]) -> None:
    try:
        threshold = float(raw_threshold)
    except ValueError:
        return
    if threshold <= DEFAULT_ESTIMATED_ROUND_TRIP_COST_BPS:
        errors.append(
            "Threshold bps precisa ser maior que o custo estimado de ida e volta "
            f"({DEFAULT_ESTIMATED_ROUND_TRIP_COST_BPS:g} bps)."
        )
