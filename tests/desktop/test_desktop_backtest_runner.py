import json
from pathlib import Path

from backtesting.adapters.desktop import (
    build_backtest_command,
    build_download_klines_command,
    extract_backtest_rules_from_exchange_info,
    load_backtest_summary,
    parse_backtest_artifact_paths,
)


def test_build_backtest_command_contains_expected_arguments():
    command = build_backtest_command(
        request_path="runtime/backtests/run/request.json",
        python_executable="python",
    )

    assert command[:4] == ["python", "-m", "backtesting.adapters.cli", "run"]
    assert "--request" in command
    assert "runtime/backtests/run/request.json" in command


def test_build_download_klines_command_contains_expected_arguments():
    command = build_download_klines_command(
        symbol="BTCUSDT",
        interval="1m",
        start="2026-01-01",
        end="2026-01-31",
        output="data/market/out.csv",
        python_executable="python",
    )

    assert command[:4] == ["python", "-m", "research.cli", "download-klines"]
    assert "--symbol" in command
    assert "BTCUSDT" in command
    assert "--output" in command
    assert "data/market/out.csv" in command


def test_parse_report_paths_from_manifest_output(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        """
{
  "schema_version": 1,
  "run_id": "run",
  "status": "SUCCEEDED",
  "started_at": "2026-01-01T00:00:00Z",
  "finished_at": "2026-01-01T00:00:01Z",
  "request_path": "request.json",
  "summary_json": "reports/backtest.json",
  "trades_csv": "reports/backtest_trades.csv",
  "decisions_jsonl": "reports/backtest_decisions.jsonl",
  "rejected_jsonl": "reports/backtest_rejected.jsonl",
  "diagnostics_json": null,
  "error": null
}
""",
        encoding="utf-8",
    )
    paths = parse_backtest_artifact_paths(
        [json.dumps({"type": "backtest_manifest", "version": 1, "path": str(manifest)})]
    )

    assert paths.summary_json == Path("reports/backtest.json")
    assert paths.trades_csv == Path("reports/backtest_trades.csv")


def test_extract_backtest_rules_from_exchange_info():
    rules = extract_backtest_rules_from_exchange_info(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.0001",
                            "minQty": "0.0001",
                        },
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "50"},
                    ],
                }
            ]
        },
        "BTCUSDT",
    )

    assert rules == {
        "quantity_step": "0.0001",
        "min_quantity": "0.0001",
        "min_notional": "50",
        "price_tick": "0.1",
    }


def test_load_backtest_summary_includes_readiness_reasons_and_warnings(tmp_path: Path):
    path = tmp_path / "report.json"
    path.write_text(
        """
{
  "metrics": {
    "total_trades": 1,
    "win_rate": "1",
    "best_trade": "2",
    "worst_trade": "-1"
  },
  "readiness": {
    "status": "BLOCKED",
    "reasons": ["amostra insuficiente"],
    "warnings": ["validar em paper"]
  },
  "rejected_signal_count": 3
}
""",
        encoding="utf-8",
    )

    summary = load_backtest_summary(path)

    assert summary["readiness_reasons"] == ["amostra insuficiente"]
    assert summary["readiness_warnings"] == ["validar em paper"]
    assert summary["win_rate"] == "1"
    assert summary["best_trade"] == "2"
    assert summary["worst_trade"] == "-1"
