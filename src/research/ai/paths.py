from pathlib import Path

AI_MODELS_DIR = Path("models") / "ai"
AI_GLOBAL_MODELS_DIR = AI_MODELS_DIR / "global"
AI_REFINED_MODELS_DIR = AI_MODELS_DIR / "refined"
AI_REGISTRY_PATH = AI_MODELS_DIR / "registry.json"


def safe_ai_name(value: str) -> str:
    cleaned = "".join(
        char for char in str(value).upper().strip() if char.isalnum() or char in {"_", "-"}
    )
    return cleaned or "UNKNOWN"


def symbol_refined_dir(symbol: str) -> Path:
    return AI_REFINED_MODELS_DIR / safe_ai_name(symbol)


def default_probabilistic_global_model_path(
    *,
    interval: str,
    version: str | None = None,
) -> Path:
    safe_interval = safe_ai_name(interval)
    suffix = f"_{safe_ai_name(version)}" if version else ""
    return AI_GLOBAL_MODELS_DIR / f"GLOBAL_{safe_interval}_probabilistic_ai{suffix}.json"


def default_probabilistic_refined_model_path(
    *,
    symbol: str,
    interval: str,
    version: str | None = None,
) -> Path:
    safe_symbol = safe_ai_name(symbol)
    safe_interval = safe_ai_name(interval)
    suffix = f"_{safe_ai_name(version)}" if version else ""
    return (
        symbol_refined_dir(safe_symbol)
        / f"{safe_symbol}_{safe_interval}_probabilistic_refined_ai{suffix}.json"
    )
