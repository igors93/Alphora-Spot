import json
from pathlib import Path
from typing import Any

from research.ai.model import load_ai_model
from research.ai.paths import AI_REGISTRY_PATH

AI_REGISTRY_VERSION = 1
AI_SCOPE_GLOBAL = "GLOBAL"
AI_SCOPE_SYMBOL_REFINED = "SYMBOL_REFINED"

VALID_AI_SCOPES = {
    AI_SCOPE_GLOBAL,
    AI_SCOPE_SYMBOL_REFINED,
}


def empty_ai_registry() -> dict[str, Any]:
    return {
        "version": AI_REGISTRY_VERSION,
        "models": [],
    }


def load_ai_registry(path: str | Path = AI_REGISTRY_PATH) -> dict[str, Any]:
    registry_path = Path(path)
    if not registry_path.exists():
        return empty_ai_registry()

    with registry_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError("Registry IA precisa ser um objeto JSON")

    models = payload.get("models", [])
    if not isinstance(models, list):
        raise ValueError("Registry IA precisa conter lista 'models'")

    return {
        "version": int(payload.get("version", AI_REGISTRY_VERSION)),
        "models": [model for model in models if isinstance(model, dict)],
    }


def save_ai_registry(
    registry: dict[str, Any],
    path: str | Path = AI_REGISTRY_PATH,
) -> Path:
    registry_path = Path(path)
    prepared = {
        "version": int(registry.get("version", AI_REGISTRY_VERSION)),
        "models": list(registry.get("models", [])),
    }

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with registry_path.open("w", encoding="utf-8") as file:
        json.dump(prepared, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")

    return registry_path


def validate_ai_model_file(path: str | Path) -> dict[str, Any]:
    model_path = Path(path)
    if not model_path.exists():
        raise ValueError(f"Modelo IA nao encontrado: {model_path}")

    model = load_ai_model(model_path)
    metrics = model.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}

    return {
        "path": str(model_path),
        "model_id": str(model.get("model_id", "")),
        "model_family": str(model.get("model_family", "")),
        "model_type": str(model.get("model_type", "")),
        "scope": str(model.get("scope", "")),
        "symbol": str(model.get("symbol", "")),
        "interval": str(model.get("interval", "")),
        "trained_at": str(model.get("trained_at", "")),
        "training_rows": int(model.get("training_rows", 0) or 0),
        "metrics": metrics,
        "trained_symbols": list(model.get("trained_symbols", []))
        if isinstance(model.get("trained_symbols", []), list)
        else [],
        "parent_global_model_id": str(model.get("parent_global_model_id", "")),
        "parent_global_model_path": str(model.get("parent_global_model_path", "")),
        "approved_for": list(model.get("approved_for", []))
        if isinstance(model.get("approved_for", []), list)
        else [],
        "blocked_for": list(model.get("blocked_for", []))
        if isinstance(model.get("blocked_for", []), list)
        else [],
        "readiness_status": str(model.get("readiness_status", "")),
        "created_by": str(model.get("created_by", "")),
        "event_definition": str(model.get("event_definition", "")),
        "dataset_hash": str(model.get("dataset_hash", "")),
        "dataset_hash_by_symbol": dict(model.get("dataset_hash_by_symbol", {}))
        if isinstance(model.get("dataset_hash_by_symbol", {}), dict)
        else {},
        "training_type": str(model.get("training_type", "")),
        "algorithm_version": str(model.get("algorithm_version", "")),
        "training_window": dict(model.get("training_window", {}))
        if isinstance(model.get("training_window", {}), dict)
        else {},
        "validation_window": dict(model.get("validation_window", {}))
        if isinstance(model.get("validation_window", {}), dict)
        else {},
        "test_window": dict(model.get("test_window", {}))
        if isinstance(model.get("test_window", {}), dict)
        else {},
        "dataset_overlap_with_parent": model.get("dataset_overlap_with_parent"),
        "overfit_risk_flags": list(model.get("overfit_risk_flags", []))
        if isinstance(model.get("overfit_risk_flags", []), list)
        else [],
    }


