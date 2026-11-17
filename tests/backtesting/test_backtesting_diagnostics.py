import json
from decimal import Decimal

from backtesting.domain.config import BacktestDecisionRecord
from backtesting.engine.metrics import build_backtest_summary
from domain.models import PositionDirection
from domain.trade_result import TradeResult
from futures.performance.metrics import calculate_performance_metrics
from futures.performance.readiness import ReadinessConfig, StrategyReadinessEvaluator


def test_backtesting_summary_aggregates_exit_reason_and_stop_warning():
    trades = [
        _trade("1", "2", Decimal("-1"), "stop_loss"),
        _trade("3", "4", Decimal("-1"), "stop_loss"),
        _trade("5", "6", Decimal("-1"), "stop_loss"),
        _trade("5.5", "6.5", Decimal("-1"), "stop_loss"),
        _trade("7", "8", Decimal("2"), "take_profit"),
        _trade("9", "10", Decimal("-0.5"), "MAX_HOLD_TICKS"),
    ]
    summary = _summary(trades)

    assert summary["by_exit_reason"]["STOP_LOSS"]["total_trades"] == 4
    assert summary["by_exit_reason"]["TAKE_PROFIT"]["winning_trades"] == 1
    assert summary["by_exit_reason"]["MAX_HOLD_TICKS"]["total_trades"] == 1
    assert summary["exit_reason_counts"]["STOP_LOSS"] == 4
    assert summary["stop_loss_dominates"] is True
    assert "STOP_LOSS_DOMINATES" in summary["warnings"]


def test_backtesting_summary_cost_edge_diagnostics_warn_negative_gross_edge():
    trades = [
        _trade("1", "2", Decimal("-1"), "stop_loss", fees=Decimal("0.10")),
        _trade("3", "4", Decimal("-1"), "stop_loss", fees=Decimal("0.10")),
    ]
    summary = _summary(trades)

    diagnostics = summary["cost_edge_diagnostics"]
    assert diagnostics["gross_edge_positive"] is False
    assert "NEGATIVE_GROSS_EDGE" in summary["warnings"]
    json.dumps(diagnostics)


def test_backtesting_summary_costs_can_dominate_result():
    trades = [
        _trade("1", "2", Decimal("0.10"), "take_profit", fees=Decimal("1.00")),
    ]
    summary = _summary(trades)

    diagnostics = summary["cost_edge_diagnostics"]
    assert diagnostics["costs_dominate_result"] is True
    assert "COSTS_DOMINATE_RESULT" in summary["warnings"]
    assert "AVG_TRADE_DOES_NOT_PAY_COSTS" in summary["warnings"]


def test_backtesting_summary_readiness_by_direction_and_regime():
    trades = [
        _trade("1", "2", Decimal("1"), "take_profit", direction=PositionDirection.LONG),
        _trade("3", "4", Decimal("-1"), "stop_loss", direction=PositionDirection.SHORT),
    ]
    decisions = [
        _decision("1", "OPEN_LONG", "TREND_UP"),
        _decision("3", "OPEN_SHORT", "TREND_DOWN"),
    ]
    summary = _summary(trades, decisions=decisions, min_sample_trades=3)

    assert summary["readiness_by_direction"]["LONG"]["readiness_status"] == "NEEDS_MORE_DATA"
    assert summary["readiness_by_direction"]["SHORT"]["readiness_status"] == "NEEDS_MORE_DATA"
    assert summary["readiness_by_market_regime"]["TREND_UP"]["total_trades"] == 1
    assert summary["readiness_by_market_regime"]["RANGE"]["readiness_status"] == "NOT_APPLICABLE"


def test_backtesting_summary_contains_final_failure_recommendations():
    summary = _summary([], untradable_symbols=["BNBUSDT"])

    assert "top_failure_reasons" in summary
    assert "SYMBOL_NOT_TRADABLE_WITH_CONFIGURED_NOTIONAL" in summary["top_failure_reasons"]
    assert "INCREASE_POSITION_NOTIONAL" in summary["next_recommended_actions"]


def _summary(
    trades,
    *,
    decisions=None,
    min_sample_trades=1,
    untradable_symbols=None,
):
    decisions = decisions or [
        _decision(
            trade.entry_time,
            "OPEN_LONG" if trade.direction is PositionDirection.LONG else "OPEN_SHORT",
            "TREND_UP",
        )
        for trade in trades
    ]
    metrics = calculate_performance_metrics(
        trades,
        initial_balance=Decimal("1000"),
        min_sample_trades=min_sample_trades,
    )
    readiness = StrategyReadinessEvaluator(ReadinessConfig(min_trades=min_sample_trades)).evaluate(
        metrics
    )
    sizing_diagnostics = {
        symbol: {
            "suggested_min_position_notional_usdt": "612",
            "tradable_with_config": False,
        }
        for symbol in (untradable_symbols or [])
    }
    return build_backtest_summary(
        trades=trades,
        decisions=decisions,
        rejected_signals=[],
        metrics=metrics,
        readiness=readiness,
        symbols_tested=["BTCUSDT"],
        strategy_mode="FIXED",
        initial_balance=Decimal("1000"),
        min_sample_trades=min_sample_trades,
        min_profit_factor=Decimal("1.2"),
        sizing_diagnostics=sizing_diagnostics,
        untradable_symbols=untradable_symbols or [],
        timeframe_diagnostics={
            "symbols": {"BTCUSDT": {"timeframe": "1m"}},
            "strategy_required_timeframes": ["1m"],
            "timeframe_match": True,
            "timeframe_warnings": [],
        },
    )


def _trade(
    entry_time,
    exit_time,
    gross_pnl,
    exit_reason,
    *,
    direction=PositionDirection.LONG,
    fees=Decimal("0"),
):
    net_pnl = gross_pnl - fees
    return TradeResult(
        symbol="BTCUSDT",
        direction=direction,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        quantity=Decimal("1"),
        notional=Decimal("100"),
        gross_pnl=gross_pnl,
        fees=fees,
        slippage_cost=Decimal("0"),
        spread_cost=Decimal("0"),
        funding_cost=Decimal("0"),
        net_pnl=net_pnl,
        return_pct=net_pnl / Decimal("100"),
        exit_reason=exit_reason,
        strategy_reason_entry="entry",
        strategy_reason_exit="exit",
    )


def _decision(timestamp, action, regime):
    return BacktestDecisionRecord(
        timestamp=timestamp,
        symbol="BTCUSDT",
        price=Decimal("100"),
        strategy_mode="FIXED",
        score=95,
        priority="READY",
        score_action=action,
        decision_action=action,
        final_action=action,
        final_status="EXECUTED",
        reason="entry",
        market_regime=regime,
        trend_alignment="WITH_TREND",
        metadata={
            "trend_context": {
                "diagnostics": {
                    "directional_regime": regime,
                    "volatility_state": "NORMAL",
                }
            }
        },
    )
