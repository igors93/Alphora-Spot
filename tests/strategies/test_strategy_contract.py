"""Phase 4 — Strategy contract tests.

Covers:
- StrategyDataRequirements.from_ai_model (with and without input_contract)
- ai_input_contract_from_model derivation
- FuturesStrategyV2.requirements_for_symbol default delegation
- ProbabilisticAIStrategy.requirements_for_symbol dynamic model resolution
- Backward compat: old strategies still work via data_requirements
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from domain.strategy_requirements import (
    EVALUATION_EVERY_CYCLE,
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    StrategyDataRequirements,
    ai_input_contract_from_model,
)
from research.ai.model import save_ai_model
from research.ai.probabilistic import (
    PROBABILISTIC_MODEL_TYPE,
    PROBABILISTIC_SCOPE_GLOBAL,
    train_probabilistic_linear_ai_model_from_price_series,
)
from strategies.futures.ai.probabilistic_strategy import ProbabilisticAIStrategy
from strategies.futures.base import FuturesStrategyV2, StrategyMetadata
from strategies.futures.fixed.random_timed_signal import RandomTimedSignalStrategy
from tests.fakes import candles_from_closes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ai_model(*, interval: str = "1m", required_history: int = 20, **overrides) -> dict[str, Any]:
    """Minimal AI model dict with input_contract."""
    model: dict[str, Any] = {
        "model_type": PROBABILISTIC_MODEL_TYPE,
        "scope": PROBABILISTIC_SCOPE_GLOBAL,
        "interval": interval,
        "required_history": required_history,
        "input_contract": {
            "timeframe": interval,
            "series_type": "CANDLE_CLOSE",
            "required_history": required_history,
            "closed_candles_only": True,
            "feature_schema_version": "probabilistic_features_v1",
        },
    }
    model.update(overrides)
    return model


def _legacy_ai_model(
    *, interval: str = "1m", required_history: int = 20, **overrides
) -> dict[str, Any]:
    """Minimal legacy AI model dict without input_contract."""
    model: dict[str, Any] = {
        "model_type": PROBABILISTIC_MODEL_TYPE,
        "scope": PROBABILISTIC_SCOPE_GLOBAL,
        "interval": interval,
        "required_history": required_history,
    }
    model.update(overrides)
    return model


def _prices(rows: int = 120, *, slope: float = 0.03) -> list[float]:
    return [100 + i * slope + (0.12 if i % 9 == 0 else 0.0) for i in range(rows)]


def _write_candles_csv(path, rows=120, *, slope=0.03) -> None:
    lines = ["timestamp,open,high,low,close,volume"]
    base = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    for index, close in enumerate(_prices(rows, slope=slope)):
        ts = (base + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{ts},{close},{close + 1},{close - 1},{close},10")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _train_and_save_model(tmp_path, *, interval: str = "1m") -> str:
    model = train_probabilistic_linear_ai_model_from_price_series(
        [candles_from_closes(_prices(), interval=interval)],
        symbol="GLOBAL",
        interval=interval,
        scope=PROBABILISTIC_SCOPE_GLOBAL,
        horizon_candles=3,
        threshold_bps=1.0,
        estimated_round_trip_cost_bps=0.5,
        min_training_rows=20,
        trained_symbols=["BTCUSDT"],
    )
    path = tmp_path / f"global_{interval}.json"
    save_ai_model(model, path)
    return str(path)


# ---------------------------------------------------------------------------
# StrategyDataRequirements.from_ai_model — with input_contract
# ---------------------------------------------------------------------------


class TestFromAiModelWithInputContract:
    def test_reads_timeframe_from_contract(self):
        req = StrategyDataRequirements.from_ai_model(_ai_model(interval="5m"))
        assert req.primary_timeframe == "5m"

    def test_reads_required_history_from_contract(self):
        req = StrategyDataRequirements.from_ai_model(_ai_model(interval="1m", required_history=50))
        assert req.required_candles["1m"] == 50

    def test_sets_on_primary_candle_close_policy(self):
        req = StrategyDataRequirements.from_ai_model(_ai_model())
        assert req.evaluation_policy == EVALUATION_ON_PRIMARY_CANDLE_CLOSE

    def test_sets_closed_candles_only_true(self):
        req = StrategyDataRequirements.from_ai_model(_ai_model())
        assert req.closed_candles_only is True

    def test_closed_candles_only_false_from_contract(self):
        model = _ai_model()
        model["input_contract"]["closed_candles_only"] = False
        req = StrategyDataRequirements.from_ai_model(model)
        assert req.closed_candles_only is False

    def test_primary_timeframe_is_only_required_candle_key(self):
        req = StrategyDataRequirements.from_ai_model(_ai_model(interval="15m"))
        assert set(req.required_candles.keys()) == {"15m"}

    def test_various_timeframes(self):
        for tf in ("1m", "5m", "15m", "1h", "4h", "1d"):
            req = StrategyDataRequirements.from_ai_model(_ai_model(interval=tf))
            assert req.primary_timeframe == tf


# ---------------------------------------------------------------------------
# StrategyDataRequirements.from_ai_model — legacy (no input_contract)
# ---------------------------------------------------------------------------


class TestFromAiModelLegacy:
    def test_derives_timeframe_from_interval_field(self):
        req = StrategyDataRequirements.from_ai_model(_legacy_ai_model(interval="5m"))
        assert req.primary_timeframe == "5m"

    def test_derives_required_history(self):
        req = StrategyDataRequirements.from_ai_model(
            _legacy_ai_model(interval="1m", required_history=30)
        )
        assert req.required_candles["1m"] == 30

    def test_defaults_to_1m_when_interval_empty(self):
        req = StrategyDataRequirements.from_ai_model({"model_type": PROBABILISTIC_MODEL_TYPE})
        assert req.primary_timeframe == "1m"

    def test_defaults_to_20_history_when_missing(self):
        req = StrategyDataRequirements.from_ai_model({"interval": "1m", "model_type": "x"})
        assert req.required_candles["1m"] == 20

    def test_history_at_least_1(self):
        req = StrategyDataRequirements.from_ai_model(
            _legacy_ai_model(interval="1m", required_history=0)
        )
        assert req.required_candles["1m"] >= 1

    def test_policy_is_on_primary_candle_close(self):
        req = StrategyDataRequirements.from_ai_model(_legacy_ai_model())
        assert req.evaluation_policy == EVALUATION_ON_PRIMARY_CANDLE_CLOSE

    def test_closed_candles_only_defaults_true(self):
        req = StrategyDataRequirements.from_ai_model(_legacy_ai_model())
        assert req.closed_candles_only is True


# ---------------------------------------------------------------------------
# ai_input_contract_from_model
# ---------------------------------------------------------------------------


class TestAiInputContractFromModel:
    def test_returns_existing_contract_unchanged(self):
        model = _ai_model(interval="5m", required_history=40)
        contract = ai_input_contract_from_model(model)
        assert contract["timeframe"] == "5m"
        assert contract["required_history"] == 40
        assert contract["series_type"] == "CANDLE_CLOSE"
        assert contract["feature_schema_version"] == "probabilistic_features_v1"

    def test_derives_contract_for_legacy_model(self):
        model = _legacy_ai_model(interval="15m", required_history=60)
        contract = ai_input_contract_from_model(model)
        assert contract["timeframe"] == "15m"
        assert contract["required_history"] == 60
        assert contract["series_type"] == "CANDLE_CLOSE"
        assert contract["closed_candles_only"] is True

    def test_legacy_schema_version_includes_model_type(self):
        model = _legacy_ai_model(interval="1m")
        contract = ai_input_contract_from_model(model)
        assert PROBABILISTIC_MODEL_TYPE.lower() in contract["feature_schema_version"]

    def test_legacy_schema_version_unknown_when_no_type(self):
        contract = ai_input_contract_from_model({"interval": "1m"})
        assert "unknown" in contract["feature_schema_version"]

    def test_does_not_mutate_original_model(self):
        model = _ai_model(interval="1m")
        original_contract = dict(model["input_contract"])
        ai_input_contract_from_model(model)
        assert model["input_contract"] == original_contract

    def test_returns_copy_not_reference(self):
        model = _ai_model(interval="1m")
        contract = ai_input_contract_from_model(model)
        contract["timeframe"] = "MUTATED"
        assert model["input_contract"]["timeframe"] == "1m"


# ---------------------------------------------------------------------------
# FuturesStrategyV2.requirements_for_symbol default
# ---------------------------------------------------------------------------


class TestBaseStrategyRequirementsForSymbol:
    def test_default_delegates_to_data_requirements(self):
        strategy = RandomTimedSignalStrategy()
        req_default = strategy.data_requirements()
        req_symbol = strategy.requirements_for_symbol("BTCUSDT")
        assert req_symbol.primary_timeframe == req_default.primary_timeframe
        assert req_symbol.required_candles == req_default.required_candles
        assert req_symbol.evaluation_policy == req_default.evaluation_policy

    def test_symbol_config_ignored_in_default(self):
        strategy = RandomTimedSignalStrategy()
        req_none = strategy.requirements_for_symbol("BTCUSDT", symbol_config=None)
        req_with = strategy.requirements_for_symbol("BTCUSDT", symbol_config={"dummy": True})
        assert req_none == req_with

    def test_different_symbols_return_same_requirements(self):
        strategy = RandomTimedSignalStrategy()
        req_btc = strategy.requirements_for_symbol("BTCUSDT")
        req_eth = strategy.requirements_for_symbol("ETHUSDT")
        assert req_btc == req_eth

    def test_returns_strategy_data_requirements_instance(self):
        strategy = RandomTimedSignalStrategy()
        req = strategy.requirements_for_symbol("BTCUSDT")
        assert isinstance(req, StrategyDataRequirements)


# ---------------------------------------------------------------------------
# Backward compat: old strategies via data_requirements
# ---------------------------------------------------------------------------


class TestLegacyStrategyBackwardCompat:
    def test_random_timed_signal_data_requirements(self):
        strategy = RandomTimedSignalStrategy()
        req = strategy.data_requirements()
        assert req.primary_timeframe == "1m"
        assert req.required_candles["1m"] >= 1
        assert req.evaluation_policy == EVALUATION_EVERY_CYCLE

    def test_custom_legacy_metadata_converts(self):
        class LegacyStrategy(FuturesStrategyV2):
            metadata = StrategyMetadata(
                strategy_name="LegacyTest",
                required_history=50,
                required_timeframes=["5m"],
            )

            def score_symbol(self, *a, **kw):
                raise NotImplementedError

            def decide_symbol(self, *a, **kw):
                raise NotImplementedError

        strategy = LegacyStrategy()
        req = strategy.data_requirements()
        assert req.primary_timeframe == "5m"
        assert req.required_candles["5m"] == 50
        assert req.evaluation_policy == EVALUATION_EVERY_CYCLE

    def test_requirements_for_symbol_falls_back_to_data_requirements(self):
        strategy = RandomTimedSignalStrategy()
        req_dr = strategy.data_requirements()
        req_rs = strategy.requirements_for_symbol("BTCUSDT")
        assert req_dr.primary_timeframe == req_rs.primary_timeframe
        assert req_dr.evaluation_policy == req_rs.evaluation_policy


# ---------------------------------------------------------------------------
# ProbabilisticAIStrategy.requirements_for_symbol — dynamic from model
# ---------------------------------------------------------------------------


class TestProbabilisticStrategyRequirementsForSymbol:
    def test_returns_requirements_from_actual_model_interval(self, tmp_path):
        model_path = _train_and_save_model(tmp_path, interval="5m")
        strategy = ProbabilisticAIStrategy.from_params({"global_model_path": model_path})

        req = strategy.requirements_for_symbol("BTCUSDT")

        assert req.primary_timeframe == "5m"
        assert req.evaluation_policy == EVALUATION_ON_PRIMARY_CANDLE_CLOSE
        assert req.closed_candles_only is True

    def test_returns_requirements_from_1m_model(self, tmp_path):
        model_path = _train_and_save_model(tmp_path, interval="1m")
        strategy = ProbabilisticAIStrategy.from_params({"global_model_path": model_path})

        req = strategy.requirements_for_symbol("BTCUSDT")

        assert req.primary_timeframe == "1m"

    def test_required_candles_matches_model_required_history(self, tmp_path):
        model_path = _train_and_save_model(tmp_path, interval="1m")
        strategy = ProbabilisticAIStrategy.from_params({"global_model_path": model_path})

        req = strategy.requirements_for_symbol("BTCUSDT")

        assert req.primary_timeframe in req.required_candles
        assert req.required_candles[req.primary_timeframe] >= 1

    def test_falls_back_to_static_when_model_unavailable(self):
        strategy = ProbabilisticAIStrategy.from_params(
            {"global_model_path": "/nonexistent/model.json"}
        )

        req = strategy.requirements_for_symbol("BTCUSDT")

        assert isinstance(req, StrategyDataRequirements)
        assert req.evaluation_policy == EVALUATION_ON_PRIMARY_CANDLE_CLOSE

    def test_different_symbols_resolve_same_global_model(self, tmp_path):
        model_path = _train_and_save_model(tmp_path, interval="15m")
        strategy = ProbabilisticAIStrategy.from_params({"global_model_path": model_path})

        req_btc = strategy.requirements_for_symbol("BTCUSDT")
        req_eth = strategy.requirements_for_symbol("ETHUSDT")

        assert req_btc.primary_timeframe == req_eth.primary_timeframe == "15m"

    def test_data_requirements_is_static_fallback(self, tmp_path):
        strategy = ProbabilisticAIStrategy.from_params({"global_model_path": ""})
        req = strategy.data_requirements()
        assert req.primary_timeframe == "1m"
        assert req.evaluation_policy == EVALUATION_ON_PRIMARY_CANDLE_CLOSE
