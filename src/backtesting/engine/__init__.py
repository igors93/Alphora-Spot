"""Public backtest entrypoints, imported lazily to avoid adapter import cycles."""

from typing import Any

from backtesting.domain.config import BacktestConfig, BacktestRunResult


def run_backtest(config: BacktestConfig, *, write_reports: bool = True) -> BacktestRunResult:
    from backtesting.engine.engine import run_backtest as execute

    return execute(config, write_reports=write_reports)


def run_robust_backtest(config: BacktestConfig, *, write_reports: bool = False) -> dict[str, Any]:
    from backtesting.engine.robustness import run_robust_backtest as execute

    return execute(config, write_reports=write_reports)


__all__ = ["run_backtest", "run_robust_backtest"]
