from decimal import Decimal

from futures.performance.metrics import PerformanceMetrics
from futures.performance.readiness import (
    ReadinessConfig,
    ReadinessStatus,
    StrategyReadinessEvaluator,
)


def make_metrics(
    *,
    total_trades: int = 30,
    net_pnl: Decimal = Decimal("10"),
    expectancy: Decimal = Decimal("1"),
    profit_factor: Decimal = Decimal("2"),
    max_drawdown_pct: Decimal = Decimal("0.1"),
    max_consecutive_losses: int = 2,
) -> PerformanceMetrics:
    return PerformanceMetrics(
        total_trades=total_trades,
        winning_trades=20,
        losing_trades=10,
        win_rate=Decimal("0.66"),
        gross_pnl=net_pnl,
        net_pnl=net_pnl,
        total_fees=Decimal("1"),
        total_slippage=Decimal("1"),
        total_spread=Decimal("1"),
        total_funding=Decimal("0"),
        average_win=Decimal("2"),
        average_loss=Decimal("-1"),
        payoff_ratio=Decimal("2"),
        profit_factor=profit_factor,
        expectancy=expectancy,
        max_drawdown=Decimal("10"),
        max_drawdown_pct=max_drawdown_pct,
        max_consecutive_losses=max_consecutive_losses,
        average_trade_net_pnl=expectancy,
        best_trade=Decimal("5"),
        worst_trade=Decimal("-2"),
        sample_sufficient=total_trades >= 30,
    )


def test_readiness_needs_more_data_when_too_few_trades():
    evaluator = StrategyReadinessEvaluator(ReadinessConfig(min_trades=30))

    result = evaluator.evaluate(make_metrics(total_trades=5))

    assert result.status == ReadinessStatus.NEEDS_MORE_DATA
    assert result.passed is False


def test_readiness_not_ready_with_negative_expectancy():
    evaluator = StrategyReadinessEvaluator(ReadinessConfig(min_trades=1))

    result = evaluator.evaluate(make_metrics(expectancy=Decimal("-0.1")))

    assert result.status == ReadinessStatus.NOT_READY
    assert result.passed is False


def test_readiness_not_ready_with_low_profit_factor():
    evaluator = StrategyReadinessEvaluator(
        ReadinessConfig(min_trades=1, min_profit_factor=Decimal("1.2"))
    )

    result = evaluator.evaluate(make_metrics(profit_factor=Decimal("1.0")))

    assert result.status == ReadinessStatus.NOT_READY


def test_readiness_backtest_ok_when_criteria_pass():
    evaluator = StrategyReadinessEvaluator(ReadinessConfig(min_trades=1))

    result = evaluator.evaluate(make_metrics())

    assert result.status == ReadinessStatus.BACKTEST_OK
    assert result.passed is True
