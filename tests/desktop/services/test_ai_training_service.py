import json
from pathlib import Path

import pytest

from desktop.services import ai_training_service as service


def _settings(csv_path: Path) -> dict:
    return {
        "symbol": "BTCUSDT",
        "ai_training_global_symbols": "BTCUSDT,ETHUSDT",
        "ai_training_refined_symbol": "BTCUSDT",
        "ai_training_interval": "1m",
        "ai_training_start": "2024-01-01",
        "ai_training_end": "2024-02-01",
        "ai_training_horizon_candles": "5",
        "ai_training_threshold_bps": "20",
        "ai_training_min_training_rows": "200",
        "ai_training_csv_path": "",
        "ai_training_csv_map": f"BTCUSDT={csv_path},ETHUSDT={csv_path}",
        "ai_training_global_output_dir": "models/ai/global",
        "ai_training_refined_output_dir": "models/ai/refined",
        "ai_training_download_if_missing": False,
        "ai_global_model_path": "models/ai/global/model.json",
    }


def test_global_training_plan_uses_configured_symbols(tmp_path: Path) -> None:
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text("open_time,open,high,low,close,volume\n", encoding="utf-8")

    plan = service.prepare_ai_training_plan(
        _settings(csv_path),
        [],
        kind=service.TRAINING_GLOBAL,
        project_root=tmp_path,
        python_executable="python",
    )

    assert plan.kind == service.TRAINING_GLOBAL
    assert plan.symbols == ("BTCUSDT", "ETHUSDT")
    assert "train-ai-probabilistic-global" in plan.command
    assert "--csv-map" in plan.command


def test_refined_training_requires_global_model(tmp_path: Path) -> None:
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text("data\n", encoding="utf-8")
    settings = _settings(csv_path)
    settings["ai_training_csv_map"] = ""
    settings["ai_training_csv_path"] = str(csv_path)
    settings["ai_global_model_path"] = ""

    errors = service.validate_ai_training_settings(
        settings,
        [],
        kind=service.TRAINING_REFINED,
        project_root=tmp_path,
    )

    assert any("modelo global" in error.lower() for error in errors)


def test_parse_and_apply_training_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_model = tmp_path / "models" / "global.json"
    refined_model = tmp_path / "models" / "btc.json"
    global_model.parent.mkdir(parents=True)
    global_model.write_text("{}", encoding="utf-8")
    refined_model.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(service, "validate_ai_model_file", lambda path: {"scope": "GLOBAL"})

    result = service.parse_ai_training_result(
        [
            json.dumps({"type": "ai_model", "version": 1, "path": str(global_model)}),
            json.dumps(
                {
                    "type": "ai_symbol_model",
                    "version": 1,
                    "symbol": "BTCUSDT",
                    "status": "OK",
                    "path": str(refined_model),
                }
            ),
        ],
        project_root=tmp_path,
    )

    settings, profiles = service.apply_ai_training_result(
        {"strategy_selection_mode": "FIXED"},
        [{"symbol": "BTCUSDT", "enabled": True}],
        result,
    )

    assert settings["strategy_selection_mode"] == "PROBABILISTIC_AI"
    assert settings["ai_probabilistic_enabled"] is True
    assert profiles[0]["ai_refined_model_path"].endswith("models/btc.json")


def test_threshold_must_exceed_the_estimated_round_trip_cost(tmp_path: Path) -> None:
    csv_path = tmp_path / "candles.csv"
    csv_path.write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")
    settings = {**_settings(csv_path), "ai_training_threshold_bps": "15"}

    errors = service.validate_ai_training_settings(
        settings,
        [],
        kind=service.TRAINING_GLOBAL,
        project_root=tmp_path,
    )

    assert any("custo estimado de ida e volta" in error for error in errors)
