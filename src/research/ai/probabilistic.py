import math
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.ai.model import (
    AI_FEATURE_SCHEMA_VERSION,
    AI_FEATURES,
    AI_INPUT_SERIES_TYPE,
    AI_MODEL_SCHEMA_VERSION,
    AI_REQUIRED_HISTORY,
    LOGISTIC_TRAINING_OBJECTIVE,
    _feature_stats,
    _fit_regularized_logistic_classifier,
    _mean,
    _safe_return,
    _score_row,
    _standardize_features,
    _to_float,
    save_ai_model,
)
from domain.ai.probabilistic import (
    PROBABILISTIC_EVENT_DEFINITION,
    PROBABILISTIC_MODEL_TYPE,
    PROBABILISTIC_SCOPE_GLOBAL,
    PROBABILISTIC_SCOPE_SYMBOL_REFINED,
    PROBABILISTIC_SIGNAL_MODE,
    PROBABILITY_EPSILON,
    _clamp_probability,
    _event_probabilities_from_scores,
    _sigmoid,
    load_probabilistic_ai_model,
)
from domain.ai.probabilistic import (
    predict_probabilistic_event as predict_probabilistic_event,
)
from research.ai.paths import (
    default_probabilistic_global_model_path,
    default_probabilistic_refined_model_path,
)
from research.ai.training_defaults import (
    DEFAULT_ESTIMATED_ROUND_TRIP_COST_BPS,
    DEFAULT_HORIZON_CANDLES,
    DEFAULT_MIN_TRAINING_ROWS,
    DEFAULT_THRESHOLD_BPS,
    validate_label_threshold,
)


def train_probabilistic_linear_ai_model(
    prices: Sequence[Any],
    *,
    symbol: str,
    interval: str,
    scope: str = PROBABILISTIC_SCOPE_SYMBOL_REFINED,
    horizon_candles: int = DEFAULT_HORIZON_CANDLES,
    threshold_bps: float = DEFAULT_THRESHOLD_BPS,
    estimated_round_trip_cost_bps: float = DEFAULT_ESTIMATED_ROUND_TRIP_COST_BPS,
    min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS,
    parent_global_model_id: str = "",
    parent_global_model_path: str = "",
    model_id: str | None = None,
) -> dict[str, Any]:
    return train_probabilistic_linear_ai_model_from_price_series(
        [prices],
        symbol=symbol,
        interval=interval,
        scope=scope,
        horizon_candles=horizon_candles,
        threshold_bps=threshold_bps,
        estimated_round_trip_cost_bps=estimated_round_trip_cost_bps,
        min_training_rows=min_training_rows,
        trained_symbols=[symbol],
        parent_global_model_id=parent_global_model_id,
        parent_global_model_path=parent_global_model_path,
        model_id=model_id,
    )


