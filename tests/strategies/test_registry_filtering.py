"""Tests for list_ai_models interval filter and list_compatible_ai_models."""

import json
from pathlib import Path

from research.ai.registry import list_ai_models, list_compatible_ai_models


def _write_registry(path: Path, models: list[dict]) -> None:
    path.write_text(
        json.dumps({"version": 1, "models": models}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _model(**overrides) -> dict:
    base = {
        "model_id": "GLOBAL_1m_prob_ai",
        "path": "/tmp/model.json",
        "scope": "GLOBAL",
        "symbol": "",
        "interval": "1m",
        "model_family": "prob_ai",
        "model_type": "PROBABILISTIC_LINEAR",
        "trained_at": "2024-01-01T00:00:00Z",
        "training_rows": 1000,
        "metrics": {},
        "readiness_status": "TRAINED",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# list_ai_models interval filter
# ---------------------------------------------------------------------------


def test_list_ai_models_interval_filter_returns_only_matching(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        [
            _model(model_id="m1", interval="1m"),
            _model(model_id="m2", interval="5m"),
            _model(model_id="m3", interval="15m"),
        ],
    )

    result = list_ai_models(interval="5m", registry_path=registry_path)

    assert len(result) == 1
    assert result[0]["model_id"] == "m2"


def test_list_ai_models_interval_filter_empty_when_no_match(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [_model(model_id="m1", interval="1m")])

    result = list_ai_models(interval="4h", registry_path=registry_path)

    assert result == []


def test_list_ai_models_interval_filter_none_returns_all(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        [_model(model_id="m1", interval="1m"), _model(model_id="m2", interval="5m")],
    )

    result = list_ai_models(interval=None, registry_path=registry_path)

    assert {m["model_id"] for m in result} == {"m1", "m2"}


def test_list_ai_models_interval_combined_with_scope(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        [
            _model(model_id="g1", interval="1m", scope="GLOBAL"),
            _model(model_id="r1", interval="1m", scope="SYMBOL_REFINED", symbol="BTCUSDT"),
            _model(model_id="g2", interval="5m", scope="GLOBAL"),
        ],
    )

    result = list_ai_models(scope="GLOBAL", interval="1m", registry_path=registry_path)

    assert len(result) == 1
    assert result[0]["model_id"] == "g1"


# ---------------------------------------------------------------------------
# list_compatible_ai_models
# ---------------------------------------------------------------------------


def test_compatible_no_restrictions_passes_all(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [_model(model_id="m1", interval="1m")])

    result = list_compatible_ai_models(
        interval="1m", execution_mode="REAL_LIVE", registry_path=registry_path
    )

    assert len(result) == 1
    assert result[0]["model_id"] == "m1"


def test_compatible_blocked_mode_is_excluded(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        [_model(model_id="m1", interval="1m", blocked_for=["REAL_LIVE"])],
    )

    result = list_compatible_ai_models(
        interval="1m", execution_mode="REAL_LIVE", registry_path=registry_path
    )

    assert result == []


def test_compatible_blocked_mode_does_not_exclude_others(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        [_model(model_id="m1", interval="1m", blocked_for=["REAL_LIVE"])],
    )

    result = list_compatible_ai_models(
        interval="1m", execution_mode="PAPER_LIVE", registry_path=registry_path
    )

    assert len(result) == 1


def test_compatible_approved_for_excludes_unlisted_mode(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        [_model(model_id="m1", interval="1m", approved_for=["PAPER_LIVE", "TESTNET_LIVE"])],
    )

    result = list_compatible_ai_models(
        interval="1m", execution_mode="REAL_LIVE", registry_path=registry_path
    )

    assert result == []


def test_compatible_approved_for_includes_listed_mode(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        [_model(model_id="m1", interval="1m", approved_for=["PAPER_LIVE", "TESTNET_LIVE"])],
    )

    result = list_compatible_ai_models(
        interval="1m", execution_mode="PAPER_LIVE", registry_path=registry_path
    )

    assert len(result) == 1


def test_compatible_wrong_interval_excluded(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [_model(model_id="m1", interval="5m")])

    result = list_compatible_ai_models(
        interval="1m", execution_mode="PAPER_LIVE", registry_path=registry_path
    )

    assert result == []


def test_compatible_mode_comparison_is_case_insensitive(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        [_model(model_id="m1", interval="1m", blocked_for=["real_live"])],
    )

    result = list_compatible_ai_models(
        interval="1m", execution_mode="REAL_LIVE", registry_path=registry_path
    )

    assert result == []


def test_compatible_scope_and_symbol_filters_forwarded(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(
        registry_path,
        [
            _model(model_id="g1", interval="1m", scope="GLOBAL", symbol=""),
            _model(
                model_id="r1",
                interval="1m",
                scope="SYMBOL_REFINED",
                symbol="BTCUSDT",
            ),
        ],
    )

    result = list_compatible_ai_models(
        interval="1m",
        execution_mode="PAPER_LIVE",
        scope="GLOBAL",
        registry_path=registry_path,
    )

    assert len(result) == 1
    assert result[0]["model_id"] == "g1"


def test_compatible_empty_registry_returns_empty(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [])

    result = list_compatible_ai_models(
        interval="1m", execution_mode="REAL_LIVE", registry_path=registry_path
    )

    assert result == []


def test_compatible_missing_registry_returns_empty(tmp_path):
    result = list_compatible_ai_models(
        interval="1m",
        execution_mode="REAL_LIVE",
        registry_path=tmp_path / "nonexistent.json",
    )

    assert result == []
