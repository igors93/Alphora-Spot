"""Canonical probabilistic AI model inference used by live and backtest."""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from domain.ai.model import (
    AI_REQUIRED_HISTORY,
    PROBABILISTIC_EVENT_DEFINITION,
    PROBABILISTIC_MODEL_TYPE,
    PROBABILISTIC_SIGNAL_MODE,
    _score_row,
    _standardize_features,
    extract_ai_features,
    has_ohlcv,
    load_ai_model,
)

PROBABILISTIC_SCOPE_GLOBAL = "GLOBAL"
PROBABILISTIC_SCOPE_SYMBOL_REFINED = "SYMBOL_REFINED"
PROBABILITY_EPSILON = 1e-6


def load_probabilistic_ai_model(path: str | Path) -> dict[str, Any]:
    model = load_ai_model(path)
    if model.get("model_type") != PROBABILISTIC_MODEL_TYPE:
        raise ValueError(f"Modelo probabilistico incompativel: {model.get('model_type')}")
    return model


def predict_probabilistic_event(
    model: dict[str, Any],
    prices: Sequence[Any],
    *,
    min_long_probability: float | None = None,
    min_short_probability: float | None = None,
    min_direction_gap: float = 0.0,
    min_expected_value_bps: float = 0.0,
    estimated_round_trip_cost_bps: float | None = None,
) -> dict[str, Any]:
    _validate_probabilistic_payload(model)
    values = list(prices)
    required_history = int(model.get("required_history", AI_REQUIRED_HISTORY))
    if len(values) < required_history:
        return no_action_prediction(
            "AI_INSUFFICIENT_HISTORY",
            required_history=required_history,
            available_history=len(values),
        )
    window = values[-required_history:]
    if not all(has_ohlcv(item) for item in window):
        return no_action_prediction(
            "AI_OHLCV_REQUIRED",
            required_history=required_history,
            available_history=len(values),
        )

    feature_result = extract_ai_features(values)
    feature_values = dict(feature_result["features"])
    standardized = _standardize_features(feature_values, model.get("feature_stats", {}))
    long_score = _score_row(
        standardized,
        {key: float(value) for key, value in model.get("weights_long", {}).items()},
        float(model.get("bias_long", 0.0)),
    )
    short_score = _score_row(
        standardized,
        {key: float(value) for key, value in model.get("weights_short", {}).items()},
        float(model.get("bias_short", 0.0)),
    )
    probabilities = _event_probabilities_from_scores(
        long_score,
        short_score,
        calibration=model.get("calibration", {}),
    )
    threshold_bps = float(model.get("threshold_bps", 0.0))
    p_long = float(probabilities["p_long_event"])
    p_short = float(probabilities["p_short_event"])
    p_neutral = float(probabilities["p_neutral_event"])
    cost_bps = (
        float(estimated_round_trip_cost_bps)
        if estimated_round_trip_cost_bps is not None
        else float(model.get("estimated_round_trip_cost_bps", 0.0) or 0.0)
    )
    payoff_profile = model.get("payoff_profile", {})
    if isinstance(payoff_profile, dict) and payoff_profile:
        long_event_return = float(payoff_profile.get("long_event_mean_bps", threshold_bps))
        short_event_return = float(payoff_profile.get("short_event_mean_bps", -threshold_bps))
        neutral_event_return = float(payoff_profile.get("neutral_event_mean_bps", 0.0))
        expected_long_gross_bps = (
            p_long * long_event_return
            + p_short * short_event_return
            + p_neutral * neutral_event_return
        )
    else:
        # Compatibility for legacy model artifacts that predate payoff profiles.
        expected_long_gross_bps = (p_long - p_short) * threshold_bps
    expected_short_gross_bps = -expected_long_gross_bps
    expected_move_bps = expected_long_gross_bps
    long_ev = expected_long_gross_bps - cost_bps
    short_ev = expected_short_gross_bps - cost_bps
    expected_value_bps = max(long_ev, short_ev, 0.0)
    direction_gap = abs(p_long - p_short)
    confidence = max(p_long, p_short)
    action_candidate = "NO_ACTION"
    direction_candidate = "NONE"

    long_threshold = (
        float(min_long_probability)
        if min_long_probability is not None
        else float(model.get("min_long_probability", 0.5))
    )
    short_threshold = (
        float(min_short_probability)
        if min_short_probability is not None
        else float(model.get("min_short_probability", long_threshold))
    )

    if (
        p_long >= long_threshold
        and p_long >= p_short
        and direction_gap >= float(min_direction_gap)
        and long_ev >= float(min_expected_value_bps)
    ):
        action_candidate = "OPEN_LONG"
        direction_candidate = "LONG"
    elif (
        p_short >= short_threshold
        and p_short > p_long
        and direction_gap >= float(min_direction_gap)
        and short_ev >= float(min_expected_value_bps)
    ):
        action_candidate = "OPEN_SHORT"
        direction_candidate = "SHORT"

    reason = {
        "OPEN_LONG": "AI_PROBABILITY_OPEN_LONG",
        "OPEN_SHORT": "AI_PROBABILITY_OPEN_SHORT",
        "NO_ACTION": "AI_PROBABILITY_NO_ACTION",
    }[action_candidate]

    return {
        "reason": reason,
        "action_candidate": action_candidate,
        "direction_candidate": direction_candidate,
        "p_long_event": p_long,
        "p_short_event": p_short,
        "p_neutral_event": p_neutral,
        "expected_move_bps": expected_move_bps,
        "expected_value_bps": expected_value_bps,
        "expected_long_gross_bps": expected_long_gross_bps,
        "expected_short_gross_bps": expected_short_gross_bps,
        "long_expected_value_bps": long_ev,
        "short_expected_value_bps": short_ev,
        "estimated_round_trip_cost_bps": cost_bps,
        "confidence": confidence,
        "direction_gap": direction_gap,
        "long_score": long_score,
        "short_score": short_score,
        "features": feature_values,
        "standardized_features": standardized,
        "required_history": required_history,
        "available_history": len(values),
    }


