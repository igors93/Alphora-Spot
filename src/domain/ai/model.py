import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROBABILISTIC_MODEL_TYPE = "probabilistic_linear_ai_v1"
PROBABILISTIC_EVENT_DEFINITION = "future_return_threshold"
PROBABILISTIC_SIGNAL_MODE = "PROBABILISTIC_EVENT_V1"
# Mean log-loss + (L2 / 2)·‖w‖², bias not penalized, solved by Newton–Raphson (IRLS).
LOGISTIC_L2 = 0.02
LOGISTIC_MAX_ITERATIONS = 50
LOGISTIC_TOLERANCE = 1e-9
LOGISTIC_TRAINING_OBJECTIVE = "L2_REGULARIZED_LOGISTIC_IRLS_WITH_PLATT_CALIBRATION"

AI_FEATURES = (
    "return_1",
    "return_3",
    "return_5",
    "ma_fast_distance",
    "ma_slow_distance",
    "volatility_10",
    "momentum_10",
    "range_pct_1",
    "body_pct_1",
    "close_location_1",
    "atr_pct_14",
    "volume_zscore_20",
)
AI_REQUIRED_HISTORY = 20
OHLCV_FIELDS = ("open", "high", "low", "close", "volume")
# Input contract of artifacts that train and infer on OHLCV candles with the IRLS fit (IA0).
# Artifacts of earlier schemas were evaluated on closes only and are refused at runtime.
AI_FEATURE_SCHEMA_VERSION = "probabilistic_features_v3_ohlcv_strict"
AI_INPUT_SERIES_TYPE = "CANDLE_OHLCV"
AI_MODEL_SCHEMA_VERSION = 3


def has_ohlcv(item: Any) -> bool:
    """Whether ``item`` is a candle: the features read open, high, low, close and volume.

    A bare price makes ``extract_ai_features`` fall back to the close for the other fields,
    which zeroes the range, body, close location and volume features. Training and inference
    must both receive candles, or the model sees different inputs in each.
    """

    if isinstance(item, dict):
        return all(field in item for field in OHLCV_FIELDS)
    return all(hasattr(item, field) for field in OHLCV_FIELDS)


def extract_ai_features(prices: Sequence[Any]) -> dict[str, Any]:
    values = [_to_float(price) for price in prices]
    if len(values) < AI_REQUIRED_HISTORY:
        return {
            "ready": False,
            "reason": "AI_INSUFFICIENT_HISTORY",
            "required_history": AI_REQUIRED_HISTORY,
            "available_history": len(values),
            "features": {},
        }

    current = values[-1]
    fast_ma = _mean(values[-5:])
    slow_ma = _mean(values[-20:])
    recent_returns = [
        _safe_return(values[index], values[index - 1])
        for index in range(len(values) - 10, len(values))
    ]
    current_item = prices[-1]
    current_open = _market_value(current_item, "open", current)
    current_high = _market_value(current_item, "high", current)
    current_low = _market_value(current_item, "low", current)
    candle_range = max(0.0, current_high - current_low)
    range_pct = candle_range / current if current > 0 else 0.0
    body_pct = abs(current - current_open) / current if current > 0 else 0.0
    close_location = (current - current_low) / candle_range - 0.5 if candle_range > 0 else 0.0
    true_ranges: list[float] = []
    start = max(1, len(prices) - 14)
    for index in range(start, len(prices)):
        item = prices[index]
        high = _market_value(item, "high", values[index])
        low = _market_value(item, "low", values[index])
        previous_close = values[index - 1]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr_pct = _mean(true_ranges) / current if current > 0 and true_ranges else 0.0
    volume_history = [
        _market_value(item, "volume", 0.0) for item in prices[-min(20, len(prices)) : -1]
    ]
    current_volume = _market_value(current_item, "volume", 0.0)
    volume_mean = _mean(volume_history)
    volume_stddev = _stddev(volume_history)
    volume_zscore = (
        (current_volume - volume_mean) / volume_stddev
        if volume_history and volume_stddev > 0
        else 0.0
    )

    return {
        "ready": True,
        "reason": "AI_FEATURES_READY",
        "required_history": AI_REQUIRED_HISTORY,
        "available_history": len(values),
        "features": {
            "return_1": _safe_return(current, values[-2]),
            "return_3": _safe_return(current, values[-4]),
            "return_5": _safe_return(current, values[-6]),
            "ma_fast_distance": _safe_return(current, fast_ma),
            "ma_slow_distance": _safe_return(current, slow_ma),
            "volatility_10": _stddev(recent_returns),
            "momentum_10": _safe_return(current, values[-11]),
            "range_pct_1": range_pct,
            "body_pct_1": body_pct,
            "close_location_1": close_location,
            "atr_pct_14": atr_pct,
            "volume_zscore_20": volume_zscore,
        },
    }


