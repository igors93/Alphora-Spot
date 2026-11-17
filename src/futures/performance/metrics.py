from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from math import sqrt
from typing import Any

from domain.trade_result import TradeResult


@dataclass(frozen=True)
class PerformanceMetrics:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    total_fees: Decimal
    total_slippage: Decimal
    total_spread: Decimal
    total_funding: Decimal
    average_win: Decimal
    average_loss: Decimal
    payoff_ratio: Decimal
    profit_factor: Decimal
    expectancy: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    max_consecutive_losses: int
    average_trade_net_pnl: Decimal
    best_trade: Decimal
    worst_trade: Decimal
    sample_sufficient: bool
    realized_net_pnl: Decimal = Decimal("0")
    unrealized_net_pnl: Decimal = Decimal("0")
    final_equity: Decimal = Decimal("0")
    total_return_pct: Decimal = Decimal("0")
    annualized_return_pct: Decimal = Decimal("0")
    sharpe_ratio: Decimal = Decimal("0")
    sortino_ratio: Decimal = Decimal("0")
    calmar_ratio: Decimal = Decimal("0")
    annualized_volatility: Decimal = Decimal("0")
    average_trade_duration_seconds: Decimal = Decimal("0")
    max_gross_exposure: Decimal = Decimal("0")
    max_margin_utilization_pct: Decimal = Decimal("0")


def calculate_performance_metrics(
    trades: list[TradeResult],
    *,
    initial_balance: Decimal = Decimal("1000"),
    min_sample_trades: int = 30,
    equity_curve: Sequence[Any] | None = None,
) -> PerformanceMetrics:
    if not trades:
        final_equity = Decimal(str(equity_curve[-1].equity)) if equity_curve else initial_balance
        net_pnl = final_equity - initial_balance
        max_drawdown, max_drawdown_pct = (
            _calculate_equity_drawdown(equity_curve, initial_balance=initial_balance)
            if equity_curve
            else (Decimal("0"), Decimal("0"))
        )
        daily_returns = _daily_equity_returns(
            equity_curve or (),
            initial_balance=initial_balance,
        )
        sharpe, sortino, volatility = _risk_adjusted_metrics(daily_returns)
        return PerformanceMetrics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=Decimal("0"),
            gross_pnl=Decimal("0"),
            net_pnl=net_pnl,
            total_fees=Decimal("0"),
            total_slippage=Decimal("0"),
            total_spread=Decimal("0"),
            total_funding=Decimal("0"),
            average_win=Decimal("0"),
            average_loss=Decimal("0"),
            payoff_ratio=Decimal("0"),
            profit_factor=Decimal("0"),
            expectancy=Decimal("0"),
            max_drawdown=max_drawdown,
            max_drawdown_pct=max_drawdown_pct,
            max_consecutive_losses=0,
            average_trade_net_pnl=Decimal("0"),
            best_trade=Decimal("0"),
            worst_trade=Decimal("0"),
            sample_sufficient=False,
            unrealized_net_pnl=net_pnl,
            final_equity=final_equity,
            total_return_pct=_safe_div(net_pnl, initial_balance),
            annualized_return_pct=_annualized_return(
                equity_curve or (),
                initial_balance=initial_balance,
                final_equity=final_equity,
            ),
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            annualized_volatility=volatility,
        )

    wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losses = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    total_trades = len(trades)
    gross_pnl = sum((trade.gross_pnl for trade in trades), Decimal("0"))
    realized_net_pnl = sum((trade.net_pnl for trade in trades), Decimal("0"))
    final_equity = (
        Decimal(str(equity_curve[-1].equity))
        if equity_curve
        else initial_balance + realized_net_pnl
    )
    net_pnl = final_equity - initial_balance
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    average_win = _safe_div(gross_profit, Decimal(str(len(wins))))
    average_loss = _safe_div(sum(losses, Decimal("0")), Decimal(str(len(losses))))
    payoff_ratio = _safe_div(average_win, abs(average_loss))
    profit_factor = _safe_div(gross_profit, gross_loss)

    if equity_curve:
        max_drawdown, max_drawdown_pct = _calculate_equity_drawdown(
            equity_curve,
            initial_balance=initial_balance,
        )
    else:
        max_drawdown = _calculate_max_drawdown(trades)
        max_drawdown_pct = _safe_div(max_drawdown, initial_balance)
    daily_returns = _daily_equity_returns(equity_curve or (), initial_balance=initial_balance)
    sharpe_ratio, sortino_ratio, annualized_volatility = _risk_adjusted_metrics(daily_returns)
    annualized_return = _annualized_return(
        equity_curve or (),
        initial_balance=initial_balance,
        final_equity=final_equity,
    )
    calmar_ratio = annualized_return / max_drawdown_pct if max_drawdown_pct > 0 else Decimal("0")

    return PerformanceMetrics(
        total_trades=total_trades,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=_safe_div(Decimal(str(len(wins))), Decimal(str(total_trades))),
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        total_fees=sum((trade.fees for trade in trades), Decimal("0")),
        total_slippage=sum((trade.slippage_cost for trade in trades), Decimal("0")),
        total_spread=sum((trade.spread_cost for trade in trades), Decimal("0")),
        total_funding=sum((trade.funding_cost for trade in trades), Decimal("0")),
        average_win=average_win,
        average_loss=average_loss,
        payoff_ratio=payoff_ratio,
        profit_factor=profit_factor,
        expectancy=_safe_div(net_pnl, Decimal(str(total_trades))),
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        max_consecutive_losses=_calculate_max_consecutive_losses(trades),
        average_trade_net_pnl=_safe_div(net_pnl, Decimal(str(total_trades))),
        best_trade=max(trade.net_pnl for trade in trades),
        worst_trade=min(trade.net_pnl for trade in trades),
        sample_sufficient=total_trades >= min_sample_trades,
        realized_net_pnl=realized_net_pnl,
        unrealized_net_pnl=net_pnl - realized_net_pnl,
        final_equity=final_equity,
        total_return_pct=_safe_div(net_pnl, initial_balance),
        annualized_return_pct=annualized_return,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        calmar_ratio=calmar_ratio,
        annualized_volatility=annualized_volatility,
        average_trade_duration_seconds=_average_trade_duration_seconds(trades),
        max_gross_exposure=max(
            (Decimal(str(point.gross_exposure)) for point in equity_curve or ()),
            default=Decimal("0"),
        ),
        max_margin_utilization_pct=max(
            (
                _safe_div(Decimal(str(point.reserved_margin)), Decimal(str(point.equity)))
                for point in equity_curve or ()
                if Decimal(str(point.equity)) > 0
            ),
            default=Decimal("0"),
        ),
    )


