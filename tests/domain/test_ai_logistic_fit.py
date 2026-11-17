"""IA0.2: the logistic fit converges to the maximum-likelihood weights."""

import math
import random

from domain.ai.model import (
    AI_FEATURES,
    _fit_regularized_logistic_classifier,
    _logistic_gradient_and_hessian,
    _solve_linear_system,
    _stable_sigmoid,
)

TRUE_WEIGHTS = {"return_1": 0.8, "momentum_10": -0.6, "volume_zscore_20": 0.5}
TRUE_BIAS = -1.2


def _synthetic(rows: int, *, seed: int) -> tuple[list[dict[str, float]], list[int]]:
    rng = random.Random(seed)
    data, labels = [], []
    for _ in range(rows):
        row = {feature: rng.gauss(0.0, 1.0) for feature in AI_FEATURES}
        score = TRUE_BIAS + sum(row[feature] * weight for feature, weight in TRUE_WEIGHTS.items())
        data.append(row)
        labels.append(1 if rng.random() < _stable_sigmoid(score) else 0)
    return data, labels


def test_fit_recovers_the_generating_weights_within_sampling_error():
    rows, labels = _synthetic(20_000, seed=11)

    weights, bias = _fit_regularized_logistic_classifier(rows, labels, l2=1e-6)

    # Standard errors from the inverse Fisher information at the solution.
    matrix = [tuple(row[feature] for feature in AI_FEATURES) for row in rows]
    theta = [bias, *(weights[feature] for feature in AI_FEATURES)]
    _gradient, hessian = _logistic_gradient_and_hessian(
        matrix, [float(label) for label in labels], theta, 1e-6
    )
    size = len(theta)
    standard_errors = []
    for index in range(size):
        unit = [1.0 if position == index else 0.0 for position in range(size)]
        column = _solve_linear_system(hessian, unit)
        assert column is not None
        standard_errors.append(math.sqrt(column[index] / len(rows)))

    assert abs(bias - TRUE_BIAS) <= 4 * standard_errors[0]
    for index, feature in enumerate(AI_FEATURES, start=1):
        expected = TRUE_WEIGHTS.get(feature, 0.0)
        assert abs(weights[feature] - expected) <= 4 * standard_errors[index], feature
    # The former 18-epoch gradient descent stopped at ~6% of these weights.
    assert weights["return_1"] > 0.7


def test_fit_stops_at_a_stationary_point_of_the_regularized_objective():
    rows, labels = _synthetic(3_000, seed=5)
    l2 = 0.02

    weights, bias = _fit_regularized_logistic_classifier(rows, labels, l2=l2)

    matrix = [tuple(row[feature] for feature in AI_FEATURES) for row in rows]
    theta = [bias, *(weights[feature] for feature in AI_FEATURES)]
    gradient, _hessian = _logistic_gradient_and_hessian(
        matrix, [float(label) for label in labels], theta, l2
    )
    assert max(abs(value) for value in gradient) < 1e-8


def test_fit_with_a_single_class_returns_the_base_rate_bias():
    rows, _labels = _synthetic(200, seed=1)

    weights, bias = _fit_regularized_logistic_classifier(rows, [0] * len(rows))

    assert all(value == 0.0 for value in weights.values())
    assert bias < -10


def test_fit_stays_finite_on_separable_data():
    rows = [{feature: float(index - 50) for feature in AI_FEATURES} for index in range(100)]
    labels = [1 if index >= 50 else 0 for index in range(100)]

    weights, bias = _fit_regularized_logistic_classifier(rows, labels, l2=0.02)

    assert all(math.isfinite(value) for value in weights.values())
    assert math.isfinite(bias)
