from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backtesting.domain.config import (
    STRATEGY_MODE_PROBABILISTIC_AI,
    BacktestConfig,
)


def build_model_data_validation(
    config: BacktestConfig,
    *,
    data_diagnostics: dict[str, Any],
) -> dict[str, Any]:
    mode = config.validation_settings.evaluation_mode
    evaluation_window_verified = _evaluation_window_isolated(
        config,
        data_diagnostics=data_diagnostics,
    )
    if config.strategy.normalized_mode != STRATEGY_MODE_PROBABILISTIC_AI:
        warnings = (
            []
            if evaluation_window_verified or mode not in {"OUT_OF_SAMPLE", "WALK_FORWARD"}
            else ["EVALUATION_WINDOW_NOT_ISOLATED"]
        )
        return {
            "applicable": False,
            "evaluation_mode": mode,
            "evaluation_window_verified": evaluation_window_verified,
            "out_of_sample_verified": (
                mode in {"OUT_OF_SAMPLE", "WALK_FORWARD"} and evaluation_window_verified
            ),
            "overlap_detected": False,
            "models": [],
            "warnings": warnings,
        }

    model_paths: list[tuple[str, str | None, str]] = []
    if config.strategy.global_model_path:
        model_paths.append(("GLOBAL", None, config.strategy.global_model_path))
    if config.strategy.use_symbol_refined_models:
        model_paths.extend(
            ("SYMBOL_REFINED", symbol, path)
            for symbol, path in config.strategy.symbol_model_paths.items()
            if symbol in config.symbol_names
        )
    results = []
    warnings = []
    if mode in {"OUT_OF_SAMPLE", "WALK_FORWARD"} and not evaluation_window_verified:
        warnings.append("EVALUATION_WINDOW_NOT_ISOLATED")
    overlap_detected = False
    all_verifiable = bool(model_paths)
    first_by_symbol = data_diagnostics.get("first_timestamp_by_symbol", {}) or {}
    union_start = data_diagnostics.get("union_start_timestamp")
    for source, symbol, raw_path in model_paths:
        path = Path(raw_path)
        if not path.exists():
            all_verifiable = False
            warnings.append(f"MODEL_FILE_MISSING:{path}")
            continue
        model = _load_json(path)
        training_window = model.get("training_window")
        training_end = ""
        if isinstance(training_window, dict):
            training_end = str(training_window.get("end", ""))
        training_end = training_end or str(model.get("data_end", ""))
        validation_window = model.get("validation_window")
        validation_end = (
            str(validation_window.get("end", "")) if isinstance(validation_window, dict) else ""
        )
        test_window = model.get("test_window")
        test_end = str(test_window.get("end", "")) if isinstance(test_window, dict) else ""
        isolation_end = test_end or str(model.get("data_end", "")) or validation_end or training_end
        backtest_start = str(
            config.evaluation_start_timestamp
            or (first_by_symbol.get(symbol, "") if symbol else union_start or "")
        )
        overlap = _overlaps_training(backtest_start, isolation_end)
        verifiable = bool(isolation_end and backtest_start)
        purged_training = (
            str(model.get("metrics", {}).get("metrics_source", "")) == "PURGED_OUT_OF_SAMPLE_TEST"
        )
        if not verifiable:
            warnings.append(f"MODEL_TRAINING_WINDOW_UNVERIFIABLE:{path}")
            all_verifiable = False
        if not purged_training:
            warnings.append(f"MODEL_NOT_TRAINED_WITH_PURGED_OOS_PIPELINE:{path}")
            all_verifiable = False
        if overlap:
            warnings.append(f"MODEL_BACKTEST_TRAINING_OVERLAP:{path}")
            overlap_detected = True
        results.append(
            {
                "source": source,
                "symbol": symbol,
                "path": str(path),
                "sha256": _sha256(path),
                "model_id": str(model.get("model_id", "")),
                "algorithm_version": str(model.get("algorithm_version", "")),
                "training_end": training_end,
                "validation_end": validation_end,
                "test_end": test_end,
                "isolation_cutoff": isolation_end,
                "backtest_start": backtest_start,
                "training_window_verifiable": verifiable,
                "purged_oos_training": purged_training,
                "overlap_detected": overlap,
            }
        )
    out_of_sample_verified = (
        mode in {"OUT_OF_SAMPLE", "WALK_FORWARD"}
        and evaluation_window_verified
        and all_verifiable
        and not overlap_detected
    )
    return {
        "applicable": True,
        "evaluation_mode": mode,
        "evaluation_window_verified": evaluation_window_verified,
        "out_of_sample_verified": out_of_sample_verified,
        "overlap_detected": overlap_detected,
        "models": results,
        "warnings": sorted(set(warnings)),
    }


def enforce_model_data_isolation(
    config: BacktestConfig,
    diagnostics: dict[str, Any],
) -> None:
    if config.validation_settings.fail_on_model_training_overlap and diagnostics.get(
        "overlap_detected"
    ):
        raise ValueError("Backtest bloqueado: dados sobrepoem janela de treinamento do modelo IA")


def _overlaps_training(backtest_start: str, training_end: str) -> bool:
    start = _parse_timestamp(backtest_start)
    end = _parse_timestamp(training_end)
    return bool(start is not None and end is not None and start <= end)


def _evaluation_window_isolated(
    config: BacktestConfig,
    *,
    data_diagnostics: dict[str, Any],
) -> bool:
    if config.validation_settings.evaluation_mode not in {
        "OUT_OF_SAMPLE",
        "WALK_FORWARD",
    }:
        return False
    evaluation_start = _parse_timestamp(str(config.evaluation_start_timestamp or ""))
    data_start = _parse_timestamp(
        str(config.data_start_timestamp or data_diagnostics.get("union_start_timestamp") or "")
    )
    evaluation_end = _parse_timestamp(str(config.evaluation_end_timestamp or ""))
    data_end = _parse_timestamp(
        str(config.data_end_timestamp or data_diagnostics.get("union_end_timestamp") or "")
    )
    if evaluation_start is None or data_start is None or evaluation_start <= data_start:
        return False
    if evaluation_end is not None and data_end is not None and evaluation_end > data_end:
        return False
    return True


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Modelo IA invalido: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