def _calculate_max_drawdown(trades: list[TradeResult]) -> Decimal:
    equity = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")

    for trade in trades:
        equity += trade.net_pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    return max_drawdown


def _calculate_max_consecutive_losses(trades: list[TradeResult]) -> int:
    current = 0
    maximum = 0

    for trade in trades:
        if trade.net_pnl < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0

    return maximum


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")

    return numerator / denominator


def _calculate_equity_drawdown(
    equity_curve: Sequence[Any],
    *,
    initial_balance: Decimal,
) -> tuple[Decimal, Decimal]:
    peak = initial_balance
    max_drawdown = Decimal("0")
    max_drawdown_pct = Decimal("0")
    for point in equity_curve:
        equity = Decimal(str(point.equity))
        peak = max(peak, equity)
        drawdown = peak - equity
        drawdown_pct = _safe_div(drawdown, peak)
        if drawdown_pct > max_drawdown_pct:
            max_drawdown = drawdown
            max_drawdown_pct = drawdown_pct
    return max_drawdown, max_drawdown_pct


def _daily_equity_returns(
    equity_curve: Sequence[Any],
    *,
    initial_balance: Decimal | None = None,
) -> list[float]:
    closes: dict[str, Decimal] = {}
    for point in equity_curve:
        parsed = _parse_timestamp(str(point.timestamp))
        if parsed is None:
            continue
        closes[parsed.date().isoformat()] = Decimal(str(point.equity))
    values = [closes[key] for key in sorted(closes)]
    # The first day's P&L is measured against the starting balance; without it
    # the first trading day silently disappears from Sharpe/Sortino.
    if values and initial_balance is not None and initial_balance > 0:
        values.insert(0, Decimal(str(initial_balance)))
    returns: list[float] = []
    for previous, current in zip(values, values[1:], strict=False):
        if previous > 0:
            returns.append(float((current - previous) / previous))
    return returns


def _risk_adjusted_metrics(returns: list[float]) -> tuple[Decimal, Decimal, Decimal]:
    if len(returns) < 2:
        return Decimal("0"), Decimal("0"), Decimal("0")
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    stddev = sqrt(max(variance, 0.0))
    downside = [min(value, 0.0) for value in returns]
    downside_deviation = sqrt(sum(value * value for value in downside) / len(downside))
    annualizer = sqrt(365.0)
    sharpe = mean / stddev * annualizer if stddev > 0 else 0.0
    sortino = mean / downside_deviation * annualizer if downside_deviation > 0 else 0.0
    return Decimal(str(sharpe)), Decimal(str(sortino)), Decimal(str(stddev * annualizer))


def _annualized_return(
    equity_curve: Sequence[Any],
    *,
    initial_balance: Decimal,
    final_equity: Decimal,
) -> Decimal:
    if len(equity_curve) < 2 or initial_balance <= 0 or final_equity <= 0:
        return Decimal("0")
    start = _parse_timestamp(str(equity_curve[0].timestamp))
    end = _parse_timestamp(str(equity_curve[-1].timestamp))
    if start is None or end is None:
        return Decimal("0")
    days = max((end - start).total_seconds() / 86400.0, 0.0)
    if days < 1:
        return Decimal("0")
    annualized = float(final_equity / initial_balance) ** (365.0 / days) - 1.0
    return Decimal(str(annualized))


def _average_trade_duration_seconds(trades: list[TradeResult]) -> Decimal:
    durations: list[Decimal] = []
    for trade in trades:
        entry = _parse_timestamp(trade.entry_time)
        exit_ = _parse_timestamp(trade.exit_time)
        if entry is not None and exit_ is not None and exit_ >= entry:
            durations.append(Decimal(str((exit_ - entry).total_seconds())))
    return _safe_div(sum(durations, Decimal("0")), Decimal(str(len(durations))))


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
