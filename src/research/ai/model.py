"""Research-facing compatibility imports for the canonical AI model contract."""

from domain.ai.model import (
    AI_FEATURES,
    AI_REQUIRED_HISTORY,
    PROBABILISTIC_EVENT_DEFINITION,
    PROBABILISTIC_MODEL_TYPE,
    PROBABILISTIC_SIGNAL_MODE,
    extract_ai_features,
    load_ai_model,
    save_ai_model,
)

__all__ = [
    "AI_FEATURES",
    "AI_REQUIRED_HISTORY",
    "PROBABILISTIC_EVENT_DEFINITION",
    "PROBABILISTIC_MODEL_TYPE",
    "PROBABILISTIC_SIGNAL_MODE",
    "extract_ai_features",
    "load_ai_model",
    "save_ai_model",
]