def no_action_prediction(
    reason: str,
    *,
    required_history: int,
    available_history: int,
) -> dict[str, Any]:
    """A prediction that cannot be acted on: neutral probabilities and the reason."""

    return {
        "reason": reason,
        "action_candidate": "NO_ACTION",
        "direction_candidate": "NONE",
        "p_long_event": 0.0,
        "p_short_event": 0.0,
        "p_neutral_event": 1.0,
        "expected_move_bps": 0.0,
        "expected_value_bps": 0.0,
        "confidence": 0.0,
        "features": {},
        "standardized_features": {},
        "required_history": required_history,
        "available_history": available_history,
    }


def _event_probabilities_from_scores(
    long_score: float,
    short_score: float,
    *,
    calibration: dict[str, Any],
) -> dict[str, float]:
    if str(calibration.get("method", "")) == "platt_scaling_v2":
        raw_long = _sigmoid(
            float(calibration.get("long_scale", 1.0)) * float(long_score)
            + float(calibration.get("long_intercept", 0.0))
        )
        raw_short = _sigmoid(
            float(calibration.get("short_scale", 1.0)) * float(short_score)
            + float(calibration.get("short_intercept", 0.0))
        )
    else:
        long_bias = float(calibration.get("long_logit_bias", 0.0) or 0.0)
        short_bias = float(calibration.get("short_logit_bias", 0.0) or 0.0)
        raw_long = _sigmoid(float(long_score) + long_bias)
        raw_short = _sigmoid(float(short_score) + short_bias)
    total_events = raw_long + raw_short

    if total_events >= 0.99:
        scale = 0.99 / total_events
        raw_long *= scale
        raw_short *= scale

    p_long = _clamp_probability(raw_long)
    p_short = _clamp_probability(raw_short)
    p_neutral = _clamp_probability(1.0 - p_long - p_short)
    total = p_long + p_short + p_neutral
    if total <= 0:
        return {
            "p_long_event": 0.0,
            "p_short_event": 0.0,
            "p_neutral_event": 1.0,
        }

    return {
        "p_long_event": p_long / total,
        "p_short_event": p_short / total,
        "p_neutral_event": p_neutral / total,
    }


def _validate_probabilistic_payload(model: dict[str, Any]) -> None:
    if model.get("model_type") != PROBABILISTIC_MODEL_TYPE:
        raise ValueError(f"Modelo probabilistico incompativel: {model.get('model_type')}")
    features = model.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("Modelo probabilistico sem lista de features")
    for key in ("weights_long", "weights_short"):
        weights = model.get(key)
        if not isinstance(weights, dict):
            raise ValueError(f"Modelo probabilistico sem {key}")
        missing = [feature for feature in features if feature not in weights]
        if missing:
            raise ValueError(f"Modelo probabilistico sem {key} para: {missing}")
    calibration = model.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError("Modelo probabilistico sem calibration")
    if model.get("event_definition") != PROBABILISTIC_EVENT_DEFINITION:
        raise ValueError("Modelo probabilistico com event_definition invalido")


def _clamp_probability(value: float) -> float:
    if not math.isfinite(float(value)):
        return 0.0
    return max(PROBABILITY_EPSILON, min(1.0 - PROBABILITY_EPSILON, float(value)))


def _sigmoid(value: float) -> float:
    if value >= 50:
        return 1.0
    if value <= -50:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


__all__ = [
    "PROBABILISTIC_EVENT_DEFINITION",
    "PROBABILISTIC_MODEL_TYPE",
    "PROBABILISTIC_SCOPE_GLOBAL",
    "PROBABILISTIC_SCOPE_SYMBOL_REFINED",
    "PROBABILISTIC_SIGNAL_MODE",
    "load_probabilistic_ai_model",
    "no_action_prediction",
    "predict_probabilistic_event",
]
