from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from config.symbol_context import symbol_config_from_app_config
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeAction
from governor.services import AIGateService
from research.ai.model import load_ai_model, save_ai_model
from research.ai.paths import (
    default_probabilistic_global_model_path,
    default_probabilistic_refined_model_path,
)
from research.ai.probabilistic import (
    PROBABILISTIC_MODEL_TYPE,
    PROBABILISTIC_SCOPE_GLOBAL,
    PROBABILISTIC_SCOPE_SYMBOL_REFINED,
    predict_probabilistic_event,
    train_probabilistic_linear_ai_model_from_price_series,
)
from research.ai.probabilistic_training_service import ProbabilisticAITrainingService
from research.ai.registry import AI_SCOPE_GLOBAL, list_ai_models
from scanner.signal_score import SignalScore
from strategies.futures.ai.probabilistic_resolver import (
    ProbabilisticAIModelInputContractMissing,
    ProbabilisticAIModelResolver,
    ProbabilisticAIModelSchemaOutdated,
    ProbabilisticAIRefinedGlobalTimeframeMismatch,
)
from strategies.futures.ai.probabilistic_strategy import ProbabilisticAIStrategy
from tests.fakes import candles_from_closes


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **payload):
        self.events.append({"event_type": event_type, **payload})


def _prices(rows=120, *, slope=0.03):
    return [100 + index * slope + (0.12 if index % 9 == 0 else 0.0) for index in range(rows)]


def _write_candles_csv(path, rows=120, *, slope=0.03):
    lines = ["timestamp,open,high,low,close,volume"]
    base = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    for index, close in enumerate(_prices(rows, slope=slope)):
        ts = (base + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{ts},{close},{close + 1},{close - 1},{close},10")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _train_prob_model(tmp_path, *, symbol="GLOBAL", scope=PROBABILISTIC_SCOPE_GLOBAL):
    model = train_probabilistic_linear_ai_model_from_price_series(
        [candles_from_closes(_prices())],
        symbol=symbol,
        interval="1m",
        scope=scope,
        horizon_candles=3,
        threshold_bps=1.0,
        estimated_round_trip_cost_bps=0.5,
        min_training_rows=20,
        trained_symbols=["BTCUSDT"] if scope == PROBABILISTIC_SCOPE_GLOBAL else [symbol],
    )
    path = tmp_path / f"{symbol.lower()}_{scope.lower()}.json"
    return model, save_ai_model(model, path)


def _market(prices=None, symbol="BTCUSDT"):
    candles = candles_from_closes(prices or _prices(40), symbol=symbol)
    values = [candle.close for candle in candles]
    return MarketSnapshot(
        symbol=symbol, price=values[-1], prices=values, candles={"1m": tuple(candles)}
    )


def _position(direction=PositionDirection.NONE):
    quantity = Decimal("1") if direction is not PositionDirection.NONE else Decimal("0")
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=direction,
        quantity=quantity,
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )


def _ai_metadata(**overrides):
    metadata = {
        "source": "AI_PROBABILISTIC",
        "model_type": PROBABILISTIC_MODEL_TYPE,
        "model_id": "GLOBAL_1M_prob_ai",
        "model_path": "models/ai/global/GLOBAL_1M_probabilistic_ai.json",
        "model_source": "GLOBAL",
        "fallback_used": False,
        "fallback_reason": "",
        "model_available": True,
        "p_long_event": 0.70,
        "p_short_event": 0.20,
        "p_neutral_event": 0.10,
        "expected_value_bps": 6.0,
        "approved_for": ["PAPER_LIVE", "TESTNET_LIVE"],
        "blocked_for": ["REAL_LIVE"],
    }
    metadata.update(overrides)
    return metadata


