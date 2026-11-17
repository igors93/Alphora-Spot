import pytest

from research.ai.probabilistic import (
    PROBABILISTIC_SCOPE_GLOBAL,
    predict_probabilistic_event,
    train_probabilistic_linear_ai_model_from_price_series,
)
from tests.fakes import candles_from_closes


def _prices(count: int = 180) -> list[float]:
    return [100.0 + index * 0.04 + (0.10 if index % 11 == 0 else 0.0) for index in range(count)]


def _model() -> dict:
    return train_probabilistic_linear_ai_model_from_price_series(
        [candles_from_closes(_prices())],
        symbol="GLOBAL",
        interval="1m",
        scope=PROBABILISTIC_SCOPE_GLOBAL,
        horizon_candles=3,
        threshold_bps=20.0,
        estimated_round_trip_cost_bps=15.0,
        min_training_rows=40,
        trained_symbols=["BTCUSDT"],
    )


def test_training_reports_calibration_and_probability_quality_metrics():
    model = _model()

    assert model["calibration"]["method"] == "platt_scaling_v2"
    assert model["training_objective"] == "L2_REGULARIZED_LOGISTIC_IRLS_WITH_PLATT_CALIBRATION"
    assert model["estimated_round_trip_cost_bps"] == 15.0
    assert model["payoff_profile"]["source"] == "PURGED_TRAINING_RETURNS"
    assert "multiclass_brier_score" in model["metrics"]
    assert "long_log_loss" in model["metrics"]
    assert "long_ece" in model["metrics"]
    assert model["metrics"]["oos_economic_metric_source"] == (
        "PURGED_CHRONOLOGICAL_TEST_COST_AWARE"
    )
    assert "oos_net_expectancy_bps" in model["metrics"]
    assert "oos_profit_factor" in model["metrics"]
    assert model["input_contract"]["series_type"] == "CANDLE_OHLCV"
    assert model["input_contract"]["feature_schema_version"] == (
        "probabilistic_features_v3_ohlcv_strict"
    )
    assert model["model_schema_version"] == 3
    assert model["readiness_status"] == "RESEARCH_ONLY"
    assert "REAL_LIVE" in model["blocked_for"]


def test_prediction_subtracts_round_trip_cost_before_opening():
    model = _model()
    without_cost = predict_probabilistic_event(
        model,
        candles_from_closes(_prices(80)),
        min_long_probability=0.0,
        min_short_probability=0.0,
        min_direction_gap=0.0,
        min_expected_value_bps=0.0,
        estimated_round_trip_cost_bps=0.0,
    )
    with_prohibitive_cost = predict_probabilistic_event(
        model,
        candles_from_closes(_prices(80)),
        min_long_probability=0.0,
        min_short_probability=0.0,
        min_direction_gap=0.0,
        min_expected_value_bps=0.0,
        estimated_round_trip_cost_bps=10_000.0,
    )

    assert without_cost["expected_value_bps"] >= with_prohibitive_cost["expected_value_bps"]
    assert with_prohibitive_cost["expected_value_bps"] == 0.0
    assert with_prohibitive_cost["action_candidate"] == "NO_ACTION"
    assert with_prohibitive_cost["estimated_round_trip_cost_bps"] == 10_000.0


def test_training_rejects_a_label_threshold_below_the_round_trip_cost():
    with pytest.raises(ValueError, match="custo de ida e volta"):
        train_probabilistic_linear_ai_model_from_price_series(
            [candles_from_closes(_prices())],
            symbol="GLOBAL",
            interval="1m",
            horizon_candles=3,
            threshold_bps=15.0,
            estimated_round_trip_cost_bps=15.0,
            min_training_rows=40,
        )


def test_training_defaults_are_shared_by_library_cli_and_desktop():
    import inspect

    from desktop.settings_store import DEFAULT_SETTINGS
    from research.ai.training_defaults import (
        DEFAULT_HORIZON_CANDLES,
        DEFAULT_MIN_TRAINING_ROWS,
        DEFAULT_THRESHOLD_BPS,
    )
    from research.cli import build_parser

    signature = inspect.signature(train_probabilistic_linear_ai_model_from_price_series)
    assert signature.parameters["horizon_candles"].default == DEFAULT_HORIZON_CANDLES
    assert signature.parameters["threshold_bps"].default == DEFAULT_THRESHOLD_BPS
    assert signature.parameters["min_training_rows"].default == DEFAULT_MIN_TRAINING_ROWS

    arguments = build_parser().parse_args(
        ["train-ai-probabilistic-global", "--symbols", "BTCUSDT", "--interval", "15m"]
    )
    assert arguments.horizon_candles == DEFAULT_HORIZON_CANDLES
    assert arguments.threshold_bps == DEFAULT_THRESHOLD_BPS
    assert arguments.min_training_rows == DEFAULT_MIN_TRAINING_ROWS

    assert int(DEFAULT_SETTINGS["ai_training_horizon_candles"]) == DEFAULT_HORIZON_CANDLES
    assert float(DEFAULT_SETTINGS["ai_training_threshold_bps"]) == DEFAULT_THRESHOLD_BPS
    assert int(DEFAULT_SETTINGS["ai_training_min_training_rows"]) == DEFAULT_MIN_TRAINING_ROWS
