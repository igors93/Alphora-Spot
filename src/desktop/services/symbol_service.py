from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from desktop.settings_store import (
    DEFAULT_PORTFOLIO_RISK_SETTINGS,
    DEFAULT_SYMBOL_PROFILE,
    profile_from_simple_settings,
)

ModelValidator = Callable[[str | Path], Mapping[str, Any]]
Sanitizer = Callable[[Any], Any]

_INTEGER_PROFILE_FIELDS = {
    "leverage": (1, 125),
    "atr_period": (1, None),
    "max_daily_trades": (0, None),
    "max_consecutive_losses": (0, None),
    "cooldown_after_loss_seconds": (0, None),
    "max_position_hold_seconds": (0, None),
}

_DECIMAL_PROFILE_FIELDS = {
    "position_notional_usdt": (Decimal("0"), False),
    "stop_loss_pct": (Decimal("0"), True),
    "take_profit_pct": (Decimal("0"), True),
    "atr_stop_multiplier": (Decimal("0"), False),
    "atr_take_multiplier": (Decimal("0"), False),
    "max_daily_loss_usdt": (Decimal("0"), True),
    "max_exposure_usdt": (Decimal("0"), True),
}

_INTEGER_PORTFOLIO_FIELDS = {
    "max_open_positions": (1, None),
    "max_correlated_positions": (0, None),
    "max_new_positions_per_cycle": (1, None),
    "governor_max_symbols_per_cycle": (1, None),
}

_DECIMAL_PORTFOLIO_FIELDS = {
    "max_total_exposure_usdt",
    "max_long_exposure_usdt",
    "max_short_exposure_usdt",
    "max_symbol_exposure_usdt",
    "max_group_exposure_usdt",
    "max_daily_loss_usdt",
    "stop_after_daily_profit_usdt",
}


@dataclass(frozen=True, slots=True)
class GovernorStateSnapshot:
    payload: dict[str, Any]
    available: bool
    updated_at: str
    error: str = ""


def load_governor_state(
    path: str | Path,
    *,
    sanitizer: Sanitizer | None = None,
) -> GovernorStateSnapshot:
    state_path = Path(path)
    if not state_path.is_file():
        return GovernorStateSnapshot(payload={}, available=False, updated_at="-")

    safe = sanitizer or _default_sanitizer()
    try:
        with state_path.open("r", encoding="utf-8") as file:
            raw_payload = json.load(file)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        return GovernorStateSnapshot(
            payload={},
            available=False,
            updated_at=_format_timestamp(state_path),
            error=_safe_text(safe, error),
        )

    if not isinstance(raw_payload, dict):
        return GovernorStateSnapshot(
            payload={},
            available=False,
            updated_at=_format_timestamp(state_path),
            error="O estado do Governor precisa conter um objeto JSON.",
        )

    try:
        sanitized = safe(raw_payload)
    except Exception:
        sanitized = None
    if not isinstance(sanitized, dict):
        return GovernorStateSnapshot(
            payload={},
            available=False,
            updated_at=_format_timestamp(state_path),
            error="Não foi possível sanitizar o estado do Governor.",
        )

    return GovernorStateSnapshot(
        payload=sanitized,
        available=True,
        updated_at=_format_timestamp(state_path),
    )


def build_symbol_rows(
    profiles: Sequence[Mapping[str, Any]],
    snapshot: GovernorStateSnapshot,
    settings: Mapping[str, Any],
    *,
    model_validator: ModelValidator | None = None,
) -> list[dict[str, Any]]:
    symbols_state = snapshot.payload.get("symbols", {})
    if not isinstance(symbols_state, Mapping):
        symbols_state = {}

    rows: list[dict[str, Any]] = []
    for index, profile_value in enumerate(profiles):
        profile = normalize_symbol_profile(profile_value)
        symbol = str(profile.get("symbol", "")).upper().strip()
        runtime = symbols_state.get(symbol, {}) if symbol else {}
        if not isinstance(runtime, Mapping):
            runtime = {}

        enabled = _as_bool(profile.get("enabled", True), default=True)
        status = str(runtime.get("status", "-")).strip() or "-"
        if not enabled:
            status = "DISABLED"
        priority = runtime.get("priority", "-")
        if isinstance(priority, Mapping):
            priority = priority.get("value", "-")

        error = str(runtime.get("error", "") or "")
        ai_status = resolve_profile_ai_status(
            profile,
            settings,
            model_validator=model_validator,
        )
        rows.append(
            {
                "profile_index": index,
                "enabled": enabled,
                "enabled_text": "Sim" if enabled else "Não",
                "symbol": symbol or "-",
                "status": status,
                "priority": str(priority or "-"),
                "score": str(runtime.get("score", "0")),
                "last_price": str(runtime.get("last_price", "0")),
                "position": str(runtime.get("position_direction", "NONE")),
                "quantity": str(runtime.get("position_quantity", "0")),
                "entry_price": str(runtime.get("entry_price", "0")),
                "unrealized_pnl": str(runtime.get("unrealized_pnl", "0")),
                "notional": str(profile.get("position_notional_usdt", "")),
                "leverage": str(profile.get("leverage", "")),
                "ai_model": ai_status,
                "next_check": str(runtime.get("next_check_at", "")),
                "reason": str(runtime.get("reason", "")),
                "error": error,
                "tone": _row_tone(enabled=enabled, status=status, error=error),
            }
        )
    return rows


