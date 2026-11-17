from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research.ai.probabilistic import PROBABILISTIC_MODEL_TYPE
from research.ai.registry import validate_ai_model_file


@dataclass(frozen=True)
class AIValidationResult:
    can_start: bool
    lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    symbol_statuses: list[dict[str, str]] = field(default_factory=list)

    def message(self) -> str:
        chunks = ["Validação IA:"]
        chunks.extend(self.lines or ["IA desativada no Governor."])
        if self.warnings:
            chunks.append("")
            chunks.append("Avisos:")
            chunks.extend(f"- {warning}" for warning in self.warnings)
        if self.errors:
            chunks.append("")
            chunks.append("Erros bloqueantes:")
            chunks.extend(f"- {error}" for error in self.errors)
        return "\n".join(chunks)


def validate_probabilistic_ai_start(
    settings: dict[str, Any],
    symbol_profiles: list[dict[str, Any]],
) -> AIValidationResult:
    strategy_mode = str(settings.get("strategy_selection_mode", "FIXED")).upper()
    ai_enabled = _as_bool(settings.get("ai_probabilistic_enabled", False))
    if strategy_mode != "PROBABILISTIC_AI" and not ai_enabled:
        return AIValidationResult(can_start=True, lines=["IA desativada no Governor."])

    errors: list[str] = []
    warnings: list[str] = []
    lines: list[str] = []
    symbol_statuses: list[dict[str, str]] = []
    _validate_thresholds(settings, errors)

    global_path_text = str(settings.get("ai_global_model_path") or "").strip()
    global_path = Path(global_path_text)
    global_metadata: dict[str, Any] | None = None
    if not global_path_text:
        errors.append("Modelo global IA não informado.")
    else:
        try:
            global_metadata = _validate_model(
                global_path,
                expected_scope="GLOBAL",
                expected_symbol="GLOBAL",
            )
            if not _path_has_segment(global_path, "global"):
                errors.append(f"Modelo global IA deve ficar em models/ai/global: {global_path}")
            else:
                lines.append(f"Modelo global: encontrado em {global_path}")
        except Exception as error:
            errors.append(f"Modelo global IA inválido: {error}")

    fallback_enabled = _as_bool(settings.get("ai_fallback_to_global_model", True))
    enabled_profiles = [
        profile
        for profile in symbol_profiles
        if _as_bool(profile.get("enabled", True)) and str(profile.get("symbol", "")).strip()
    ]
    if not enabled_profiles:
        warnings.append("Nenhuma moeda habilitada para validar modelo IA.")

    for profile in enabled_profiles:
        symbol = str(profile.get("symbol", "")).upper().strip()
        refined_path_text = str(profile.get("ai_refined_model_path", "")).strip()
        refined_path = Path(refined_path_text)
        use_refined = _as_bool(profile.get("ai_use_symbol_refined_model", True))
        if use_refined and refined_path_text:
            try:
                _validate_model(
                    refined_path,
                    expected_scope="SYMBOL_REFINED",
                    expected_symbol=symbol,
                )
                if not _path_has_segment(refined_path, "refined"):
                    errors.append(
                        f"{symbol}: modelo refinado deve ficar em models/ai/refined/{symbol}."
                    )
                elif symbol not in {part.upper() for part in refined_path.parts}:
                    errors.append(
                        f"{symbol}: caminho do modelo refinado não inclui a pasta da moeda."
                    )
                else:
                    lines.append(f"{symbol}: modelo refinado encontrado.")
                    symbol_statuses.append(
                        {
                            "symbol": symbol,
                            "status": "SYMBOL_REFINED",
                            "path": str(refined_path),
                        }
                    )
                    continue
            except Exception as error:
                errors.append(f"{symbol}: modelo refinado inválido: {error}")
                symbol_statuses.append(
                    {
                        "symbol": symbol,
                        "status": "BLOCKED",
                        "path": str(refined_path),
                    }
                )
                continue

        if fallback_enabled and global_metadata is not None:
            lines.append(f"{symbol}: usando fallback global.")
            symbol_statuses.append(
                {"symbol": symbol, "status": "GLOBAL_FALLBACK", "path": str(global_path)}
            )
            continue

        reason = "sem modelo refinado e sem modelo global válido"
        if not fallback_enabled:
            reason = "sem modelo refinado e fallback global desativado"
        errors.append(f"{symbol}: bloqueado — {reason}.")
        symbol_statuses.append({"symbol": symbol, "status": "BLOCKED", "path": ""})

    return AIValidationResult(
        can_start=not errors,
        lines=lines,
        errors=errors,
        warnings=warnings,
        symbol_statuses=symbol_statuses,
    )


def _validate_thresholds(settings: dict[str, Any], errors: list[str]) -> None:
    probability_keys = (
        "ai_min_long_probability",
        "ai_min_short_probability",
        "ai_min_direction_gap",
        "ai_close_long_min_short_probability",
        "ai_close_short_min_long_probability",
    )
    for key in probability_keys:
        try:
            value = float(settings.get(key, 0))
        except (TypeError, ValueError):
            errors.append(f"{key} precisa ser número entre 0 e 1.")
            continue
        if not 0.0 <= value <= 1.0:
            errors.append(f"{key} precisa ficar entre 0 e 1.")

    try:
        expected_value = float(settings.get("ai_min_expected_value_bps", 0))
    except (TypeError, ValueError):
        errors.append("ai_min_expected_value_bps precisa ser número.")
        return
    if expected_value < 0:
        errors.append("ai_min_expected_value_bps não pode ser negativo.")


def _validate_model(
    path: Path,
    *,
    expected_scope: str,
    expected_symbol: str,
) -> dict[str, Any]:
    metadata = validate_ai_model_file(path)
    model_type = str(metadata.get("model_type", ""))
    if model_type != PROBABILISTIC_MODEL_TYPE:
        raise ValueError(f"model_type={model_type}; esperado {PROBABILISTIC_MODEL_TYPE}")
    scope = str(metadata.get("scope", "")).upper().strip()
    if scope != expected_scope:
        raise ValueError(f"scope={scope}; esperado {expected_scope}")
    symbol = str(metadata.get("symbol", "")).upper().strip()
    if symbol != expected_symbol:
        raise ValueError(f"symbol={symbol}; esperado {expected_symbol}")
    return metadata


def _path_has_segment(path: Path, segment: str) -> bool:
    wanted = segment.lower()
    return any(part.lower() == wanted for part in path.parts)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "y", "sim"}
