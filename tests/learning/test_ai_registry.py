import json

import pytest

from research.ai.model import AI_FEATURES, PROBABILISTIC_MODEL_TYPE, save_ai_model
from research.ai.probabilistic import (
    PROBABILISTIC_EVENT_DEFINITION,
    PROBABILISTIC_SCOPE_GLOBAL,
    PROBABILISTIC_SIGNAL_MODE,
)
from research.ai.registry import (
    AI_SCOPE_GLOBAL,
    load_ai_registry,
    register_ai_model,
    save_ai_registry,
    validate_ai_model_file,
)


def _model():
    return {
        "model_id": "GLOBAL_1M_prob_ai",
        "model_type": PROBABILISTIC_MODEL_TYPE,
        "model_family": "PROBABILISTIC_LINEAR_AI",
        "signal_mode": PROBABILISTIC_SIGNAL_MODE,
        "scope": PROBABILISTIC_SCOPE_GLOBAL,
        "symbol": "GLOBAL",
        "interval": "1m",
        "features": list(AI_FEATURES),
        "weights_long": {feature: 0.0 for feature in AI_FEATURES},
        "weights_short": {feature: 0.0 for feature in AI_FEATURES},
        "bias_long": 0.0,
        "bias_short": 0.0,
        "horizon_candles": 5,
        "threshold_bps": 10,
        "required_history": 20,
        "feature_stats": {feature: {"mean": 0.0, "stddev": 1.0} for feature in AI_FEATURES},
        "calibration": {"method": "test", "long_logit_bias": 0.0, "short_logit_bias": 0.0},
        "trained_at": "2026-05-22T00:00:00Z",
        "training_rows": 100,
        "trained_symbols": ["BTCUSDT"],
        "metrics": {"accuracy": 0.5},
        "event_definition": PROBABILISTIC_EVENT_DEFINITION,
    }


def test_load_ai_registry_missing_file_returns_empty_registry(tmp_path):
    registry = load_ai_registry(tmp_path / "missing.json")

    assert registry == {"version": 1, "models": []}


def test_save_and_load_ai_registry_preserves_models(tmp_path):
    path = tmp_path / "registry.json"
    save_ai_registry(
        {
            "version": 1,
            "models": [
                {
                    "model_id": "GLOBAL_1m_prob_ai",
                    "path": "model.json",
                    "scope": "GLOBAL",
                }
            ],
        },
        path,
    )

    loaded = load_ai_registry(path)

    assert loaded["version"] == 1
    assert loaded["models"][0]["model_id"] == "GLOBAL_1m_prob_ai"


def test_validate_ai_model_file_accepts_valid_probabilistic_model(tmp_path):
    model_path = save_ai_model(_model(), tmp_path / "model.json")

    metadata = validate_ai_model_file(model_path)

    assert metadata["model_type"] == PROBABILISTIC_MODEL_TYPE
    assert metadata["symbol"] == "GLOBAL"
    assert metadata["interval"] == "1m"
    assert metadata["training_rows"] == 100


def test_validate_ai_model_file_rejects_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    with pytest.raises(ValueError):
        validate_ai_model_file(path)


def test_register_ai_model_adds_and_updates_entry(tmp_path):
    model_path = save_ai_model(_model(), tmp_path / "model.json")
    registry_path = tmp_path / "registry.json"

    first = register_ai_model(
        model_path,
        scope=AI_SCOPE_GLOBAL,
        registry_path=registry_path,
    )
    second = register_ai_model(
        model_path,
        scope=AI_SCOPE_GLOBAL,
        registry_path=registry_path,
    )

    registry = load_ai_registry(registry_path)

    assert first["model_id"] == second["model_id"]
    assert len(registry["models"]) == 1
    assert registry["models"][0]["symbol"] == "GLOBAL"
