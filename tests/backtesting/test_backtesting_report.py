import json
import sys
from decimal import Decimal
from pathlib import Path

from backtesting.adapters.cli import main as run_backtest_cli
from backtesting.adapters.desktop import build_backtest_request, write_backtest_request
from backtesting.domain.config import (
    BacktestConfig,
    BacktestStrategyConfig,
    BacktestSymbolConfig,
)
from backtesting.engine import run_backtest
from config.symbol_context import PortfolioRiskSettings
from domain.symbol_rules import SymbolRules
from tests.backtesting.test_backtesting_ai import _write_candles as write_ai_candles
from tests.backtesting.test_backtesting_ai import _write_model


def test_backtesting_report_writes_summary_trades_events_and_rejections(tmp_path):
    csv_path = write_ai_candles(tmp_path / "candles.csv")
    model_path = _write_model(tmp_path / "global.json", long_bias=1.0, short_bias=-1.0)
    config = _ai_config(
        tmp_path,
        csv_path,
        model_path,
        portfolio_settings=PortfolioRiskSettings(
            max_open_positions=5,
            max_total_exposure_usdt=10,
        ),
    )

    result = run_backtest(config, write_reports=True)

    assert result.paths is not None
    assert result.paths.summary_json.exists()
    assert result.paths.trades_csv.exists()
    assert result.paths.decisions_jsonl.exists()
    assert result.paths.rejected_jsonl.exists()
    assert result.paths.equity_csv is not None
    assert result.paths.equity_csv.exists()
    assert result.paths.manifest_json is not None
    assert result.paths.manifest_json.exists()

    payload = json.loads(result.paths.summary_json.read_text(encoding="utf-8"))

    assert payload["summary"]["rejected_signals_count"] > 0
    assert payload["summary"]["blocked_by_risk"] > 0
    assert payload["summary"]["ai_global_signals"] > 0
    assert payload["readiness"]["passed"] == payload["summary"]["readiness_passed"]
    assert "operational_verdict" in payload["summary"]
    assert "by_exit_reason" in payload["summary"]
    assert "sizing_diagnostics" in payload["summary"]
    assert "dynamic_exit_diagnostics" in payload["summary"]
    assert "smart_sizing_diagnostics" in payload["summary"]
    assert "smart_execution_diagnostics" in payload["summary"]
    assert "state_reconciliation_diagnostics" in payload["summary"]
    assert "operational_module_modes" in payload["summary"]
    assert "operational_module_modes" in payload["config"]
    json.dumps(payload["summary"]["operational_module_modes"])
    json.dumps(payload["summary"]["smart_sizing_diagnostics"])
    json.dumps(payload["summary"]["smart_execution_diagnostics"])
    json.dumps(payload["summary"]["state_reconciliation_diagnostics"])
    trades_header = result.paths.trades_csv.read_text(encoding="utf-8").splitlines()[0]
    assert "dynamic_exit_used" in trades_header
    assert "actual_stop_price_used" in trades_header
    assert "smart_sizing_mode" in trades_header
    assert "entry_order_type" in trades_header
    assert "limit_fill_status" in trades_header
    assert "fallback_to_market_used" in trades_header
    assert result.paths.decisions_jsonl.read_text(encoding="utf-8").strip()
    assert result.paths.rejected_jsonl.read_text(encoding="utf-8").strip()
    manifest = json.loads(result.paths.manifest_json.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["git_commit"]
    assert manifest["source_tree_sha256"]
    assert manifest["config"]
    assert manifest["input_files"]["BTCUSDT:1m"]["sha256"]
    assert manifest["model_files"]["GLOBAL"]["sha256"]
    assert manifest["output_files"]["equity_csv"]["sha256"]
    assert "coalestra" not in manifest["dependencies"]
    assert "relinker" not in manifest["dependencies"]


def test_backtesting_cli_runs_integrated_engine_and_prints_manifest(
    tmp_path,
    capsys,
    monkeypatch,
):
    csv_path = write_ai_candles(tmp_path / "candles.csv")
    model_path = _write_model(tmp_path / "global.json", long_bias=1.0, short_bias=-1.0)

    request = build_backtest_request(
        csv_path=str(csv_path),
        csv_map=None,
        symbol="BTCUSDT",
        symbols=None,
        strategy_mode="PROBABILISTIC_AI",
        strategy_module="strategies.futures.ai.probabilistic_strategy",
        strategy_class="ProbabilisticAIStrategy",
        timeframe="1m",
        strategy_params=None,
        global_model_path=str(model_path),
        symbol_model_map=None,
        use_symbol_refined_models=True,
        fallback_to_global_model=True,
        min_long_probability=None,
        min_short_probability=None,
        min_direction_gap=None,
        min_expected_value_bps=None,
        initial_balance="1000",
        maker_fee_pct="0",
        taker_fee_pct="0",
        slippage_bps="0",
        spread_bps="0",
        funding_enabled=False,
        default_funding_rate="0",
        position_notional="50",
        stop_loss_pct="0.50",
        take_profit_pct="0.50",
        leverage="1",
        quantity_step="0.001",
        min_quantity="0.001",
        min_notional="5",
        price_tick="0.10",
        max_open_positions="5",
        max_total_exposure="0",
        max_long_exposure="0",
        max_short_exposure="0",
        max_symbol_exposure="0",
        max_group_exposure="0",
        max_correlated_positions="0",
        max_new_positions_per_cycle="1",
        output_dir=str(tmp_path),
    )
    request_path = write_backtest_request(tmp_path / "request.json", request)
    monkeypatch.setattr(
        sys,
        "argv",
        ["python", "run", "--request", str(request_path)],
    )
    run_backtest_cli()

    output = capsys.readouterr().out

    manifest_event = json.loads(output.strip().splitlines()[-1])
    assert manifest_event["type"] == "backtest_manifest"
    assert manifest_event["version"] == 1
    manifest_path = Path(manifest_event["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == request.run_id
    assert manifest["request_hash"]
    assert manifest["config_hash"]
    assert manifest["request_path"] == str(request_path.resolve())

    monkeypatch.setattr(
        sys,
        "argv",
        ["python", "robust", "--request", str(request_path)],
    )
    run_backtest_cli()
    robust_output = capsys.readouterr().out
    robust_event = json.loads(robust_output.strip().splitlines()[-1])
    assert robust_event["type"] == "robust_backtest_report"
    assert robust_event["version"] == 1
    robust_path = Path(robust_event["path"])
    robust_payload = json.loads(robust_path.read_text(encoding="utf-8"))
    assert robust_payload["run_id"] == request.run_id
    assert robust_payload["request_hash"]
    assert robust_payload["reproducibility"]["source_tree_sha256"]


def _ai_config(
    tmp_path: Path,
    csv_path: Path,
    model_path: Path,
    *,
    portfolio_settings: PortfolioRiskSettings,
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
            global_model_path=str(model_path),
        ),
        portfolio_settings=portfolio_settings,
        output_dir=str(tmp_path),
    )


def test_backtesting_cli_module_is_importable():
    assert sys.executable
