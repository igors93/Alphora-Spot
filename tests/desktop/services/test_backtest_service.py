from pathlib import Path

from desktop.services.backtest_service import (
    normalize_backtest_settings,
    parse_downloaded_path,
    prepare_download_plan,
)


def test_download_plan_uses_research_cli(tmp_path: Path) -> None:
    settings = normalize_backtest_settings(
        {
            "symbol": "btcusdt",
            "backtest_interval": "1m",
            "backtest_start": "2024-01-01",
            "backtest_end": "2024-01-02",
            "backtest_csv_path": "data/btc.csv",
        },
        {},
    )

    plan = prepare_download_plan(settings, project_root=tmp_path)

    assert plan.command[1:4] == ("-m", "research.cli", "download-klines")
    assert "BTCUSDT" in plan.command


def test_parse_downloaded_path_resolves_relative_path(tmp_path: Path) -> None:
    path = parse_downloaded_path(
        ["Candles baixados: data/market/btc.csv"],
        project_root=tmp_path,
    )

    assert path == tmp_path / "data" / "market" / "btc.csv"