def register_ai_model(
    model_path: str | Path,
    *,
    scope: str,
    model_id: str | None = None,
    symbol: str = "",
    interval: str = "",
    trained_symbols: list[str] | None = None,
    parent_model_id: str = "",
    parent_model_path: str = "",
    parent_global_model_id: str = "",
    parent_global_model_path: str = "",
    approved_for: list[str] | None = None,
    blocked_for: list[str] | None = None,
    readiness_status: str = "",
    created_by: str = "",
    event_definition: str = "",
    registry_path: str | Path = AI_REGISTRY_PATH,
) -> dict[str, Any]:
    normalized_scope = str(scope or "").upper().strip()
    if normalized_scope not in VALID_AI_SCOPES:
        raise ValueError(f"Escopo IA invalido: {scope}")

    metadata = validate_ai_model_file(model_path)
    resolved_symbol = str(symbol or metadata.get("symbol", "")).upper().strip()
    resolved_interval = str(interval or metadata.get("interval", "")).strip()
    resolved_model_id = (
        str(model_id).strip()
        if model_id
        else _default_model_id(
            scope=normalized_scope,
            symbol=resolved_symbol,
            interval=resolved_interval,
            path=Path(model_path),
        )
    )

    entry = {
        "model_id": resolved_model_id,
        "path": str(Path(model_path)),
        "scope": normalized_scope,
        "symbol": resolved_symbol,
        "interval": resolved_interval,
        "model_family": (str(metadata.get("model_family", "") or metadata["model_type"])),
        "model_type": metadata["model_type"],
        "trained_at": metadata["trained_at"],
        "training_rows": metadata["training_rows"],
        "metrics": metadata["metrics"],
        "readiness_status": str(readiness_status or metadata.get("readiness_status") or "TRAINED"),
    }
    cleaned_trained_symbols = [
        str(item).upper().strip()
        for item in list(
            trained_symbols
            or metadata.get("trained_symbols", [])
            or metadata.get("metrics", {}).get("trained_symbols", [])
        )
        if str(item).strip()
    ]
    if cleaned_trained_symbols:
        entry["trained_symbols"] = cleaned_trained_symbols

    resolved_parent_global_model_id = str(
        parent_global_model_id or metadata.get("parent_global_model_id", "") or parent_model_id
    ).strip()
    resolved_parent_global_model_path = str(
        parent_global_model_path
        or metadata.get("parent_global_model_path", "")
        or parent_model_path
    ).strip()

    if resolved_parent_global_model_id:
        entry["parent_global_model_id"] = resolved_parent_global_model_id
        entry["parent_model_id"] = resolved_parent_global_model_id

    if resolved_parent_global_model_path:
        entry["parent_global_model_path"] = resolved_parent_global_model_path
        entry["parent_model_path"] = resolved_parent_global_model_path

    resolved_approved_for = [
        str(item).upper().strip()
        for item in list(approved_for or metadata.get("approved_for", []))
        if str(item).strip()
    ]
    if resolved_approved_for:
        entry["approved_for"] = resolved_approved_for

    resolved_blocked_for = [
        str(item).upper().strip()
        for item in list(blocked_for or metadata.get("blocked_for", []))
        if str(item).strip()
    ]
    if resolved_blocked_for:
        entry["blocked_for"] = resolved_blocked_for

    resolved_created_by = str(created_by or metadata.get("created_by", "")).strip()
    if resolved_created_by:
        entry["created_by"] = resolved_created_by

    resolved_event_definition = str(
        event_definition or metadata.get("event_definition", "")
    ).strip()
    if resolved_event_definition:
        entry["event_definition"] = resolved_event_definition

    for optional_key in (
        "dataset_hash",
        "dataset_hash_by_symbol",
        "training_type",
        "algorithm_version",
        "training_window",
        "validation_window",
        "test_window",
        "dataset_overlap_with_parent",
        "overfit_risk_flags",
    ):
        value = metadata.get(optional_key)
        if value not in (None, "", [], {}):
            entry[optional_key] = value

    registry = load_ai_registry(registry_path)
    models = [
        model for model in registry.get("models", []) if model.get("model_id") != resolved_model_id
    ]
    models.append(entry)
    registry["models"] = sorted(
        models,
        key=lambda item: (
            str(item.get("scope", "")),
            str(item.get("symbol", "")),
            str(item.get("model_id", "")),
        ),
    )
    save_ai_registry(registry, registry_path)
    return entry


def list_ai_models(
    *,
    scope: str | None = None,
    symbol: str | None = None,
    interval: str | None = None,
    registry_path: str | Path = AI_REGISTRY_PATH,
) -> list[dict[str, Any]]:
    registry = load_ai_registry(registry_path)
    models = list(registry.get("models", []))

    if scope is not None:
        normalized_scope = str(scope).upper().strip()
        models = [
            model
            for model in models
            if str(model.get("scope", "")).upper().strip() == normalized_scope
        ]

    if symbol is not None:
        normalized_symbol = str(symbol).upper().strip()
        models = [
            model
            for model in models
            if str(model.get("symbol", "")).upper().strip() == normalized_symbol
        ]

    if interval is not None:
        normalized_interval = str(interval).strip().lower()
        models = [
            model
            for model in models
            if str(model.get("interval", "")).strip().lower() == normalized_interval
        ]

    return models


def list_compatible_ai_models(
    *,
    interval: str,
    execution_mode: str,
    scope: str | None = None,
    symbol: str | None = None,
    registry_path: str | Path = AI_REGISTRY_PATH,
) -> list[dict[str, Any]]:
    """Return models compatible with the given interval and execution_mode.

    A model is compatible when its interval matches and the execution_mode is
    either not in its ``blocked_for`` list or is present in ``approved_for``.
    Models that have no ``approved_for`` or ``blocked_for`` fields pass through.
    """
    normalized_mode = str(execution_mode).strip().upper()
    models = list_ai_models(
        scope=scope,
        symbol=symbol,
        interval=interval,
        registry_path=registry_path,
    )
    compatible = []
    for model in models:
        blocked = [str(m).strip().upper() for m in model.get("blocked_for", [])]
        approved = [str(m).strip().upper() for m in model.get("approved_for", [])]
        if normalized_mode in blocked:
            continue
        if approved and normalized_mode not in approved:
            continue
        compatible.append(model)
    return compatible


def find_ai_model(
    model_id: str,
    *,
    registry_path: str | Path = AI_REGISTRY_PATH,
) -> dict[str, Any] | None:
    wanted = str(model_id).strip()
    for model in list_ai_models(registry_path=registry_path):
        if str(model.get("model_id", "")) == wanted:
            return model
    return None


def _default_model_id(*, scope: str, symbol: str, interval: str, path: Path) -> str:
    if scope == AI_SCOPE_GLOBAL:
        return f"GLOBAL_{interval}_prob_ai"
    if scope == AI_SCOPE_SYMBOL_REFINED and symbol and interval:
        return f"{symbol}_{interval}_prob_refined_ai"
    return path.stem