def normalize_symbol_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(DEFAULT_SYMBOL_PROFILE)
    normalized.update(deepcopy(dict(profile)))
    normalized["enabled"] = _as_bool(normalized.get("enabled", True), default=True)
    normalized["symbol"] = str(normalized.get("symbol", "")).upper().strip()
    normalized["margin_type"] = str(normalized.get("margin_type", "ISOLATED")).upper().strip()
    normalized["exit_model"] = str(normalized.get("exit_model", "FIXED_PCT")).upper().strip()
    normalized["protection_policy"] = (
        str(normalized.get("protection_policy", "AUTO_REPAIR")).upper().strip()
    )
    normalized["protection_working_type"] = (
        str(normalized.get("protection_working_type", "MARK_PRICE")).upper().strip()
    )
    normalized["recovery_action"] = (
        str(normalized.get("recovery_action", "PROTECT")).upper().strip()
    )
    normalized["ai_use_symbol_refined_model"] = _as_bool(
        normalized.get("ai_use_symbol_refined_model", True),
        default=True,
    )
    normalized["ai_refined_model_path"] = str(normalized.get("ai_refined_model_path", "")).strip()
    return normalized


def new_symbol_profile(settings: Mapping[str, Any]) -> dict[str, Any]:
    profile = profile_from_simple_settings(dict(settings))
    profile["symbol"] = ""
    profile["enabled"] = True
    return normalize_symbol_profile(profile)


def duplicate_symbol_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    duplicated = normalize_symbol_profile(profile)
    duplicated["symbol"] = ""
    duplicated["enabled"] = False
    return duplicated


def validate_symbol_profile(
    profile: Mapping[str, Any],
    existing_profiles: Sequence[Mapping[str, Any]],
    *,
    exclude_index: int | None = None,
    model_validator: ModelValidator | None = None,
) -> tuple[str, ...]:
    normalized = normalize_symbol_profile(profile)
    errors: list[str] = []

    symbol = normalized["symbol"]
    if not symbol:
        errors.append("Informe o símbolo antes de salvar.")
    elif not symbol.isalnum():
        errors.append("O símbolo deve conter apenas letras e números.")
    else:
        for index, existing in enumerate(existing_profiles):
            if exclude_index is not None and index == exclude_index:
                continue
            existing_symbol = str(existing.get("symbol", "")).upper().strip()
            if existing_symbol == symbol:
                errors.append(f"O símbolo {symbol} já existe em outro perfil.")
                break

    for field, (minimum, maximum) in _INTEGER_PROFILE_FIELDS.items():
        _validate_integer_field(
            errors,
            normalized,
            field,
            minimum=minimum,
            maximum=maximum,
        )

    for field, (dec_minimum, allow_equal) in _DECIMAL_PROFILE_FIELDS.items():
        _validate_decimal_field(
            errors,
            normalized,
            field,
            minimum=dec_minimum,
            allow_equal=allow_equal,
        )

    if normalized["margin_type"] not in {"ISOLATED", "CROSS"}:
        errors.append("margin_type deve ser ISOLATED ou CROSS.")
    if normalized["exit_model"] not in {"FIXED_PCT", "ATR"}:
        errors.append("exit_model deve ser FIXED_PCT ou ATR.")

    refined_path = normalized["ai_refined_model_path"]
    if normalized["ai_use_symbol_refined_model"] and refined_path:
        validator = model_validator or _default_model_validator()
        try:
            metadata = validator(refined_path)
        except Exception as error:
            errors.append(f"Modelo refinado IA inválido: {error}")
        else:
            model_type = str(metadata.get("model_type", ""))
            model_scope = str(metadata.get("scope", "")).upper().strip()
            model_symbol = str(metadata.get("symbol", "")).upper().strip()
            if model_type != "probabilistic_linear_ai_v1":
                errors.append("O modelo refinado precisa usar probabilistic_linear_ai_v1.")
            if model_scope != "SYMBOL_REFINED":
                errors.append("O modelo refinado precisa possuir scope SYMBOL_REFINED.")
            if model_symbol and symbol and model_symbol != symbol:
                errors.append(
                    f"O modelo refinado pertence a {model_symbol}, mas o perfil é {symbol}."
                )

    return tuple(errors)