def test_probabilistic_global_training_service_saves_json_and_registry(tmp_path):
    btc_csv = tmp_path / "btc.csv"
    eth_csv = tmp_path / "eth.csv"
    registry_path = tmp_path / "registry.json"
    _write_candles_csv(btc_csv)
    _write_candles_csv(eth_csv, slope=-0.02)
    progress_events = []
    service = ProbabilisticAITrainingService(
        registry_path=registry_path,
        event_journal_path=tmp_path / "events.jsonl",
        progress_callback=progress_events.append,
    )

    result = service.train_global(
        symbols=["BTCUSDT", "ETHUSDT"],
        interval="1m",
        csv_by_symbol={"BTCUSDT": btc_csv, "ETHUSDT": eth_csv},
        horizon_candles=3,
        threshold_bps=16,
        min_training_rows=40,
        output_path=tmp_path / "global.json",
    )

    model = load_ai_model(result["global_model"]["path"])
    registry = list_ai_models(scope=AI_SCOPE_GLOBAL, registry_path=registry_path)

    assert result["status"] == "OK"
    assert model["model_type"] == PROBABILISTIC_MODEL_TYPE
    assert model["signal_mode"] == "PROBABILISTIC_EVENT_V1"
    assert model["scope"] == "GLOBAL"
    assert model["dataset_hash"]
    assert model["algorithm_version"]
    assert model["training_type"] == "GLOBAL"
    assert model["training_window"]["start"]
    assert model["validation_window"]["start"]
    assert model["test_window"]["start"]
    assert model["metrics"]["metrics_source"] == "PURGED_OUT_OF_SAMPLE_TEST"
    assert model["training_rows"] > model["test_rows"]
    assert result["global_model"]["training_rows"] >= 40
    assert result["global_model"]["dataset_hash"] == model["dataset_hash"]
    assert registry[0]["event_definition"] == "future_return_threshold"
    assert registry[0]["dataset_hash"] == model["dataset_hash"]
    assert any("treinando modelo global" in event for event in progress_events)
    assert any("atualizando registry" in event for event in progress_events)


def test_probabilistic_refinement_trains_symbol_models_with_parent(tmp_path):
    btc_csv = tmp_path / "btc.csv"
    eth_csv = tmp_path / "eth.csv"
    _write_candles_csv(btc_csv)
    _write_candles_csv(eth_csv, slope=-0.02)
    service = ProbabilisticAITrainingService(
        registry_path=tmp_path / "registry.json",
        event_journal_path=None,
    )
    global_result = service.train_global(
        symbols=["BTCUSDT", "ETHUSDT"],
        interval="1m",
        csv_by_symbol={"BTCUSDT": btc_csv, "ETHUSDT": eth_csv},
        horizon_candles=3,
        threshold_bps=16,
        min_training_rows=40,
        output_path=tmp_path / "global.json",
    )

    refined = service.train_refined(
        symbols=["BTCUSDT", "ETHUSDT"],
        interval="1m",
        global_model_path=global_result["global_model"]["path"],
        csv_by_symbol={"BTCUSDT": btc_csv, "ETHUSDT": eth_csv},
        horizon_candles=3,
        threshold_bps=16,
        min_training_rows=20,
        output_dir=tmp_path / "refined",
    )

    assert refined["symbol_models"]["BTCUSDT"]["source"] == "SYMBOL_REFINED"
    assert refined["symbol_models"]["ETHUSDT"]["source"] == "SYMBOL_REFINED"
    btc_model = load_ai_model(refined["symbol_models"]["BTCUSDT"]["path"])
    eth_model = load_ai_model(refined["symbol_models"]["ETHUSDT"]["path"])
    assert btc_model["scope"] == PROBABILISTIC_SCOPE_SYMBOL_REFINED
    assert eth_model["scope"] == PROBABILISTIC_SCOPE_SYMBOL_REFINED
    assert btc_model["parent_global_model_id"] == global_result["global_model"]["model_id"]
    assert btc_model["parent_global_dataset_hash"]
    assert btc_model["dataset_hash"]
    assert btc_model["dataset_overlap_with_parent"]["status"] in {
        "SAME_DATASET",
        "FULL_PERIOD_OVERLAP",
        "PARTIAL_PERIOD_OVERLAP",
    }


def test_probabilistic_training_flags_duplicate_dataset(tmp_path):
    btc_csv = tmp_path / "btc.csv"
    _write_candles_csv(btc_csv)
    service = ProbabilisticAITrainingService(
        registry_path=tmp_path / "registry.json",
        event_journal_path=None,
    )

    first = service.train_global(
        symbols=["BTCUSDT"],
        interval="1m",
        csv_by_symbol={"BTCUSDT": btc_csv},
        horizon_candles=3,
        threshold_bps=16,
        min_training_rows=40,
        output_path=tmp_path / "global-1.json",
    )
    second = service.train_global(
        symbols=["BTCUSDT"],
        interval="1m",
        csv_by_symbol={"BTCUSDT": btc_csv},
        horizon_candles=3,
        threshold_bps=16,
        min_training_rows=40,
        output_path=tmp_path / "global-2.json",
    )

    assert first["global_model"]["dataset_hash"] == second["global_model"]["dataset_hash"]
    assert second["warnings"]
    model = load_ai_model(second["global_model"]["path"])
    assert "Treino duplicado detectado" in model["training_warnings"][0]