def train_probabilistic_linear_ai_model_from_price_series(
    price_series: Sequence[Sequence[Any]],
    *,
    symbol: str,
    interval: str,
    scope: str = PROBABILISTIC_SCOPE_GLOBAL,
    horizon_candles: int = DEFAULT_HORIZON_CANDLES,
    threshold_bps: float = DEFAULT_THRESHOLD_BPS,
    estimated_round_trip_cost_bps: float = DEFAULT_ESTIMATED_ROUND_TRIP_COST_BPS,
    min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS,
    trained_symbols: Sequence[str] | None = None,
    parent_global_model_id: str = "",
    parent_global_model_path: str = "",
    model_id: str | None = None,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").upper().strip()
    normalized_interval = str(interval or "").strip()
    normalized_scope = str(scope or "").upper().strip()

    if normalized_scope not in {PROBABILISTIC_SCOPE_GLOBAL, PROBABILISTIC_SCOPE_SYMBOL_REFINED}:
        raise ValueError(f"Escopo probabilistico invalido: {scope}")
    if normalized_scope == PROBABILISTIC_SCOPE_GLOBAL:
        normalized_symbol = "GLOBAL"
    if not normalized_symbol:
        raise ValueError("symbol nao pode ficar vazio")
    if not normalized_interval:
        raise ValueError("interval nao pode ficar vazio")
    if int(horizon_candles) <= 0:
        raise ValueError("horizon_candles precisa ser maior que zero")
    if float(threshold_bps) <= 0:
        raise ValueError("threshold_bps precisa ser maior que zero")
    if float(estimated_round_trip_cost_bps) < 0:
        raise ValueError("estimated_round_trip_cost_bps nao pode ser negativo")
    validate_label_threshold(threshold_bps, estimated_round_trip_cost_bps)
    if int(min_training_rows) <= 0:
        raise ValueError("min_training_rows precisa ser maior que zero")
    if not price_series:
        raise ValueError("Treino IA probabilistica exige ao menos uma serie de precos")

    train_rows: list[dict[str, float]] = []
    train_long_labels: list[int] = []
    train_short_labels: list[int] = []
    train_future_returns_bps: list[float] = []
    validation_rows: list[dict[str, float]] = []
    validation_long_labels: list[int] = []
    validation_short_labels: list[int] = []
    test_rows: list[dict[str, float]] = []
    test_long_labels: list[int] = []
    test_short_labels: list[int] = []
    test_future_returns_bps: list[float] = []

    for prices in price_series:
        values = list(prices)
        series_rows, series_long, series_short, series_returns = (
            _probabilistic_training_rows_from_values(
                values,
                horizon_candles=int(horizon_candles),
                threshold_bps=float(threshold_bps),
            )
        )
        split = _purged_chronological_split(
            series_rows,
            series_long,
            series_short,
            series_returns,
            purge_rows=int(horizon_candles),
        )
        train_rows.extend(split["train"][0])
        train_long_labels.extend(split["train"][1])
        train_short_labels.extend(split["train"][2])
        train_future_returns_bps.extend(split["train"][3])
        validation_rows.extend(split["validation"][0])
        validation_long_labels.extend(split["validation"][1])
        validation_short_labels.extend(split["validation"][2])
        test_rows.extend(split["test"][0])
        test_long_labels.extend(split["test"][1])
        test_short_labels.extend(split["test"][2])
        test_future_returns_bps.extend(split["test"][3])

    if len(train_rows) < int(min_training_rows):
        raise ValueError(
            "Treino IA probabilistica exige pelo menos "
            f"{int(min_training_rows)} linhas de treino purgado; disponiveis={len(train_rows)}"
        )
    if not validation_rows or not test_rows:
        raise ValueError(
            "Treino IA probabilistica exige janelas purgadas de validacao e teste nao vazias"
        )

    normalized_trained_symbols = [
        str(item).upper().strip() for item in list(trained_symbols or []) if str(item).strip()
    ]
    if not normalized_trained_symbols:
        normalized_trained_symbols = [normalized_symbol]

    feature_stats = _feature_stats(train_rows)
    standardized_rows = [_standardize_features(row, feature_stats) for row in train_rows]
    weights_long, bias_long = _fit_regularized_logistic_classifier(
        standardized_rows,
        train_long_labels,
    )
    weights_short, bias_short = _fit_regularized_logistic_classifier(
        standardized_rows,
        train_short_labels,
    )

    calibration_rows = validation_rows
    calibration_long_labels = validation_long_labels
    calibration_short_labels = validation_short_labels
    standardized_calibration = [
        _standardize_features(row, feature_stats) for row in calibration_rows
    ]
    long_scores = [_score_row(row, weights_long, bias_long) for row in standardized_calibration]
    short_scores = [_score_row(row, weights_short, bias_short) for row in standardized_calibration]
    calibration = _probability_calibration(
        long_scores=long_scores,
        short_scores=short_scores,
        long_labels=calibration_long_labels,
        short_labels=calibration_short_labels,
    )
    evaluation_rows = test_rows
    evaluation_long_labels = test_long_labels
    evaluation_short_labels = test_short_labels
    standardized_evaluation = [_standardize_features(row, feature_stats) for row in evaluation_rows]
    evaluation_long_scores = [
        _score_row(row, weights_long, bias_long) for row in standardized_evaluation
    ]
    evaluation_short_scores = [
        _score_row(row, weights_short, bias_short) for row in standardized_evaluation
    ]
    probabilities = [
        _event_probabilities_from_scores(
            long_score,
            short_score,
            calibration=calibration,
        )
        for long_score, short_score in zip(
            evaluation_long_scores,
            evaluation_short_scores,
            strict=True,
        )
    ]

    long_predictions = [1 if item["p_long_event"] >= 0.5 else 0 for item in probabilities]
    short_predictions = [1 if item["p_short_event"] >= 0.5 else 0 for item in probabilities]
    long_accuracy = _binary_accuracy(long_predictions, evaluation_long_labels)
    short_accuracy = _binary_accuracy(short_predictions, evaluation_short_labels)
    neutral_labels = [
        1 if long_label == 0 and short_label == 0 else 0
        for long_label, short_label in zip(
            evaluation_long_labels,
            evaluation_short_labels,
            strict=True,
        )
    ]
    long_probabilities = [float(item["p_long_event"]) for item in probabilities]
    short_probabilities = [float(item["p_short_event"]) for item in probabilities]
    neutral_probabilities = [float(item["p_neutral_event"]) for item in probabilities]
    payoff_profile = _payoff_profile(
        train_future_returns_bps,
        threshold_bps=float(threshold_bps),
    )
    economic_metrics = _out_of_sample_economic_metrics(
        probabilities=probabilities,
        future_returns_bps=test_future_returns_bps,
        payoff_profile=payoff_profile,
        threshold_bps=float(threshold_bps),
        estimated_round_trip_cost_bps=float(estimated_round_trip_cost_bps),
    )

    resolved_model_id = model_id or _default_probabilistic_model_id(
        scope=normalized_scope,
        symbol=normalized_symbol,
        interval=normalized_interval,
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metrics = {
        "accuracy": (long_accuracy + short_accuracy) / 2.0,
        "long_accuracy": long_accuracy,
        "short_accuracy": short_accuracy,
        "direction_accuracy": (long_accuracy + short_accuracy) / 2.0,
        "positive_labels": sum(evaluation_long_labels),
        "negative_labels": len(evaluation_long_labels) - sum(evaluation_long_labels),
        "short_positive_labels": sum(evaluation_short_labels),
        "short_negative_labels": len(evaluation_short_labels) - sum(evaluation_short_labels),
        "neutral_labels": sum(neutral_labels),
        "neutral_rate": sum(neutral_labels) / len(neutral_labels),
        "avg_future_return_bps": _mean(test_future_returns_bps),
        "avg_abs_future_return_bps": _mean([abs(value) for value in test_future_returns_bps]),
        "series_count": len(price_series),
        "trained_symbols": normalized_trained_symbols,
        "training_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "test_rows": len(test_rows),
        "purge_rows": int(horizon_candles),
        "metrics_source": "PURGED_OUT_OF_SAMPLE_TEST",
        "long_brier_score": _binary_brier_score(
            long_probabilities,
            evaluation_long_labels,
        ),
        "short_brier_score": _binary_brier_score(
            short_probabilities,
            evaluation_short_labels,
        ),
        "multiclass_brier_score": _multiclass_brier_score(
            probabilities,
            evaluation_long_labels,
            evaluation_short_labels,
        ),
        "long_log_loss": _binary_log_loss(long_probabilities, evaluation_long_labels),
        "short_log_loss": _binary_log_loss(short_probabilities, evaluation_short_labels),
        "long_ece": _expected_calibration_error(
            long_probabilities,
            evaluation_long_labels,
        ),
        "short_ece": _expected_calibration_error(
            short_probabilities,
            evaluation_short_labels,
        ),
        "neutral_probability_mean": _mean(neutral_probabilities),
        **economic_metrics,
    }
    readiness_status = _model_readiness_status(
        training_rows=len(train_rows),
        test_rows=len(test_rows),
        metrics=metrics,
    )
    approved_for = ["PAPER_LIVE"] if readiness_status == "PAPER_CANDIDATE" else []
    blocked_for = ["TESTNET_LIVE", "REAL_LIVE"]
    if readiness_status == "RESEARCH_ONLY":
        blocked_for.insert(0, "PAPER_LIVE")

    return {
        "model_id": resolved_model_id,
        "model_type": PROBABILISTIC_MODEL_TYPE,
        "model_schema_version": AI_MODEL_SCHEMA_VERSION,
        "model_family": "PROBABILISTIC_LINEAR_AI",
        "signal_mode": PROBABILISTIC_SIGNAL_MODE,
        "scope": normalized_scope,
        "symbol": normalized_symbol,
        "interval": normalized_interval,
        "features": list(AI_FEATURES),
        "weights_long": weights_long,
        "bias_long": bias_long,
        "weights_short": weights_short,
        "bias_short": bias_short,
        "horizon_candles": int(horizon_candles),
        "threshold_bps": float(threshold_bps),
        "estimated_round_trip_cost_bps": float(estimated_round_trip_cost_bps),
        "required_history": AI_REQUIRED_HISTORY,
        "feature_stats": feature_stats,
        "calibration": calibration,
        "payoff_profile": payoff_profile,
        "training_objective": LOGISTIC_TRAINING_OBJECTIVE,
        "trained_at": now,
        "training_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "test_rows": len(test_rows),
        "purge_rows": int(horizon_candles),
        "trained_symbols": normalized_trained_symbols,
        "parent_global_model_id": str(parent_global_model_id or ""),
        "parent_global_model_path": str(parent_global_model_path or ""),
        "approved_for": approved_for,
        "blocked_for": blocked_for,
        "readiness_status": readiness_status,
        "created_by": "ProbabilisticAITrainingService",
        "metrics": metrics,
        "event_definition": PROBABILISTIC_EVENT_DEFINITION,
        "input_contract": {
            "timeframe": normalized_interval,
            "series_type": AI_INPUT_SERIES_TYPE,
            "required_history": AI_REQUIRED_HISTORY,
            "closed_candles_only": True,
            "feature_schema_version": AI_FEATURE_SCHEMA_VERSION,
        },
    }


def train_probabilistic_global_model_from_csvs(
    csv_by_symbol: dict[str, str | Path],
    *,
    interval: str,
    horizon_candles: int = DEFAULT_HORIZON_CANDLES,
    threshold_bps: float = DEFAULT_THRESHOLD_BPS,
    min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS,
    output_path: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    from research.data_loader import load_candles_from_csv

    if not csv_by_symbol:
        raise ValueError("Treino probabilistico global exige ao menos um CSV por simbolo")

    normalized: dict[str, list[Any]] = {}
    for symbol, csv_path in csv_by_symbol.items():
        normalized_symbol = str(symbol).upper().strip()
        if not normalized_symbol:
            raise ValueError("Treino probabilistico recebeu simbolo vazio")
        candles = load_candles_from_csv(str(csv_path), interval=interval, symbol=normalized_symbol)
        normalized[normalized_symbol] = list(candles)

    model = train_probabilistic_linear_ai_model_from_price_series(
        list(normalized.values()),
        symbol="GLOBAL",
        interval=interval,
        scope=PROBABILISTIC_SCOPE_GLOBAL,
        horizon_candles=horizon_candles,
        threshold_bps=threshold_bps,
        min_training_rows=min_training_rows,
        trained_symbols=list(normalized.keys()),
    )
    path = (
        Path(output_path)
        if output_path
        else default_probabilistic_global_model_path(
            interval=interval,
        )
    )
    return model, save_ai_model(model, path)


def train_probabilistic_refined_model_from_csv(
    csv_path: str | Path,
    *,
    symbol: str,
    interval: str,
    global_model_path: str | Path,
    horizon_candles: int = DEFAULT_HORIZON_CANDLES,
    threshold_bps: float = DEFAULT_THRESHOLD_BPS,
    min_training_rows: int = DEFAULT_MIN_TRAINING_ROWS,
    output_path: str | Path | None = None,
) -> tuple[dict[str, Any], Path]:
    from research.data_loader import load_candles_from_csv

    normalized_symbol = str(symbol or "").upper().strip()
    if not normalized_symbol:
        raise ValueError("Modelo refinado probabilistico exige symbol")

    global_model = load_probabilistic_ai_model(global_model_path)
    candles = load_candles_from_csv(str(csv_path), interval=interval, symbol=normalized_symbol)
    model = train_probabilistic_linear_ai_model(
        candles,
        symbol=normalized_symbol,
        interval=interval,
        scope=PROBABILISTIC_SCOPE_SYMBOL_REFINED,
        horizon_candles=horizon_candles,
        threshold_bps=threshold_bps,
        min_training_rows=min_training_rows,
        parent_global_model_id=str(global_model.get("model_id", "")),
        parent_global_model_path=str(global_model_path),
    )
    path = (
        Path(output_path)
        if output_path
        else default_probabilistic_refined_model_path(
            symbol=normalized_symbol,
            interval=interval,
        )
    )
    return model, save_ai_model(model, path)


def default_probabilistic_model_path(
    *,
    symbol: str,
    interval: str,
    scope: str,
) -> Path:
    normalized_scope = str(scope or "").upper().strip()
    if normalized_scope == PROBABILISTIC_SCOPE_GLOBAL:
        return default_probabilistic_global_model_path(interval=interval)
    return default_probabilistic_refined_model_path(symbol=symbol, interval=interval)


def _probabilistic_training_rows_from_values(
    values: list[Any],
    *,
    horizon_candles: int,
    threshold_bps: float,
) -> tuple[list[dict[str, float]], list[int], list[int], list[float]]:
    from domain.ai.model import extract_ai_features, has_ohlcv

    if values and not (has_ohlcv(values[0]) and has_ohlcv(values[-1])):
        raise ValueError(
            "Treino IA probabilistica exige candles OHLCV (open, high, low, close, volume); "
            "precos soltos zeram as features de amplitude, corpo e volume"
        )
    rows: list[dict[str, float]] = []
    long_labels: list[int] = []
    short_labels: list[int] = []
    future_returns_bps: list[float] = []

    for index in range(AI_REQUIRED_HISTORY - 1, len(values) - int(horizon_candles)):
        # Every feature is bounded by ``AI_REQUIRED_HISTORY``. Keeping only
        # that rolling window avoids quadratic training time on annual data.
        feature_window = values[index - AI_REQUIRED_HISTORY + 1 : index + 1]
        feature_result = extract_ai_features(feature_window)
        if not feature_result["ready"]:
            continue

        current = _to_float(values[index])
        future = _to_float(values[index + int(horizon_candles)])
        future_return_bps = _safe_return(future, current) * 10000.0
        rows.append(dict(feature_result["features"]))
        long_labels.append(1 if future_return_bps >= float(threshold_bps) else 0)
        short_labels.append(1 if future_return_bps <= -float(threshold_bps) else 0)
        future_returns_bps.append(future_return_bps)

    return rows, long_labels, short_labels, future_returns_bps


def _purged_chronological_split(
    rows: list[dict[str, float]],
    long_labels: list[int],
    short_labels: list[int],
    future_returns: list[float],
    *,
    purge_rows: int,
) -> dict[str, tuple[list, list[int], list[int], list[float]]]:
    total = len(rows)
    train_boundary = int(total * 0.70)
    validation_boundary = int(total * 0.85)
    purge = max(0, int(purge_rows))
    train_end = max(0, train_boundary - purge)
    validation_end = max(train_boundary, validation_boundary - purge)

    def sliced(start: int, end: int) -> tuple[list, list[int], list[int], list[float]]:
        return (
            rows[start:end],
            long_labels[start:end],
            short_labels[start:end],
            future_returns[start:end],
        )

    return {
        "train": sliced(0, train_end),
        "validation": sliced(train_boundary, validation_end),
        "test": sliced(validation_boundary, total),
    }


def _probability_calibration(
    *,
    long_scores: list[float],
    short_scores: list[float],
    long_labels: list[int],
    short_labels: list[int],
) -> dict[str, float | str]:
    long_rate = _label_rate(long_labels)
    short_rate = _label_rate(short_labels)
    neutral_rate = max(PROBABILITY_EPSILON, 1.0 - long_rate - short_rate)
    long_scale, long_intercept = _fit_platt_scaler(long_scores, long_labels)
    short_scale, short_intercept = _fit_platt_scaler(short_scores, short_labels)
    return {
        "method": "platt_scaling_v2",
        "long_base_rate": long_rate,
        "short_base_rate": short_rate,
        "neutral_base_rate": neutral_rate,
        "long_scale": long_scale,
        "long_intercept": long_intercept,
        "short_scale": short_scale,
        "short_intercept": short_intercept,
        # Legacy aliases retained so older readers can still inspect artifacts.
        "long_logit_bias": _logit(long_rate) - _mean(long_scores),
        "short_logit_bias": _logit(short_rate) - _mean(short_scores),
        "probability_floor": PROBABILITY_EPSILON,
    }


def _fit_platt_scaler(scores: list[float], labels: list[int]) -> tuple[float, float]:
    if not scores or len(scores) != len(labels):
        return 1.0, 0.0
    rate = _label_rate(labels)
    if rate <= PROBABILITY_EPSILON or rate >= 1.0 - PROBABILITY_EPSILON:
        return 0.0, _logit(rate)

    scale = 1.0
    intercept = _logit(rate) - _mean(scores)
    l2 = 1e-3
    for _ in range(25):
        gradient_scale = l2 * scale
        gradient_intercept = 0.0
        hessian_scale = l2
        hessian_cross = 0.0
        hessian_intercept = 0.0
        for score, label in zip(scores, labels, strict=True):
            probability = _sigmoid(scale * score + intercept)
            error = probability - float(label)
            variance = max(1e-8, probability * (1.0 - probability))
            gradient_scale += error * score
            gradient_intercept += error
            hessian_scale += variance * score * score
            hessian_cross += variance * score
            hessian_intercept += variance

        determinant = hessian_scale * hessian_intercept - hessian_cross * hessian_cross
        if determinant <= 1e-12:
            break
        delta_scale = (
            gradient_scale * hessian_intercept - gradient_intercept * hessian_cross
        ) / determinant
        delta_intercept = (
            gradient_intercept * hessian_scale - gradient_scale * hessian_cross
        ) / determinant
        scale -= delta_scale
        intercept -= delta_intercept
        scale = max(-20.0, min(20.0, scale))
        intercept = max(-40.0, min(40.0, intercept))
        if abs(delta_scale) + abs(delta_intercept) < 1e-7:
            break
    return scale, intercept


def _model_readiness_status(
    *,
    training_rows: int,
    test_rows: int,
    metrics: dict[str, Any],
) -> str:
    calibration_error = max(
        float(metrics.get("long_ece", 1.0)),
        float(metrics.get("short_ece", 1.0)),
    )
    selected_trades = int(metrics.get("oos_selected_trades", 0) or 0)
    net_expectancy_bps = float(metrics.get("oos_net_expectancy_bps", 0.0) or 0.0)
    profit_factor = float(metrics.get("oos_profit_factor", 0.0) or 0.0)
    if (
        training_rows >= 2_000
        and test_rows >= 500
        and calibration_error <= 0.10
        and selected_trades >= 100
        and net_expectancy_bps > 0.0
        and profit_factor >= 1.10
    ):
        return "PAPER_CANDIDATE"
    return "RESEARCH_ONLY"


def _payoff_profile(
    future_returns_bps: list[float],
    *,
    threshold_bps: float,
) -> dict[str, float | int | str]:
    long_events = [value for value in future_returns_bps if value >= threshold_bps]
    short_events = [value for value in future_returns_bps if value <= -threshold_bps]
    neutral_events = [
        value for value in future_returns_bps if -threshold_bps < value < threshold_bps
    ]
    return {
        "long_event_mean_bps": _mean(long_events) if long_events else threshold_bps,
        "short_event_mean_bps": _mean(short_events) if short_events else -threshold_bps,
        "neutral_event_mean_bps": _mean(neutral_events) if neutral_events else 0.0,
        "long_event_count": len(long_events),
        "short_event_count": len(short_events),
        "neutral_event_count": len(neutral_events),
        "source": "PURGED_TRAINING_RETURNS",
    }


def _out_of_sample_economic_metrics(
    *,
    probabilities: list[dict[str, float]],
    future_returns_bps: list[float],
    payoff_profile: dict[str, float | int | str],
    threshold_bps: float,
    estimated_round_trip_cost_bps: float,
    min_probability: float = 0.62,
    min_direction_gap: float = 0.10,
    min_expected_value_bps: float = 3.0,
) -> dict[str, float | int | str]:
    """Measure economic usefulness on the untouched chronological test split.

    Calibration alone can approve a constant, non-tradable model. This check
    applies the same probability, direction-gap, cost and expected-value logic
    used at runtime, then scores only the selected observations against their
    realized future returns.
    """

    long_event_return = float(payoff_profile.get("long_event_mean_bps", threshold_bps))
    short_event_return = float(payoff_profile.get("short_event_mean_bps", -threshold_bps))
    neutral_event_return = float(payoff_profile.get("neutral_event_mean_bps", 0.0))
    realized_net_returns: list[float] = []

    for item, future_return in zip(probabilities, future_returns_bps, strict=True):
        p_long = float(item["p_long_event"])
        p_short = float(item["p_short_event"])
        p_neutral = float(item["p_neutral_event"])
        expected_long_gross = (
            p_long * long_event_return
            + p_short * short_event_return
            + p_neutral * neutral_event_return
        )
        expected_short_gross = -expected_long_gross
        direction = 0
        if (
            p_long >= min_probability
            and p_long >= p_short
            and p_long - p_short >= min_direction_gap
            and expected_long_gross - estimated_round_trip_cost_bps >= min_expected_value_bps
        ):
            direction = 1
        elif (
            p_short >= min_probability
            and p_short > p_long
            and p_short - p_long >= min_direction_gap
            and expected_short_gross - estimated_round_trip_cost_bps >= min_expected_value_bps
        ):
            direction = -1
        if direction:
            realized_net_returns.append(
                direction * float(future_return) - estimated_round_trip_cost_bps
            )

    gains = sum(value for value in realized_net_returns if value > 0.0)
    losses = -sum(value for value in realized_net_returns if value < 0.0)
    profit_factor = gains / losses if losses > 0.0 else (999.0 if gains > 0.0 else 0.0)
    return {
        "oos_selected_trades": len(realized_net_returns),
        "oos_selection_rate": (
            len(realized_net_returns) / len(future_returns_bps) if future_returns_bps else 0.0
        ),
        "oos_net_expectancy_bps": _mean(realized_net_returns),
        "oos_net_total_bps": sum(realized_net_returns),
        "oos_win_rate": (
            sum(value > 0.0 for value in realized_net_returns) / len(realized_net_returns)
            if realized_net_returns
            else 0.0
        ),
        "oos_profit_factor": min(999.0, profit_factor),
        "oos_economic_metric_source": "PURGED_CHRONOLOGICAL_TEST_COST_AWARE",
        "oos_policy_min_probability": min_probability,
        "oos_policy_min_direction_gap": min_direction_gap,
        "oos_policy_min_expected_value_bps": min_expected_value_bps,
        "oos_policy_round_trip_cost_bps": estimated_round_trip_cost_bps,
    }


def _binary_brier_score(probabilities: list[float], labels: list[int]) -> float:
    if not labels:
        return 1.0
    return _mean(
        [
            (probability - float(label)) ** 2
            for probability, label in zip(probabilities, labels, strict=True)
        ]
    )


def _multiclass_brier_score(
    probabilities: list[dict[str, float]],
    long_labels: list[int],
    short_labels: list[int],
) -> float:
    if not probabilities:
        return 1.0
    losses = []
    for item, long_label, short_label in zip(
        probabilities,
        long_labels,
        short_labels,
        strict=True,
    ):
        neutral_label = 1 if long_label == 0 and short_label == 0 else 0
        losses.append(
            (
                (float(item["p_long_event"]) - long_label) ** 2
                + (float(item["p_short_event"]) - short_label) ** 2
                + (float(item["p_neutral_event"]) - neutral_label) ** 2
            )
            / 3.0
        )
    return _mean(losses)


def _binary_log_loss(probabilities: list[float], labels: list[int]) -> float:
    if not labels:
        return 0.0
    losses = []
    for probability, label in zip(probabilities, labels, strict=True):
        clipped = _clamp_probability(probability)
        losses.append(
            -(float(label) * math.log(clipped) + (1.0 - float(label)) * math.log(1.0 - clipped))
        )
    return _mean(losses)


def _expected_calibration_error(
    probabilities: list[float],
    labels: list[int],
    *,
    bins: int = 10,
) -> float:
    if not labels:
        return 1.0
    total = len(labels)
    error = 0.0
    for index in range(max(1, bins)):
        lower = index / bins
        upper = (index + 1) / bins
        members = [
            (probability, label)
            for probability, label in zip(probabilities, labels, strict=True)
            if lower <= probability < upper or (index == bins - 1 and probability == 1.0)
        ]
        if not members:
            continue
        confidence = _mean([item[0] for item in members])
        observed = _mean([float(item[1]) for item in members])
        error += len(members) / total * abs(confidence - observed)
    return error


def _binary_accuracy(predictions: list[int], labels: list[int]) -> float:
    if not labels:
        return 0.0
    return sum(
        1 for prediction, label in zip(predictions, labels, strict=True) if prediction == label
    ) / len(labels)


def _label_rate(labels: list[int]) -> float:
    if not labels:
        return 0.5
    return _clamp_probability(sum(labels) / len(labels))


def _logit(value: float) -> float:
    probability = _clamp_probability(value)
    return math.log(probability / (1.0 - probability))


def _default_probabilistic_model_id(*, scope: str, symbol: str, interval: str) -> str:
    safe_interval = _safe_identifier(interval)
    if scope == PROBABILISTIC_SCOPE_GLOBAL:
        return f"GLOBAL_{safe_interval}_prob_ai"
    return f"{_safe_identifier(symbol)}_{safe_interval}_prob_refined_ai"


def _safe_identifier(value: str) -> str:
    return (
        "".join(char for char in str(value).upper().strip() if char.isalnum() or char in {"_", "-"})
        or "UNKNOWN"
    )