def normalize_portfolio_risk(values: Mapping[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(DEFAULT_PORTFOLIO_RISK_SETTINGS)
    normalized.update(deepcopy(dict(values)))
    normalized["allow_multiple_same_direction"] = _as_bool(
        normalized.get("allow_multiple_same_direction", True),
        default=True,
    )
    normalized["allow_multiple_positions_same_symbol"] = _as_bool(
        normalized.get("allow_multiple_positions_same_symbol", False),
        default=False,
    )
    return normalized


def validate_portfolio_risk(values: Mapping[str, Any]) -> tuple[str, ...]:
    normalized = normalize_portfolio_risk(values)
    errors: list[str] = []
    for field, (minimum, maximum) in _INTEGER_PORTFOLIO_FIELDS.items():
        _validate_integer_field(
            errors,
            normalized,
            field,
            minimum=minimum,
            maximum=maximum,
        )
    for field in _DECIMAL_PORTFOLIO_FIELDS:
        _validate_decimal_field(
            errors,
            normalized,
            field,
            minimum=Decimal("0"),
            allow_equal=True,
        )
    return tuple(errors)


def resolve_profile_ai_status(
    profile: Mapping[str, Any],
    settings: Mapping[str, Any],
    *,
    model_validator: ModelValidator | None = None,
) -> str:
    normalized = normalize_symbol_profile(profile)
    refined_path = normalized["ai_refined_model_path"]
    use_refined = normalized["ai_use_symbol_refined_model"]
    validator = model_validator or _default_model_validator()

    if use_refined and refined_path:
        try:
            metadata = validator(refined_path)
        except Exception:
            return "Refinado inválido"
        if (
            str(metadata.get("model_type", "")) == "probabilistic_linear_ai_v1"
            and str(metadata.get("scope", "")).upper().strip() == "SYMBOL_REFINED"
            and str(metadata.get("symbol", "")).upper().strip() == normalized["symbol"]
        ):
            return "Refinado"
        return "Refinado incompatível"

    fallback_enabled = _as_bool(
        settings.get("ai_fallback_to_global_model", True),
        default=True,
    )
    global_path = str(settings.get("ai_global_model_path", "")).strip()
    if fallback_enabled and global_path:
        try:
            metadata = validator(global_path)
        except Exception:
            return "Fallback inválido"
        if (
            str(metadata.get("model_type", "")) == "probabilistic_linear_ai_v1"
            and str(metadata.get("scope", "")).upper().strip() == "GLOBAL"
        ):
            return "Fallback global"
        return "Fallback incompatível"

    return "Não configurado"


def _row_tone(*, enabled: bool, status: str, error: str) -> str:
    if error:
        return "danger"
    if not enabled:
        return "muted"
    normalized = status.upper()
    if normalized in {"ERROR", "BLOCKED", "PAUSED"}:
        return "danger" if normalized == "ERROR" else "warning"
    if normalized not in {"", "-", "DISABLED"}:
        return "success"
    return "neutral"


def _validate_integer_field(
    errors: list[str],
    values: Mapping[str, Any],
    field: str,
    *,
    minimum: int | None,
    maximum: int | None,
) -> None:
    raw = values.get(field, "")
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        errors.append(f"{field} precisa ser um número inteiro.")
        return
    if minimum is not None and number < minimum:
        errors.append(f"{field} precisa ser maior ou igual a {minimum}.")
    if maximum is not None and number > maximum:
        errors.append(f"{field} precisa ser menor ou igual a {maximum}.")


def _validate_decimal_field(
    errors: list[str],
    values: Mapping[str, Any],
    field: str,
    *,
    minimum: Decimal,
    allow_equal: bool,
) -> None:
    raw = values.get(field, "")
    try:
        number = Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        errors.append(f"{field} precisa ser um número válido.")
        return
    if not number.is_finite():
        errors.append(f"{field} precisa ser um número finito.")
        return
    invalid = number < minimum if allow_equal else number <= minimum
    if invalid:
        comparator = "maior ou igual a" if allow_equal else "maior que"
        errors.append(f"{field} precisa ser {comparator} {minimum}.")


def _format_timestamp(path: Path) -> str:
    try:
        timestamp = path.stat().st_mtime
    except OSError:
        return "-"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%d/%m/%Y %H:%M:%S")


def _safe_text(sanitizer: Sanitizer, value: Any) -> str:
    try:
        return str(sanitizer(value))
    except Exception:
        return "[SANITIZATION_ERROR]"


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "sim"}:
        return True
    if normalized in {"0", "false", "no", "n", "nao", "não"}:
        return False
    return default


def _default_model_validator() -> ModelValidator:
    from research.ai.registry import validate_ai_model_file

    return validate_ai_model_file


def _default_sanitizer() -> Sanitizer:
    from utils.privacy import sanitize_for_audit

    return sanitize_for_audit