def test_probabilistic_resolver_uses_refined_then_global_fallback(tmp_path):
    global_model, global_path = _train_prob_model(tmp_path)
    refined = {
        **global_model,
        "scope": PROBABILISTIC_SCOPE_SYMBOL_REFINED,
        "symbol": "BTCUSDT",
        "model_id": "BTCUSDT_1M_prob_refined_ai",
    }
    refined_path = save_ai_model(refined, tmp_path / "btc_refined.json")
    resolver = ProbabilisticAIModelResolver(
        global_model_path=str(global_path),
        symbol_model_paths={"BTCUSDT": str(refined_path)},
    )

    btc = resolver.resolve("BTCUSDT")
    eth = resolver.resolve("ETHUSDT")

    assert btc.model_source == "SYMBOL_REFINED"
    assert btc.fallback_used is False
    assert eth.model_source == "GLOBAL"
    assert eth.fallback_used is True
    assert eth.fallback_reason == "SYMBOL_REFINED_MODEL_NOT_CONFIGURED"


def test_probabilistic_resolver_rejects_model_without_input_contract(tmp_path):
    model, _ = _train_prob_model(tmp_path)
    model.pop("input_contract")
    model_path = save_ai_model(model, tmp_path / "legacy_without_contract.json")
    resolver = ProbabilisticAIModelResolver(global_model_path=str(model_path))

    with pytest.raises(
        ProbabilisticAIModelInputContractMissing, match="AI_MODEL_INPUT_CONTRACT_MISSING"
    ):
        resolver.resolve("BTCUSDT")


def test_probabilistic_resolver_rejects_refined_global_timeframe_mismatch(tmp_path):
    global_model, global_path = _train_prob_model(tmp_path)
    refined = {
        **global_model,
        "scope": PROBABILISTIC_SCOPE_SYMBOL_REFINED,
        "symbol": "BTCUSDT",
        "model_id": "BTCUSDT_5M_prob_refined_ai",
        "interval": "5m",
        "input_contract": {
            **global_model["input_contract"],
            "timeframe": "5m",
        },
    }
    refined_path = save_ai_model(refined, tmp_path / "btc_refined_5m.json")
    resolver = ProbabilisticAIModelResolver(
        global_model_path=str(global_path),
        symbol_model_paths={"BTCUSDT": str(refined_path)},
    )

    with pytest.raises(
        ProbabilisticAIRefinedGlobalTimeframeMismatch,
        match="AI_REFINED_GLOBAL_TIMEFRAME_MISMATCH",
    ):
        resolver.resolve("BTCUSDT")


def test_probabilistic_prediction_reports_history_and_probabilities(tmp_path):
    model, _ = _train_prob_model(tmp_path)

    insufficient = predict_probabilistic_event(model, [100, 101])
    prediction = predict_probabilistic_event(model, candles_from_closes(_prices(40)))

    assert insufficient["reason"] == "AI_INSUFFICIENT_HISTORY"
    assert prediction["reason"] in {
        "AI_PROBABILITY_OPEN_LONG",
        "AI_PROBABILITY_OPEN_SHORT",
        "AI_PROBABILITY_NO_ACTION",
    }
    total = prediction["p_long_event"] + prediction["p_short_event"] + prediction["p_neutral_event"]
    assert 0 <= prediction["p_long_event"] <= 1
    assert 0 <= prediction["p_short_event"] <= 1
    assert abs(total - 1.0) < 0.0001


def test_probabilistic_strategy_returns_signal_score_without_execution(tmp_path):
    _model, model_path = _train_prob_model(tmp_path)
    strategy = ProbabilisticAIStrategy.from_params(
        {
            "global_model_path": str(model_path),
            "min_long_probability": 0.1,
            "min_short_probability": 0.1,
            "min_direction_gap": 0.0,
            "min_expected_value_bps": 0.0,
        }
    )

    score = strategy.score_symbol("BTCUSDT", _market(), _position(), {})

    assert isinstance(score, SignalScore)
    assert score.metadata["source"] == "AI_PROBABILISTIC"
    assert score.metadata["model_source"] in {"GLOBAL", "SYMBOL_REFINED"}
    assert 0 <= score.metadata["p_long_event"] <= 1
    assert not hasattr(strategy, "client")


def test_probabilistic_strategy_can_close_position(tmp_path):
    _model, model_path = _train_prob_model(tmp_path)
    strategy = ProbabilisticAIStrategy.from_params(
        {
            "global_model_path": str(model_path),
            "close_long_min_short_probability": 0.0,
            "min_hold_candles": 0,
        }
    )

    decision = strategy.decide_symbol(
        "BTCUSDT",
        _market(),
        _position(PositionDirection.LONG),
        {},
    )

    assert decision.action is TradeAction.CLOSE_POSITION


