from decimal import Decimal

from domain.models import PositionDirection
from domain.trade_result import TradeResult
from futures.performance.metrics import calculate_performance_metrics


def make_trade(net_pnl: str, gross_pnl: str | None = None) -> TradeResult:
    net = Decimal(net_pnl)
    gross = Decimal(gross_pnl) if gross_pnl is not None else net
    return TradeResult(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        entry_time="1",
        exit_time="2",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        quantity=Decimal("1"),
        notional=Decimal("100"),
        gross_pnl=gross,
        fees=Decimal("0.1"),
        slippage_cost=Decimal("0.2"),
        spread_cost=Decimal("0.3"),
        funding_cost=Decimal("0.4"),
        net_pnl=net,
        return_pct=net / Decimal("100"),
        exit_reason="test",
        strategy_reason_entry="entry",
        strategy_reason_exit="exit",
    )


def test_metrics_empty_list_is_safe():
    metrics = calculate_performance_metrics([])

    assert metrics.total_trades == 0
    assert metrics.net_pnl == 0
    assert metrics.sample_sufficient is False


def test_metrics_calculates_win_rate_profit_factor_expectancy_and_costs():
    trades = [make_trade("10"), make_trade("-5"), make_trade("15")]

    metrics = calculate_performance_metrics(trades, min_sample_trades=3)

    assert metrics.total_trades == 3
    assert metrics.winning_trades == 2
    assert metrics.losing_trades == 1
    assert metrics.win_rate == Decimal("0.6666666666666666666666666667")
    assert metrics.profit_factor == Decimal("5")
    assert metrics.expectancy == Decimal("6.666666666666666666666666667")
    assert metrics.total_fees == Decimal("0.3")
    assert metrics.total_slippage == Decimal("0.6")
    assert metrics.total_spread == Decimal("0.9")
    assert metrics.total_funding == Decimal("1.2")
    assert metrics.sample_sufficient is True


def test_metrics_calculates_drawdown_and_consecutive_losses():
    trades = [make_trade("10"), make_trade("-3"), make_trade("-4"), make_trade("2")]

    metrics = calculate_performance_metrics(trades, initial_balance=Decimal("100"))

    assert metrics.max_drawdown == Decimal("7")
    assert metrics.max_drawdown_pct == Decimal("0.07")
    assert metrics.max_consecutive_losses == 2
    assert metrics.best_trade == Decimal("10")
    assert metrics.worst_trade == Decimal("-4")