def save_ai_model(model: dict[str, Any], path: str | Path) -> Path:
    _validate_probabilistic_model(model)
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(model, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    return output_path


def load_ai_model(path: str | Path) -> dict[str, Any]:
    model_path = Path(path)
    with model_path.open("r", encoding="utf-8") as file:
        model = json.load(file)
    if not isinstance(model, dict):
        raise ValueError("Modelo IA precisa ser um objeto JSON")
    _validate_probabilistic_model(model)
    return model


def _validate_probabilistic_model(model: dict[str, Any]) -> None:
    if model.get("model_type") != PROBABILISTIC_MODEL_TYPE:
        raise ValueError(f"Modelo IA probabilistico incompativel: {model.get('model_type')}")

    features = model.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("Modelo IA probabilistico sem lista de features")

    for key in ("weights_long", "weights_short"):
        weights = model.get(key)
        if not isinstance(weights, dict):
            raise ValueError(f"Modelo IA probabilistico sem {key}")
        missing_weights = [feature for feature in features if feature not in weights]
        if missing_weights:
            raise ValueError(f"Modelo IA probabilistico sem {key} para: {missing_weights}")

    if not isinstance(model.get("calibration"), dict):
        raise ValueError("Modelo IA probabilistico sem calibration")

    if model.get("signal_mode") != PROBABILISTIC_SIGNAL_MODE:
        raise ValueError("Modelo IA probabilistico com signal_mode invalido")

    if model.get("event_definition") != PROBABILISTIC_EVENT_DEFINITION:
        raise ValueError("Modelo IA probabilistico com event_definition invalido")


def _feature_stats(rows: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    stats = {}
    for feature in AI_FEATURES:
        values = [row[feature] for row in rows]
        mean = _mean(values)
        stddev = _stddev(values)
        stats[feature] = {"mean": mean, "stddev": stddev or 1.0}
    return stats


def _standardize_features(
    features: dict[str, float],
    feature_stats: dict[str, Any],
) -> dict[str, float]:
    standardized = {}
    for feature in AI_FEATURES:
        stats = feature_stats.get(feature, {})
        mean = float(stats.get("mean", 0.0))
        stddev = float(stats.get("stddev", 1.0)) or 1.0
        value = (float(features.get(feature, 0.0)) - mean) / stddev
        standardized[feature] = max(-10.0, min(10.0, value))
    return standardized


def _fit_mean_difference_classifier(
    rows: list[dict[str, float]],
    labels: list[int],
) -> tuple[dict[str, float], float]:
    positive_rows = [row for row, label in zip(rows, labels, strict=True) if label == 1]
    negative_rows = [row for row, label in zip(rows, labels, strict=True) if label == 0]

    if not positive_rows:
        return {feature: 0.0 for feature in AI_FEATURES}, -1.0
    if not negative_rows:
        return {feature: 0.0 for feature in AI_FEATURES}, 1.0

    positive_mean = {
        feature: _mean([row[feature] for row in positive_rows]) for feature in AI_FEATURES
    }
    negative_mean = {
        feature: _mean([row[feature] for row in negative_rows]) for feature in AI_FEATURES
    }
    weights = {feature: positive_mean[feature] - negative_mean[feature] for feature in AI_FEATURES}
    midpoint = {
        feature: (positive_mean[feature] + negative_mean[feature]) / 2.0 for feature in AI_FEATURES
    }
    bias = -sum(weights[feature] * midpoint[feature] for feature in AI_FEATURES)
    return weights, bias


def _fit_regularized_logistic_classifier(
    rows: list[dict[str, float]],
    labels: list[int],
    *,
    l2: float = LOGISTIC_L2,
    max_iterations: int = LOGISTIC_MAX_ITERATIONS,
    tolerance: float = LOGISTIC_TOLERANCE,
) -> tuple[dict[str, float], float]:
    """Fit an L2-regularized logistic classifier to convergence (IRLS).

    Minimizes the mean log-loss plus ``l2 / 2 · ‖w‖²`` (the bias is not penalized) with
    Newton–Raphson steps and a backtracking line search, until the largest parameter change
    falls below ``tolerance``. Artifacts stay JSON-only and inference needs no numeric
    library: with 12 features the Hessian is 13 × 13, solved by Gaussian elimination.
    """

    zero = {feature: 0.0 for feature in AI_FEATURES}
    if not rows or len(rows) != len(labels):
        return zero, 0.0

    positive_rate = min(1.0 - 1e-6, max(1e-6, sum(labels) / len(labels)))
    bias = math.log(positive_rate / (1.0 - positive_rate))
    if positive_rate <= 1e-6 or positive_rate >= 1.0 - 1e-6:
        return zero, bias

    matrix = [tuple(float(row[feature]) for feature in AI_FEATURES) for row in rows]
    targets = [float(label) for label in labels]
    theta = [bias] + [0.0] * len(AI_FEATURES)
    penalty = float(l2)
    objective = _logistic_objective(matrix, targets, theta, penalty)

    for _ in range(max(1, int(max_iterations))):
        gradient, hessian = _logistic_gradient_and_hessian(matrix, targets, theta, penalty)
        direction = _solve_linear_system(hessian, gradient)
        if direction is None:
            break
        step = 1.0
        while True:
            candidate = [
                value - step * delta for value, delta in zip(theta, direction, strict=True)
            ]
            candidate_objective = _logistic_objective(matrix, targets, candidate, penalty)
            if candidate_objective <= objective + 1e-15 or step < 1e-6:
                break
            step /= 2.0
        change = max(abs(step * delta) for delta in direction)
        theta, objective = candidate, candidate_objective
        if change < float(tolerance):
            break

    return dict(zip(AI_FEATURES, theta[1:], strict=True)), theta[0]


def _logistic_objective(
    matrix: list[tuple[float, ...]],
    targets: list[float],
    theta: list[float],
    penalty: float,
) -> float:
    bias, weights = theta[0], theta[1:]
    total = 0.0
    for values, target in zip(matrix, targets, strict=True):
        score = bias + sum(weight * value for weight, value in zip(weights, values, strict=True))
        # log(1 + e^s) - y·s, computed without overflow.
        total += max(score, 0.0) + math.log1p(math.exp(-abs(score))) - target * score
    return total / len(targets) + 0.5 * penalty * sum(weight * weight for weight in weights)


def _logistic_gradient_and_hessian(
    matrix: list[tuple[float, ...]],
    targets: list[float],
    theta: list[float],
    penalty: float,
) -> tuple[list[float], list[list[float]]]:
    size = len(theta)
    bias, weights = theta[0], theta[1:]
    gradient = [0.0] * size
    hessian = [[0.0] * size for _ in range(size)]
    for values, target in zip(matrix, targets, strict=True):
        score = bias + sum(weight * value for weight, value in zip(weights, values, strict=True))
        probability = _stable_sigmoid(score)
        error = probability - target
        variance = max(probability * (1.0 - probability), 1e-12)
        extended = (1.0, *values)
        for row_index in range(size):
            component = extended[row_index]
            gradient[row_index] += error * component
            weighted = variance * component
            hessian_row = hessian[row_index]
            for column_index in range(row_index, size):
                hessian_row[column_index] += weighted * extended[column_index]
    count = float(len(targets))
    for row_index in range(size):
        gradient[row_index] /= count
        for column_index in range(row_index, size):
            hessian[row_index][column_index] /= count
            hessian[column_index][row_index] = hessian[row_index][column_index]
    for index in range(1, size):
        gradient[index] += penalty * theta[index]
        hessian[index][index] += penalty
    return gradient, hessian


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None when the system is singular."""

    size = len(vector)
    augmented = [list(row) + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-14:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        for row in range(column + 1, size):
            factor = augmented[row][column] / augmented[column][column]
            if factor:
                for index in range(column, size + 1):
                    augmented[row][index] -= factor * augmented[column][index]
    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        tail = sum(augmented[row][index] * solution[index] for index in range(row + 1, size))
        solution[row] = (augmented[row][size] - tail) / augmented[row][row]
    return solution


def _score_row(row: dict[str, float], weights: dict[str, float], bias: float) -> float:
    return float(bias) + sum(
        float(weights.get(feature, 0.0)) * float(row.get(feature, 0.0)) for feature in AI_FEATURES
    )


def _stable_sigmoid(value: float) -> float:
    if value >= 0:
        exponent = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + exponent)
    exponent = math.exp(max(value, -60.0))
    return exponent / (1.0 + exponent)


def _to_float(value: Any) -> float:
    if isinstance(value, dict) and "close" in value:
        value = value["close"]
    elif hasattr(value, "close"):
        value = value.close
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("Preco invalido para IA")
    return converted


def _market_value(value: Any, field: str, default: float) -> float:
    raw = default
    if isinstance(value, dict):
        raw = value.get(field, default)
    elif hasattr(value, field):
        raw = getattr(value, field)
    try:
        converted = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return converted if math.isfinite(converted) else float(default)


def _safe_return(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return (current / previous) - 1.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stddev(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)
