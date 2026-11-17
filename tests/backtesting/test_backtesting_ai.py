from decimal import Decimal

from backtesting.domain.config import (
    BacktestConfig,
    BacktestStrategyConfig,
    BacktestSymbolConfig,
)
from backtesting.engine import run_backtest
from config.symbol_context import PortfolioRiskSettings
from domain.ai.model import AI_FEATURE_SCHEMA_VERSION, AI_INPUT_SERIES_TYPE
from domain.symbol_rules import SymbolRules
from research.ai.model import AI_FEATURES, PROBABILISTIC_MODEL_TYPE, save_ai_model
from research.ai.probabilistic import (
    PROBABILISTIC_EVENT_DEFINITION,
    PROBABILISTIC_SCOPE_GLOBAL,
    PROBABILISTIC_SCOPE_SYMBOL_REFINED,
    PROBABILISTIC_SIGNAL_MODE,
)


def test_backtesting_probabilistic_ai_generates_open_long_with_global_model(tmp_path):
    csv_path = _write_candles(tmp_path / "candles.csv")
    model_path = _write_model(tmp_path / "global.json", long_bias=2.0, short_bias=-2.0)

    result = run_backtest(
        _ai_config(tmp_path, csv_path, global_model_path=model_path),
        write_reports=False,
    )

    executed = [
        item
        for item in result.decisions
        if item.final_action == "OPEN_LONG" and item.final_status == "EXECUTED"
    ]
    assert executed
    assert executed[0].model_source == "GLOBAL"
    assert executed[0].long_score == 2.0


def test_backtesting_probabilistic_ai_uses_refined_symbol_model(tmp_path):
    csv_path = _write_candles(tmp_path / "candles.csv")
    global_path = _write_model(tmp_path / "global.json", long_bias=-2.0, short_bias=-2.0)
    refined_path = _write_model(
        tmp_path / "btc_refined.json",
        scope=PROBABILISTIC_SCOPE_SYMBOL_REFINED,
        symbol="BTCUSDT",
        long_bias=-2.0,
        short_bias=2.0,
    )

    result = run_backtest(
        _ai_config(
            tmp_path,
            csv_path,
            global_model_path=global_path,
            symbol_model_paths={"BTCUSDT": str(refined_path)},
        ),
        write_reports=False,
    )

    executed = [
        item
        for item in result.decisions
        if item.final_action == "OPEN_SHORT" and item.final_status == "EXECUTED"
    ]
    assert executed
    assert executed[0].model_source == "SYMBOL_REFINED"
    assert executed[0].short_score == 2.0
    assert result.summary["ai_refined_signals"] > 0


def _ai_config(
    tmp_path,
    csv_path,
    *,
    global_model_path,
    symbol_model_paths=None,
) -> BacktestConfig:
    return BacktestConfig(
        symbols=(
            BacktestSymbolConfig(
                symbol="BTCUSDT",
                csv_path=str(csv_path),
                rules=SymbolRules(
                    symbol="BTCUSDT",
                    quantity_step=Decimal("0.001"),
                    min_quantity=Decimal("0.001"),
                    min_notional=Decimal("5"),
                    price_tick=Decimal("0.10"),
                ),
                stop_loss_pct=Decimal("0.50"),
                take_profit_pct=Decimal("0.50"),
            ),
        ),
        strategy=BacktestStrategyConfig(
            mode="PROBABILISTIC_AI",
            global_model_path=str(global_model_path),
            symbol_model_paths=symbol_model_paths or {},
        ),
        portfolio_settings=PortfolioRiskSettings(max_open_positions=5),
        output_dir=str(tmp_path),
    )


def _write_model(
    path,
    *,
    long_bias,
    short_bias,
    scope=PROBABILISTIC_SCOPE_GLOBAL,
    symbol="GLOBAL",
):
    model = {
        "model_id": path.stem,
        "model_type": PROBABILISTIC_MODEL_TYPE,
        "model_family": "PROBABILISTIC_LINEAR_AI",
        "signal_mode": PROBABILISTIC_SIGNAL_MODE,
        "scope": scope,
        "symbol": symbol,
        "interval": "1m",
        "features": list(AI_FEATURES),
        "weights_long": {feature: 0.0 for feature in AI_FEATURES},
        "weights_short": {feature: 0.0 for feature in AI_FEATURES},
        "bias_long": long_bias,
        "bias_short": short_bias,
        "horizon_candles": 5,
        "threshold_bps": 10,
        "required_history": 20,
        "feature_stats": {feature: {"mean": 0.0, "stddev": 1.0} for feature in AI_FEATURES},
        "calibration": {
            "method": "test",
            "long_logit_bias": 0.0,
            "short_logit_bias": 0.0,
        },
        "trained_at": "2026-05-22T00:00:00Z",
        "training_rows": 100,
        "trained_symbols": [symbol],
        "metrics": {"accuracy": 1.0},
        "event_definition": PROBABILISTIC_EVENT_DEFINITION,
        "input_contract": {
            "timeframe": "1m",
            "series_type": AI_INPUT_SERIES_TYPE,
            "required_history": 20,
            "closed_candles_only": True,
            "feature_schema_version": AI_FEATURE_SCHEMA_VERSION,
        },
    }
    return save_ai_model(model, path)


def _write_candles(path):
    rows = ["timestamp,open,high,low,close,volume"]
    for index in range(25):
        rows.append(f"{index},100,100,100,100,1")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path