def test_governor_probability_gate_blocks_and_passes_new_entries(config_factory):
    config = replace(
        config_factory(),
        ai_probabilistic_enabled=True,
        ai_min_long_probability=0.62,
        ai_min_short_probability=0.62,
        ai_min_direction_gap=0.18,
        ai_min_expected_value_bps=3.0,
    )
    gate_service = AIGateService(
        config=config,
        journal=FakeJournal(),
        strategy=type("Coordinator", (), {"global_strategy_name": "ai"})(),
    )
    symbol_config = symbol_config_from_app_config(config)

    below = SignalScore(
        symbol="BTCUSDT",
        score=95,
        action="OPEN_LONG",
        reason="ai",
        metadata=_ai_metadata(p_long_event=0.50),
    )
    passed = SignalScore(
        symbol="BTCUSDT",
        score=95,
        action="OPEN_LONG",
        reason="ai",
        metadata=_ai_metadata(),
    )
    no_metadata = SignalScore(
        symbol="BTCUSDT",
        score=95,
        action="OPEN_LONG",
        reason="fixed",
    )

    assert (
        gate_service.evaluate(
            symbol_config=symbol_config,
            score=below,
            action="OPEN_LONG",
            position=_position(),
            stage="TEST",
        )["reason"]
        == "AI_PROBABILITY_BELOW_THRESHOLD"
    )
    assert (
        gate_service.evaluate(
            symbol_config=symbol_config,
            score=passed,
            action="OPEN_LONG",
            position=_position(),
            stage="TEST",
        )["allowed"]
        is True
    )
    assert (
        gate_service.evaluate(
            symbol_config=symbol_config,
            score=no_metadata,
            action="OPEN_LONG",
            position=_position(),
            stage="TEST",
        )["reason"]
        == "AI_PROBABILITY_METADATA_MISSING"
    )


def test_governor_probability_gate_does_not_block_closes(config_factory):
    config = replace(config_factory(), ai_probabilistic_enabled=True)
    gate_service = AIGateService(config=config)
    symbol_config = symbol_config_from_app_config(config)

    gate = gate_service.evaluate(
        symbol_config=symbol_config,
        score=SignalScore(symbol="BTCUSDT", score=95, action="CLOSE_POSITION", reason="x"),
        action="CLOSE_POSITION",
        position=_position(PositionDirection.LONG),
        stage="TEST",
    )

    assert gate is None


def test_probabilistic_mode_forces_strategy_and_rejects_fixed_strategy_env(
    monkeypatch,
    tmp_path,
):
    from config import load_config

    model, model_path = _train_prob_model(tmp_path)

    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_AI_PROBABILISTIC_ENABLED", "true")
    monkeypatch.setenv("ALPHORA_AI_GLOBAL_MODEL_PATH", str(model_path))
    monkeypatch.setenv("ALPHORA_STRATEGY_MODULE", "strategies.futures.fixed.example")
    monkeypatch.setenv("ALPHORA_STRATEGY_CLASS", "ExampleStrategy")

    config = load_config()

    assert model["model_type"] == PROBABILISTIC_MODEL_TYPE
    assert config.strategy_runtime_mode == "PROBABILISTIC_AI"
    assert config.strategy_module == "strategies.futures.ai.probabilistic_strategy"
    assert config.strategy_class == "ProbabilisticAIStrategy"
    assert config.ai_probabilistic_enabled is True


def test_default_probabilistic_paths_are_json():
    path = default_probabilistic_global_model_path(interval="1m")
    refined_path = default_probabilistic_refined_model_path(
        symbol="BTCUSDT",
        interval="1m",
    )

    assert str(path).endswith("GLOBAL_1M_probabilistic_ai.json")
    assert str(refined_path).endswith("BTCUSDT_1M_probabilistic_refined_ai.json")


def test_probabilistic_resolver_refuses_an_outdated_feature_schema(tmp_path):
    model, path = _train_prob_model(tmp_path)
    model["input_contract"] = {
        **model["input_contract"],
        "feature_schema_version": "probabilistic_features_v2_ohlcv",
        "series_type": "CANDLE_OHLCV_WITH_CLOSE_FALLBACK",
    }
    save_ai_model(model, path)
    strategy = ProbabilisticAIStrategy.from_params({"global_model_path": str(path)})

    with pytest.raises(ProbabilisticAIModelSchemaOutdated, match="AI_MODEL_SCHEMA_OUTDATED"):
        strategy.model_resolver.resolve("BTCUSDT")
    score = strategy.score_symbol("BTCUSDT", _market(), _position(), {})

    assert score.reason == "AI_MODEL_SCHEMA_OUTDATED"
    assert score.action == "NO_ACTION"
