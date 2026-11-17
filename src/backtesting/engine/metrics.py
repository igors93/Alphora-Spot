from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from backtesting.domain.config import BacktestDecisionRecord, BacktestRejectedSignal
from backtesting.domain.realism import BacktestFillSettings, BacktestValidationSettings
from domain.models import PositionDirection
from domain.trade_result import TradeResult
from futures.performance.metrics import (
    PerformanceMetrics,
    calculate_performance_metrics,
)
from futures.performance.readiness import (
    ReadinessConfig,
    ReadinessResult,
    StrategyReadinessEvaluator,
)
from futures.trend import MarketRegime
from operational_module_modes import module_mode_effects, module_modes_from_mapping

TREND_ALIGNMENT_GROUPS = ("WITH_TREND", "AGAINST_TREND", "NEUTRAL_OR_UNKNOWN")
EXIT_STOP_KEYWORDS = ("stop", "stop_loss")
EXIT_TAKE_KEYWORDS = ("take", "take_profit")
EXIT_MAX_HOLD_KEYWORDS = ("max_hold", "max_hold_ticks")
EXIT_TREND_REVERSAL_KEYWORDS = ("trend_reversal",)


def build_backtest_summary(
    *,
    trades: list[TradeResult],
    decisions: list[BacktestDecisionRecord],
    rejected_signals: list[BacktestRejectedSignal],
    metrics: PerformanceMetrics,
    readiness: ReadinessResult,
    symbols_tested: list[str],
    strategy_mode: str,
    initial_balance: Decimal,
    min_sample_trades: int,
    min_profit_factor: Decimal,
    min_expectancy: Decimal = Decimal("0"),
    max_drawdown_pct: Decimal = Decimal("0.2"),
    max_consecutive_losses: int = 5,
    data_diagnostics: dict[str, Any] | None = None,
    execution_diagnostics: dict[str, Any] | None = None,
    sizing_diagnostics: dict[str, Any] | None = None,
    untradable_symbols: list[str] | None = None,
    allow_untradable_symbols: bool = False,
    timeframe_diagnostics: dict[str, Any] | None = None,
    strategy_class: str | None = None,
    operational_module_modes: dict[str, str] | None = None,
    statistical_validation: dict[str, Any] | None = None,
    model_data_validation: dict[str, Any] | None = None,
    validation_settings: BacktestValidationSettings | None = None,
    fill_settings: BacktestFillSettings | None = None,
) -> dict[str, Any]:
    by_direction = _direction_metrics(trades)
    by_symbol = _symbol_metrics(trades, symbols_tested)
    by_market_regime = _market_regime_metrics(trades, decisions)
    by_trend_alignment = _trend_alignment_metrics(trades, decisions)
    by_directional_regime = _decision_diagnostic_group_metrics(
        trades,
        decisions,
        diagnostic_key="directional_regime",
        default_group="NONE",
    )
    by_volatility_state = _decision_diagnostic_group_metrics(
        trades,
        decisions,
        diagnostic_key="volatility_state",
        default_group="UNKNOWN",
    )
    by_exit_reason = _exit_reason_metrics(trades)
    exit_reason_counts = Counter(_normalize_exit_reason(trade.exit_reason) for trade in trades)
    rejected_by_reason = Counter(item.reason for item in rejected_signals)
    rejected_by_layer = Counter(item.layer for item in rejected_signals)
    rejected_by_symbol = Counter(item.symbol for item in rejected_signals)
    decisions_by_symbol = Counter(item.symbol for item in decisions)
    decision_counts = Counter(item.decision_action for item in decisions)
    final_action_counts = Counter(item.final_action for item in decisions)
    block_reason_counts = Counter(item.block_reason for item in decisions if item.block_reason)
    ai_stats = _ai_diagnostics(trades, decisions)
    best_symbol, worst_symbol = _best_worst_symbol(by_symbol)
    concentration = _result_concentration(by_symbol)
    cycle_competition = _cycle_competition(decisions)
    execution_diagnostics = execution_diagnostics or {}
    ambiguous_stop_take_count = int(
        execution_diagnostics.get(
            "ambiguous_stop_take_count",
            sum(1 for trade in trades if str(trade.exit_reason).startswith("ambiguous_stop_take")),
        )
        or 0
    )
    sizing_diagnostics = sizing_diagnostics or {}
    untradable_symbols = sorted(untradable_symbols or [])
    timeframe_diagnostics = timeframe_diagnostics or {}
    operational_module_modes = operational_module_modes or {}
    statistical_validation = statistical_validation or {}
    model_data_validation = model_data_validation or {}
    validation_settings = validation_settings or BacktestValidationSettings()
    fill_settings = fill_settings or BacktestFillSettings()
    mode_resolution = module_modes_from_mapping(operational_module_modes)
    operational_module_mode_values = mode_resolution.mode_values()
    sizing_invalid = bool(untradable_symbols)
    cost_edge_diagnostics = _cost_edge_diagnostics(trades)
    dynamic_exit_diagnostics = _dynamic_exit_diagnostics(trades)
    smart_sizing_diagnostics = _smart_sizing_diagnostics(trades, rejected_signals)
    smart_execution_diagnostics = _smart_execution_diagnostics(trades, rejected_signals)
    state_reconciliation_diagnostics = _state_reconciliation_diagnostics(
        operational_module_mode_values
    )
    readiness_config = ReadinessConfig(
        min_trades=min_sample_trades,
        min_profit_factor=min_profit_factor,
        min_expectancy=min_expectancy,
        max_drawdown_pct=max_drawdown_pct,
        max_consecutive_losses=max_consecutive_losses,
    )
    readiness_by_direction = _readiness_by_direction(
        trades,
        readiness_config=readiness_config,
        initial_balance=initial_balance,
        sizing_invalid=sizing_invalid and not allow_untradable_symbols,
    )
    readiness_by_market_regime = _readiness_by_market_regime(
        trades,
        decisions,
        readiness_config=readiness_config,
        initial_balance=initial_balance,
    )
    strategy_blockers = _strategy_blockers(decisions)
    trend_warnings = _trend_warnings(
        trades=trades,
        by_direction=by_direction,
        by_market_regime=by_market_regime,
        by_trend_alignment=by_trend_alignment,
    )
    exit_warnings = _exit_reason_warnings(exit_reason_counts, len(trades))
    cost_warnings = _cost_edge_warnings(cost_edge_diagnostics)
    dynamic_exit_warnings = _dynamic_exit_warnings(
        strategy_class=strategy_class,
        diagnostics=dynamic_exit_diagnostics,
    )
    sizing_warnings = ["SYMBOL_NOT_TRADABLE_WITH_CONFIGURED_NOTIONAL"] if sizing_invalid else []
    data_quality_warnings = list((data_diagnostics or {}).get("quality_warnings", []))
    model_validation_warnings = list(model_data_validation.get("warnings", []))
    execution_reliability_warnings = _execution_reliability_warnings(execution_diagnostics)
    all_warnings = _dedupe(
        trend_warnings
        + exit_warnings
        + cost_warnings
        + dynamic_exit_warnings
        + sizing_warnings
        + data_quality_warnings
        + model_validation_warnings
        + execution_reliability_warnings
    )
    for symbol, metrics_by_symbol in by_symbol.items():
        metrics_by_symbol["decisions"] = decisions_by_symbol.get(symbol, 0)
        metrics_by_symbol["rejected_signals"] = rejected_by_symbol.get(symbol, 0)

    reliability_gates = {
        "data_quality_passed": not data_quality_warnings,
        "timeframes_passed": bool(timeframe_diagnostics.get("timeframe_match", True)),
        "no_intrabar_ambiguity": ambiguous_stop_take_count == 0,
        "no_limit_entry_path_ambiguity": int(
            execution_diagnostics.get("limit_entry_intrabar_ambiguity_count", 0) or 0
        )
        == 0,
        "no_open_positions_at_end": int(execution_diagnostics.get("open_positions_at_end", 0) or 0)
        == 0,
        "no_liquidations": int(execution_diagnostics.get("liquidation_count", 0) or 0) == 0,
        "liquidity_participation_reliable": int(
            execution_diagnostics.get("high_participation_fill_count", 0) or 0
        )
        == 0,
        "exit_liquidity_within_cap": int(
            execution_diagnostics.get("exit_liquidity_breach_count", 0) or 0
        )
        == 0,
        "market_impact_model_enabled": bool(
            execution_diagnostics.get("market_impact_model_enabled", False)
        ),
        "limit_trade_through_required": fill_settings.require_limit_trade_through,
        "funding_model_enabled": bool(execution_diagnostics.get("funding_model_enabled", False)),
        "funding_coverage_passed": bool(
            execution_diagnostics.get("funding_coverage_passed", False)
        ),
        "out_of_sample_verified": (
            bool(model_data_validation.get("out_of_sample_verified", False))
            if validation_settings.require_out_of_sample_for_approval
            else True
        ),
        "model_data_isolation_passed": not bool(
            model_data_validation.get("overlap_detected", False)
        ),
        "statistical_validation_passed": bool(statistical_validation.get("passed", False)),
    }
    reliability_passed = all(reliability_gates.values())
    verdict = _operational_verdict(
        metrics=metrics,
        readiness=readiness,
        min_sample_trades=min_sample_trades,
        min_profit_factor=min_profit_factor,
        sizing_invalid=sizing_invalid,
        reliability_passed=reliability_passed,
    )
    readiness_reasons = list(readiness.reasons)
    if sizing_invalid:
        readiness_reasons.insert(
            0,
            "Sizing invalido para simbolos: " + ", ".join(untradable_symbols),
        )
    failure_reasons = _top_failure_reasons(
        warnings=all_warnings,
        readiness_reasons=readiness_reasons,
        metrics=metrics,
        strategy_blockers=strategy_blockers,
        sizing_invalid=sizing_invalid,
        extra_reasons=[
            f"RELIABILITY_GATE_FAILED:{name}"
            for name, passed in reliability_gates.items()
            if not passed
        ],
    )
    recommended_actions = _next_recommended_actions(
        failure_reasons=failure_reasons,
        sizing_diagnostics=sizing_diagnostics,
        untradable_symbols=untradable_symbols,
        metrics=metrics,
    )

    return {
        "strategy_mode": strategy_mode,
        "symbols_tested": symbols_tested,
        "symbols_tested_count": len(symbols_tested),
        "symbols_with_trades": [
            symbol
            for symbol, symbol_metrics in by_symbol.items()
            if symbol_metrics["total_trades"] > 0
        ],
        "symbols_without_trades": [
            symbol
            for symbol, symbol_metrics in by_symbol.items()
            if symbol_metrics["total_trades"] == 0
        ],
        "data_feed": data_diagnostics or {},
        "timeframe": timeframe_diagnostics,
        "timeframe_by_symbol": {
            symbol: payload.get("timeframe")
            for symbol, payload in (timeframe_diagnostics.get("symbols", {}) or {}).items()
            if isinstance(payload, dict)
        },
        "strategy_required_timeframes": timeframe_diagnostics.get(
            "strategy_required_timeframes",
            ["1m"],
        ),
        "timeframe_match": timeframe_diagnostics.get("timeframe_match", True),
        "timeframe_warnings": timeframe_diagnostics.get("timeframe_warnings", []),
        "operational_module_modes": operational_module_mode_values,
        "operational_module_mode_effects": module_mode_effects(operational_module_mode_values),
        "sizing_diagnostics": sizing_diagnostics,
        "sizing_status": "INVALID_FOR_SIZING" if sizing_invalid else "OK",
        "untradable_symbols": untradable_symbols,
        "allow_untradable_symbols": allow_untradable_symbols,
        "execution_diagnostics": execution_diagnostics,
        "total_trades": metrics.total_trades,
        "win_rate": str(metrics.win_rate),
        "gross_pnl": str(metrics.gross_pnl),
        "net_pnl": str(metrics.net_pnl),
        "realized_net_pnl": str(metrics.realized_net_pnl),
        "unrealized_net_pnl": str(metrics.unrealized_net_pnl),
        "final_equity": str(metrics.final_equity),
        "total_return_pct": str(metrics.total_return_pct),
        "annualized_return_pct": str(metrics.annualized_return_pct),
        "sharpe_ratio": str(metrics.sharpe_ratio),
        "sortino_ratio": str(metrics.sortino_ratio),
        "calmar_ratio": str(metrics.calmar_ratio),
        "annualized_volatility": str(metrics.annualized_volatility),
        "average_trade_duration_seconds": str(metrics.average_trade_duration_seconds),
        "max_gross_exposure": str(metrics.max_gross_exposure),
        "max_margin_utilization_pct": str(metrics.max_margin_utilization_pct),
        "fees": str(metrics.total_fees),
        "slippage": str(metrics.total_slippage),
        "spread": str(metrics.total_spread),
        "funding": str(metrics.total_funding),
        "profit_factor": str(metrics.profit_factor),
        "expectancy": str(metrics.expectancy),
        "max_drawdown": str(metrics.max_drawdown),
        "max_drawdown_pct": str(metrics.max_drawdown_pct),
        "max_consecutive_losses": metrics.max_consecutive_losses,
        "trades_long": by_direction["LONG"]["total_trades"],
        "trades_short": by_direction["SHORT"]["total_trades"],
        "pnl_long": by_direction["LONG"]["net_pnl"],
        "pnl_short": by_direction["SHORT"]["net_pnl"],
        "rejected_signals_count": len(rejected_signals),
        "rejected_by_reason": dict(rejected_by_reason),
        "rejected_by_layer": dict(rejected_by_layer),
        "rejected_by_symbol": dict(rejected_by_symbol),
        "blocked_by_reason": dict(block_reason_counts),
        "blocked_by_governor": rejected_by_layer.get("GOVERNOR", 0),
        "blocked_by_risk": rejected_by_layer.get("RISK", 0),
        "blocked_by_exchange_rule": rejected_by_layer.get("EXCHANGE_RULE", 0),
        "decision_counts": dict(decision_counts),
        "final_action_counts": dict(final_action_counts),
        "ai_global_signals": ai_stats["ai_global_signals"],
        "ai_refined_signals": ai_stats["ai_refined_signals"],
        "avg_long_score_winners": ai_stats["avg_long_score_winners"],
        "avg_long_score_losers": ai_stats["avg_long_score_losers"],
        "avg_short_score_winners": ai_stats["avg_short_score_winners"],
        "avg_short_score_losers": ai_stats["avg_short_score_losers"],
        "best_symbol": best_symbol,
        "worst_symbol": worst_symbol,
        "profitable_symbols": [
            symbol
            for symbol, symbol_metrics in by_symbol.items()
            if Decimal(str(symbol_metrics.get("net_pnl", "0"))) > 0
        ],
        "losing_symbols": [
            symbol
            for symbol, symbol_metrics in by_symbol.items()
            if Decimal(str(symbol_metrics.get("net_pnl", "0"))) < 0
        ],
        "symbols_by_net_pnl": _symbols_by_net_pnl(by_symbol),
        "result_concentration": concentration,
        "cycle_competition": cycle_competition,
        "ambiguous_stop_take_count": ambiguous_stop_take_count,
        "ambiguous_policy": execution_diagnostics.get("ambiguous_policy"),
        "ambiguous_outcome_counts": execution_diagnostics.get("ambiguous_outcome_counts", {}),
        "open_positions_at_end": execution_diagnostics.get("open_positions_at_end", 0),
        "unrealized_pnl": execution_diagnostics.get("unrealized_pnl_at_end", "0"),
        "open_exposure_at_end": execution_diagnostics.get("open_exposure_at_end", "0"),
        "end_of_data_policy": execution_diagnostics.get("end_of_data_policy"),
        "end_of_data_closed_count": execution_diagnostics.get("end_of_data_closed_count", 0),
        "by_exit_reason": by_exit_reason,
        "exit_reason_counts": dict(exit_reason_counts),
        "stop_to_take_ratio": str(_stop_to_take_ratio(exit_reason_counts)),
        "stop_loss_dominates": "STOP_LOSS_DOMINATES" in exit_warnings,
        "max_hold_dominates": "MAX_HOLD_DOMINATES" in exit_warnings,
        "trend_reversal_exit_count": _trend_reversal_exit_count(exit_reason_counts),
        "cost_edge_diagnostics": cost_edge_diagnostics,
        "dynamic_exit_diagnostics": dynamic_exit_diagnostics,
        "smart_sizing_diagnostics": smart_sizing_diagnostics,
        "smart_execution_diagnostics": smart_execution_diagnostics,
        "state_reconciliation_diagnostics": state_reconciliation_diagnostics,
        "same_candle_exit_policy": execution_diagnostics.get(
            "same_candle_exit_policy",
            "CONSERVATIVE_STOP_FIRST",
        ),
        "by_symbol": by_symbol,
        "by_direction": by_direction,
        "by_market_regime": by_market_regime,
        "by_directional_regime": by_directional_regime,
        "by_volatility_state": by_volatility_state,
        "by_trend_alignment": by_trend_alignment,
        "readiness_by_direction": readiness_by_direction,
        "readiness_by_market_regime": readiness_by_market_regime,
        "strategy_blockers": strategy_blockers["by_blocker"],
        "strategy_blocked_by_reason": strategy_blockers["by_reason"],
        "top_strategy_blockers": strategy_blockers["top_strategy_blockers"],
        "blocked_setups_count": strategy_blockers["blocked_setups_count"],
        "blocked_setups_by_side": strategy_blockers["blocked_setups_by_side"],
        "blocked_setups_by_regime": strategy_blockers["blocked_setups_by_regime"],
        "warnings": all_warnings,
        "data_quality_warnings": data_quality_warnings,
        "execution_reliability_warnings": execution_reliability_warnings,
        "model_data_validation": model_data_validation,
        "statistical_validation": statistical_validation,
        "validation_settings": asdict(validation_settings),
        "fill_settings": asdict(fill_settings),
        "reliability_gates": reliability_gates,
        "reliability_passed": reliability_passed,
        "trend_warnings": trend_warnings,
        "exit_reason_warnings": exit_warnings,
        "cost_edge_warnings": cost_warnings,
        "dynamic_exit_warnings": dynamic_exit_warnings,
        "operational_verdict": verdict,
        "verdict_explanation": _verdict_explanation(verdict, failure_reasons),
        "top_failure_reasons": failure_reasons,
        "next_recommended_actions": recommended_actions,
        "readiness_status": (
            readiness.status.value
            if not readiness.passed or reliability_passed
            else "RESEARCH_ONLY"
        ),
        "readiness_passed": readiness.passed and reliability_passed,
        "economic_readiness_passed": readiness.passed,
        "readiness_reasons": readiness_reasons,
        "readiness_warnings": _dedupe(readiness.warnings + dynamic_exit_warnings),
        "sample_sufficient": metrics.sample_sufficient,
    }


def metrics_for_trades(
    trades: list[TradeResult],
    *,
    initial_balance: Decimal,
    min_sample_trades: int,
    equity_curve: Sequence[Any] | None = None,
) -> PerformanceMetrics:
    return calculate_performance_metrics(
        trades,
        initial_balance=initial_balance,
        min_sample_trades=min_sample_trades,
        equity_curve=equity_curve,
    )


def _direction_metrics(trades: list[TradeResult]) -> dict[str, dict[str, Any]]:
    result = {
        "LONG": _empty_group_metrics(),
        "SHORT": _empty_group_metrics(),
    }
    for direction in (PositionDirection.LONG, PositionDirection.SHORT):
        group_trades = [trade for trade in trades if trade.direction == direction]
        result[direction.value] = _trade_group_metrics(group_trades)
    return result


def _symbol_metrics(
    trades: list[TradeResult],
    symbols_tested: list[str],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeResult]] = defaultdict(list)
    for trade in trades:
        grouped[trade.symbol].append(trade)
    for symbol in symbols_tested:
        grouped.setdefault(symbol, [])
    return {
        symbol: _trade_group_metrics(symbol_trades)
        for symbol, symbol_trades in sorted(grouped.items())
    }


def _market_regime_metrics(
    trades: list[TradeResult],
    decisions: list[BacktestDecisionRecord],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeResult]] = {regime.value: [] for regime in MarketRegime}
    grouped.setdefault("UNCLASSIFIED", [])
    entry_decisions = _entry_decisions_by_trade_key(decisions)
    for trade in trades:
        decision = entry_decisions.get(_trade_entry_key(trade))
        regime = (
            str(decision.market_regime).upper().strip()
            if decision is not None and decision.market_regime
            else "UNCLASSIFIED"
        )
        grouped.setdefault(regime, []).append(trade)
    return {regime: _trade_group_metrics(group_trades) for regime, group_trades in grouped.items()}


def _trend_alignment_metrics(
    trades: list[TradeResult],
    decisions: list[BacktestDecisionRecord],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeResult]] = {alignment: [] for alignment in TREND_ALIGNMENT_GROUPS}
    entry_decisions = _entry_decisions_by_trade_key(decisions)
    for trade in trades:
        decision = entry_decisions.get(_trade_entry_key(trade))
        alignment = (
            str(decision.trend_alignment).upper().strip()
            if decision is not None and decision.trend_alignment
            else "NEUTRAL_OR_UNKNOWN"
        )
        if alignment not in grouped:
            alignment = "NEUTRAL_OR_UNKNOWN"
        grouped[alignment].append(trade)
    return {
        alignment: _trade_group_metrics(group_trades) for alignment, group_trades in grouped.items()
    }


def _decision_diagnostic_group_metrics(
    trades: list[TradeResult],
    decisions: list[BacktestDecisionRecord],
    *,
    diagnostic_key: str,
    default_group: str,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeResult]] = defaultdict(list)
    entry_decisions = _entry_decisions_by_trade_key(decisions)
    for trade in trades:
        decision = entry_decisions.get(_trade_entry_key(trade))
        diagnostic_value = None
        if decision is not None:
            trend_context = decision.metadata.get("trend_context")
            if isinstance(trend_context, dict):
                diagnostics = trend_context.get("diagnostics", {})
                if isinstance(diagnostics, dict):
                    diagnostic_value = diagnostics.get(diagnostic_key)
            diagnostic_value = diagnostic_value or decision.metadata.get(diagnostic_key)
        group = str(diagnostic_value or default_group).upper().strip()
        grouped[group].append(trade)
    if not grouped:
        grouped[default_group] = []
    return {
        group: _trade_group_metrics(group_trades) for group, group_trades in sorted(grouped.items())
    }


def _exit_reason_metrics(trades: list[TradeResult]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeResult]] = defaultdict(list)
    for trade in trades:
        grouped[_normalize_exit_reason(trade.exit_reason)].append(trade)
    return {
        reason: _trade_group_metrics(group_trades)
        for reason, group_trades in sorted(grouped.items())
    }


def _trade_group_metrics(trades: list[TradeResult]) -> dict[str, Any]:
    if not trades:
        return _empty_group_metrics()
    net_pnl = sum((trade.net_pnl for trade in trades), Decimal("0"))
    gross_pnl = sum((trade.gross_pnl for trade in trades), Decimal("0"))
    wins = sum(1 for trade in trades if trade.net_pnl > 0)
    losses = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    gross_profit = sum((trade.net_pnl for trade in trades if trade.net_pnl > 0), Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    total_cost = sum((_trade_total_cost(trade) for trade in trades), Decimal("0"))
    return {
        "total_trades": len(trades),
        "winning_trades": wins,
        "win_rate": str(Decimal(str(wins)) / Decimal(str(len(trades)))),
        "gross_pnl": str(gross_pnl),
        "net_pnl": str(net_pnl),
        "fees": str(sum((trade.fees for trade in trades), Decimal("0"))),
        "slippage": str(sum((trade.slippage_cost for trade in trades), Decimal("0"))),
        "spread": str(sum((trade.spread_cost for trade in trades), Decimal("0"))),
        "funding": str(sum((trade.funding_cost for trade in trades), Decimal("0"))),
        "profit_factor": str(_safe_div(gross_profit, gross_loss)),
        "expectancy": str(_safe_div(net_pnl, Decimal(str(len(trades))))),
        "avg_net_pnl": str(_safe_div(net_pnl, Decimal(str(len(trades))))),
        "avg_gross_pnl": str(_safe_div(gross_pnl, Decimal(str(len(trades))))),
        "avg_cost_per_trade": str(_safe_div(total_cost, Decimal(str(len(trades))))),
        "max_consecutive_losses": _max_consecutive_losses(trades),
        "best_trade": str(max(trade.net_pnl for trade in trades)),
        "worst_trade": str(min(trade.net_pnl for trade in trades)),
    }


def _empty_group_metrics() -> dict[str, Any]:
    return {
        "total_trades": 0,
        "winning_trades": 0,
        "win_rate": "0",
        "gross_pnl": "0",
        "net_pnl": "0",
        "fees": "0",
        "slippage": "0",
        "spread": "0",
        "funding": "0",
        "profit_factor": "0",
        "expectancy": "0",
        "avg_net_pnl": "0",
        "avg_gross_pnl": "0",
        "avg_cost_per_trade": "0",
        "max_consecutive_losses": 0,
        "best_trade": "0",
        "worst_trade": "0",
    }


def _entry_decisions_by_trade_key(
    decisions: list[BacktestDecisionRecord],
) -> dict[tuple[str, str, str], BacktestDecisionRecord]:
    result: dict[tuple[str, str, str], BacktestDecisionRecord] = {}
    for decision in decisions:
        if decision.final_status != "EXECUTED":
            continue
        if decision.final_action not in {"OPEN_LONG", "OPEN_SHORT"}:
            continue
        result.setdefault(
            (decision.symbol, decision.timestamp, decision.final_action),
            decision,
        )
    return result


def _trade_entry_key(trade: TradeResult) -> tuple[str, str, str]:
    action = "OPEN_LONG" if trade.direction is PositionDirection.LONG else "OPEN_SHORT"
    return (trade.symbol, trade.entry_time, action)


def _trade_total_cost(trade: TradeResult) -> Decimal:
    return trade.fees + trade.slippage_cost + trade.spread_cost + trade.funding_cost


def _max_consecutive_losses(trades: list[TradeResult]) -> int:
    current = 0
    maximum = 0
    for trade in trades:
        if trade.net_pnl < 0:
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def _trend_warnings(
    *,
    trades: list[TradeResult],
    by_direction: dict[str, dict[str, Any]],
    by_market_regime: dict[str, dict[str, Any]],
    by_trend_alignment: dict[str, dict[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    trades_long = int(by_direction["LONG"]["total_trades"])
    trades_short = int(by_direction["SHORT"]["total_trades"])
    if trades_short == 0 and trades_long > 0:
        warnings.append("LONG_ONLY_STRATEGY")
        if Decimal(str(by_direction["LONG"].get("net_pnl", "0"))) < 0:
            warnings.append("LONG_ONLY_NEGATIVE_RESULT")

    against = by_trend_alignment["AGAINST_TREND"]
    against_net = Decimal(str(against.get("net_pnl", "0")))
    against_trades = int(against.get("total_trades", 0))
    losing_trades = [trade for trade in trades if trade.net_pnl < 0]
    against_loss_share = _safe_decimal_share(
        abs(min(against_net, Decimal("0"))),
        sum((abs(trade.net_pnl) for trade in losing_trades), Decimal("0")),
    )
    if against_trades > 0 and against_net < 0 and against_loss_share >= Decimal("0.30"):
        warnings.append("AGAINST_TREND_LOSSES_DOMINATE")

    chop = by_market_regime.get(MarketRegime.CHOP.value, _empty_group_metrics())
    chop_trades = int(chop.get("total_trades", 0))
    chop_net = Decimal(str(chop.get("net_pnl", "0")))
    if chop_trades >= 3 and chop_net < 0:
        warnings.append("CHOP_TRADING_NEGATIVE")

    low_data = by_market_regime.get(MarketRegime.LOW_DATA.value, _empty_group_metrics())
    low_data_trades = int(low_data.get("total_trades", 0))
    if low_data_trades >= 5 or (
        len(trades) >= 10
        and _safe_decimal_share(
            Decimal(str(low_data_trades)),
            Decimal(str(len(trades))),
        )
        >= Decimal("0.20")
    ):
        warnings.append("LOW_DATA_TREND_CONTEXT")

    return warnings


def _exit_reason_warnings(
    exit_reason_counts: Counter,
    total_trades: int,
) -> list[str]:
    warnings: list[str] = []
    stop_count = _keyword_count(exit_reason_counts, EXIT_STOP_KEYWORDS)
    take_count = _keyword_count(exit_reason_counts, EXIT_TAKE_KEYWORDS)
    max_hold_count = _keyword_count(exit_reason_counts, EXIT_MAX_HOLD_KEYWORDS)
    if stop_count >= 3 and stop_count >= max(2 * take_count, take_count + 3):
        warnings.append("STOP_LOSS_DOMINATES")
    if total_trades > 0 and max_hold_count >= 3:
        if Decimal(str(max_hold_count)) / Decimal(str(total_trades)) >= Decimal("0.30"):
            warnings.append("MAX_HOLD_DOMINATES")
    return warnings


def _cost_edge_diagnostics(trades: list[TradeResult]) -> dict[str, Any]:
    total_trades = len(trades)
    gross_pnl = sum((trade.gross_pnl for trade in trades), Decimal("0"))
    net_pnl = sum((trade.net_pnl for trade in trades), Decimal("0"))
    total_costs = sum((_trade_total_cost(trade) for trade in trades), Decimal("0"))
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    abs_gross_pnl = abs(gross_pnl)
    avg_gross = _safe_div(gross_pnl, Decimal(str(total_trades)))
    avg_net = _safe_div(net_pnl, Decimal(str(total_trades)))
    avg_cost = _safe_div(total_costs, Decimal(str(total_trades)))
    return {
        "total_trades": total_trades,
        "gross_pnl": str(gross_pnl),
        "total_costs": str(total_costs),
        "net_pnl": str(net_pnl),
        "avg_gross_pnl_per_trade": str(avg_gross),
        "avg_net_pnl_per_trade": str(avg_net),
        "avg_total_cost_per_trade": str(avg_cost),
        "cost_to_abs_gross_pnl_ratio": str(_safe_div(total_costs, abs_gross_pnl)),
        "gross_edge_positive": gross_pnl > 0,
        "costs_dominate_result": total_costs > abs_gross_pnl and total_costs > 0,
        "min_required_avg_gross_pnl_to_break_even": str(avg_cost),
        "avg_winner_gross_pnl": str(_avg_decimal([trade.gross_pnl for trade in winners])),
        "avg_loser_gross_pnl": str(_avg_decimal([trade.gross_pnl for trade in losers])),
        "avg_winner_net_pnl": str(_avg_decimal([trade.net_pnl for trade in winners])),
        "avg_loser_net_pnl": str(_avg_decimal([trade.net_pnl for trade in losers])),
    }


def _dynamic_exit_diagnostics(trades: list[TradeResult]) -> dict[str, Any]:
    return {
        **_dynamic_exit_group(trades),
        "by_symbol": _dynamic_exit_grouped(trades, lambda trade: trade.symbol),
        "by_direction": _dynamic_exit_grouped(
            trades,
            lambda trade: trade.direction.value,
            defaults=("LONG", "SHORT"),
        ),
        "by_exit_source": _dynamic_exit_grouped(
            trades,
            lambda trade: str(getattr(trade, "exit_source", "") or "UNKNOWN").upper(),
            defaults=("STRATEGY_METADATA", "GLOBAL_FALLBACK"),
        ),
    }


def _dynamic_exit_grouped(
    trades: list[TradeResult],
    key_fn,
    *,
    defaults: tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeResult]] = defaultdict(list)
    for trade in trades:
        grouped[str(key_fn(trade) or "UNKNOWN")].append(trade)
    for key in defaults:
        grouped.setdefault(key, [])
    return {key: _dynamic_exit_group(group_trades) for key, group_trades in sorted(grouped.items())}


def _dynamic_exit_group(trades: list[TradeResult]) -> dict[str, Any]:
    total_trades = len(trades)
    dynamic_count = sum(1 for trade in trades if bool(trade.dynamic_exit_used))
    fallback_count = total_trades - dynamic_count
    invalid_count = sum(
        1 for trade in trades if getattr(trade, "planned_exit_rejection_reason", None)
    )
    return {
        "total_trades": total_trades,
        "dynamic_exit_used_count": dynamic_count,
        "global_fallback_count": fallback_count,
        "invalid_strategy_exit_count": invalid_count,
        "dynamic_exit_usage_rate": str(
            _safe_div(Decimal(str(dynamic_count)), Decimal(str(total_trades)))
        ),
        "avg_reward_risk_liquido": str(_avg_optional_trade_decimal(trades, "reward_risk_liquido")),
        "avg_raw_reward_risk": str(_avg_optional_trade_decimal(trades, "raw_reward_risk")),
        "avg_cost_to_reward_ratio": str(
            _avg_optional_trade_decimal(trades, "cost_to_reward_ratio")
        ),
        "avg_risk_pct": str(_avg_optional_trade_decimal(trades, "risk_pct")),
        "avg_reward_pct": str(_avg_optional_trade_decimal(trades, "reward_pct")),
    }


def _dynamic_exit_warnings(
    *,
    strategy_class: str | None,
    diagnostics: dict[str, Any],
) -> list[str]:
    normalized_class = str(strategy_class or "").upper()
    if "TRENDPULLBACK" not in normalized_class:
        return []
    if int(diagnostics.get("dynamic_exit_used_count", 0) or 0) == 0:
        return ["TRENDPULLBACK_DYNAMIC_EXIT_NOT_USED"]
    return []


def _execution_reliability_warnings(
    execution_diagnostics: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if int(execution_diagnostics.get("gap_stop_count", 0) or 0):
        warnings.append("GAP_STOPS_PRESENT")
    if int(execution_diagnostics.get("liquidation_count", 0) or 0):
        warnings.append("LIQUIDATIONS_PRESENT")
    if int(execution_diagnostics.get("partial_fill_count", 0) or 0):
        warnings.append("PARTIAL_FILLS_PRESENT")
    if int(execution_diagnostics.get("limit_entry_intrabar_ambiguity_count", 0) or 0):
        warnings.append("LIMIT_ENTRY_INTRABAR_PATH_AMBIGUITY")
    if int(execution_diagnostics.get("high_participation_fill_count", 0) or 0):
        warnings.append("HIGH_VOLUME_PARTICIPATION")
    if int(execution_diagnostics.get("exit_liquidity_breach_count", 0) or 0):
        warnings.append("EXIT_LIQUIDITY_CAP_EXCEEDED_WITH_STRESSED_IMPACT")
    if not bool(execution_diagnostics.get("market_impact_model_enabled", False)):
        warnings.append("MARKET_IMPACT_MODEL_DISABLED")
    if not bool(execution_diagnostics.get("funding_model_enabled", False)):
        warnings.append("FUNDING_MODEL_DISABLED")
    if not bool(execution_diagnostics.get("funding_coverage_passed", False)):
        warnings.append("FUNDING_HISTORY_INCOMPLETE")
    if int(execution_diagnostics.get("open_positions_at_end", 0) or 0):
        warnings.append("OPEN_POSITIONS_AT_END")
    return warnings


def _smart_sizing_diagnostics(
    trades: list[TradeResult],
    rejected_signals: list[BacktestRejectedSignal],
) -> dict[str, Any]:
    return {
        **_smart_sizing_group(trades, rejected_signals),
        "by_symbol": _smart_sizing_grouped(trades, lambda trade: trade.symbol),
        "by_direction": _smart_sizing_grouped(
            trades,
            lambda trade: trade.direction.value,
            defaults=("LONG", "SHORT"),
        ),
    }


def _smart_sizing_grouped(
    trades: list[TradeResult],
    key_fn,
    *,
    defaults: tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeResult]] = defaultdict(list)
    for trade in trades:
        grouped[str(key_fn(trade) or "UNKNOWN")].append(trade)
    for key in defaults:
        grouped.setdefault(key, [])
    return {
        key: _smart_sizing_group(group_trades, []) for key, group_trades in sorted(grouped.items())
    }


def _smart_sizing_group(
    trades: list[TradeResult],
    rejected_signals: list[BacktestRejectedSignal],
) -> dict[str, Any]:
    decisions = sum(1 for trade in trades if bool(trade.smart_sizing_used))
    affected_count = sum(1 for trade in trades if bool(trade.smart_sizing_affected_result))
    reductions = sum(
        1
        for trade in trades
        if bool(trade.smart_sizing_affected_result)
        and _trade_decimal_value(trade, "final_notional_usdt")
        < _trade_decimal_value(trade, "base_notional_usdt")
    )
    blocks = sum(1 for item in rejected_signals if item.layer == "SMART_SIZING")
    multipliers = [
        Decimal(str(trade.sizing_multiplier))
        for trade in trades
        if trade.sizing_multiplier is not None
    ]
    min_policy_usage = Counter(
        _trade_smart_sizing_diagnostic(trade, "min_notional_policy") or "UNKNOWN"
        for trade in trades
        if bool(trade.smart_sizing_used)
    )
    for signal in rejected_signals:
        if signal.layer == "SMART_SIZING":
            min_policy_usage[
                _rejected_smart_sizing_diagnostic(signal, "min_notional_policy") or "UNKNOWN"
            ] += 1
    reductions_by_reason = Counter(
        str(trade.sizing_reason or "UNKNOWN")
        for trade in trades
        if bool(trade.smart_sizing_affected_result)
    )
    return {
        "mode": _first_smart_sizing_mode(trades, rejected_signals),
        "total_decisions": decisions + blocks,
        "smart_sizing_decisions": decisions + blocks,
        "smart_sizing_reductions": reductions,
        "smart_sizing_blocks": blocks,
        "blocks": blocks,
        "reductions": reductions,
        "avg_sizing_multiplier": str(_avg_decimal(multipliers)),
        "avg_multiplier": str(_avg_decimal(multipliers)),
        "affected_result_count": affected_count,
        "min_notional_policy_usage": dict(sorted(min_policy_usage.items())),
        "reductions_by_reason": dict(sorted(reductions_by_reason.items())),
    }


def _smart_execution_diagnostics(
    trades: list[TradeResult],
    rejected_signals: list[BacktestRejectedSignal],
) -> dict[str, Any]:
    decisions = sum(1 for trade in trades if bool(trade.smart_execution_used))
    blocks = sum(1 for item in rejected_signals if item.layer == "SMART_EXECUTION")
    market_entries = sum(
        1 for trade in trades if str(trade.entry_order_type or "MARKET").upper() == "MARKET"
    )
    limit_entries = sum(
        1 for trade in trades if str(trade.entry_order_type or "").upper() == "LIMIT"
    )
    spread_values = _smart_execution_decimal_values(
        trades,
        rejected_signals,
        "spread_bps",
    )
    slippage_values = _smart_execution_decimal_values(
        trades,
        rejected_signals,
        "estimated_slippage_bps",
    )
    fallback_to_market_count = sum(
        1
        for trade in trades
        if bool(trade.fallback_to_market_used)
        or _trade_smart_execution_diagnostic(trade, "fallback_to_market_used") is True
    )
    limit_filled = sum(
        1 for trade in trades if str(trade.limit_fill_status or "") == "LIMIT_FILLED"
    )
    limit_expired = sum(
        1
        for signal in rejected_signals
        if signal.layer == "SMART_EXECUTION"
        and (
            signal.reason == "SMART_EXECUTION_LIMIT_EXPIRED"
            or _rejected_smart_execution_diagnostic(signal, "limit_fill_status") == "LIMIT_EXPIRED"
        )
    )
    limit_selected = limit_entries + limit_expired + fallback_to_market_count
    spread_blocks = sum(
        1
        for item in rejected_signals
        if item.layer == "SMART_EXECUTION" and item.reason == "SMART_EXECUTION_SPREAD_TOO_HIGH"
    )
    drift_blocks = sum(
        1
        for item in rejected_signals
        if item.layer == "SMART_EXECUTION" and item.reason == "SMART_EXECUTION_PRICE_DRIFT_TOO_HIGH"
    )
    stale_blocks = sum(
        1
        for item in rejected_signals
        if item.layer == "SMART_EXECUTION" and item.reason == "SMART_EXECUTION_MARKET_DATA_STALE"
    )
    return {
        "mode": _first_smart_execution_mode(trades, rejected_signals),
        "total_decisions": decisions + blocks,
        "smart_execution_decisions": decisions + blocks,
        "smart_execution_blocks": blocks,
        "blocks": blocks,
        "market_entries": market_entries,
        "market_selected": market_entries,
        "limit_entries": limit_entries,
        "limit_selected": limit_selected,
        "limit_filled": limit_filled,
        "limit_expired": limit_expired,
        "fallback_to_market_count": fallback_to_market_count,
        "fallback_to_market": fallback_to_market_count,
        "spread_blocks": spread_blocks,
        "drift_blocks": drift_blocks,
        "stale_blocks": stale_blocks,
        "avg_estimated_spread_bps": str(_avg_decimal(spread_values)),
        "avg_estimated_slippage_bps": str(_avg_decimal(slippage_values)),
        "affected_result_count": sum(
            1 for trade in trades if bool(trade.smart_execution_affected_result)
        ),
    }


def _state_reconciliation_diagnostics(
    operational_module_modes: dict[str, str],
) -> dict[str, Any]:
    return {
        "mode": operational_module_modes.get("state_reconciliation", "OFF"),
        "status": "NOT_APPLICABLE_IN_BACKTEST",
        "issues_count": 0,
        "snapshots": 0,
        "issues_by_code": {},
        "critical_issues": 0,
        "blocks": 0,
        "repairs_attempted": 0,
        "repairs_succeeded": 0,
        "repairs_failed": 0,
        "last_report_path": None,
        "should_block_new_entries": False,
    }


def _first_smart_sizing_mode(
    trades: list[TradeResult],
    rejected_signals: list[BacktestRejectedSignal],
) -> str:
    for trade in trades:
        if trade.smart_sizing_mode:
            return str(trade.smart_sizing_mode)
    for signal in rejected_signals:
        if signal.layer == "SMART_SIZING" and isinstance(signal.checks, dict):
            mode = signal.checks.get("mode")
            if mode:
                return str(mode)
    return "OFF"


def _first_smart_execution_mode(
    trades: list[TradeResult],
    rejected_signals: list[BacktestRejectedSignal],
) -> str:
    for trade in trades:
        if trade.smart_execution_mode:
            return str(trade.smart_execution_mode)
    for signal in rejected_signals:
        if signal.layer == "SMART_EXECUTION" and isinstance(signal.checks, dict):
            mode = signal.checks.get("mode")
            if mode:
                return str(mode)
    return "OFF"


def _trade_decimal_value(trade: TradeResult, attr: str) -> Decimal:
    value = getattr(trade, attr, None)
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _trade_smart_sizing_diagnostic(trade: TradeResult, key: str):
    diagnostics = trade.sizing_diagnostics
    if not isinstance(diagnostics, dict):
        return None
    nested = diagnostics.get("diagnostics")
    if isinstance(nested, dict) and key in nested:
        return nested.get(key)
    return diagnostics.get(key)


def _rejected_smart_sizing_diagnostic(signal: BacktestRejectedSignal, key: str):
    checks = signal.checks
    if not isinstance(checks, dict):
        return None
    nested = checks.get("diagnostics")
    if isinstance(nested, dict) and key in nested:
        return nested.get(key)
    return checks.get(key)


def _smart_execution_decimal_values(
    trades: list[TradeResult],
    rejected_signals: list[BacktestRejectedSignal],
    key: str,
) -> list[Decimal]:
    values = []
    for trade in trades:
        value = _trade_smart_execution_diagnostic(trade, key)
        if value is None:
            continue
        try:
            values.append(Decimal(str(value)))
        except Exception:
            continue
    for signal in rejected_signals:
        if signal.layer != "SMART_EXECUTION":
            continue
        value = _rejected_smart_execution_diagnostic(signal, key)
        if value is None:
            continue
        try:
            values.append(Decimal(str(value)))
        except Exception:
            continue
    return values


def _trade_smart_execution_diagnostic(trade: TradeResult, key: str):
    diagnostics = trade.smart_execution_diagnostics
    if not isinstance(diagnostics, dict):
        return None
    nested = diagnostics.get("diagnostics")
    if isinstance(nested, dict) and key in nested:
        return nested.get(key)
    return diagnostics.get(key)


def _rejected_smart_execution_diagnostic(
    signal: BacktestRejectedSignal,
    key: str,
):
    checks = signal.checks
    if not isinstance(checks, dict):
        return None
    nested = checks.get("diagnostics")
    if isinstance(nested, dict) and key in nested:
        return nested.get(key)
    return checks.get(key)


def _avg_optional_trade_decimal(trades: list[TradeResult], attr: str) -> Decimal:
    values: list[Decimal] = []
    for trade in trades:
        value = getattr(trade, attr, None)
        if value is None:
            continue
        values.append(Decimal(str(value)))
    return _avg_decimal(values)


def _cost_edge_warnings(diagnostics: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    gross_pnl = Decimal(str(diagnostics.get("gross_pnl", "0")))
    avg_gross = Decimal(str(diagnostics.get("avg_gross_pnl_per_trade", "0")))
    avg_cost = Decimal(str(diagnostics.get("avg_total_cost_per_trade", "0")))
    total_trades = int(diagnostics.get("total_trades", 0) or 0)
    if total_trades > 0 and gross_pnl < 0:
        warnings.append("NEGATIVE_GROSS_EDGE")
    if total_trades > 0 and diagnostics.get("costs_dominate_result"):
        warnings.append("COSTS_DOMINATE_RESULT")
    if total_trades > 0 and avg_gross <= avg_cost:
        warnings.append("AVG_TRADE_DOES_NOT_PAY_COSTS")
    return warnings


def _readiness_by_direction(
    trades: list[TradeResult],
    *,
    readiness_config: ReadinessConfig,
    initial_balance: Decimal,
    sizing_invalid: bool,
) -> dict[str, dict[str, Any]]:
    result = {}
    for direction in (PositionDirection.LONG, PositionDirection.SHORT):
        group_trades = [trade for trade in trades if trade.direction is direction]
        result[direction.value] = _readiness_group(
            group_trades,
            readiness_config=readiness_config,
            initial_balance=initial_balance,
            invalid_for_sizing=sizing_invalid,
        )
    return result


def _readiness_by_market_regime(
    trades: list[TradeResult],
    decisions: list[BacktestDecisionRecord],
    *,
    readiness_config: ReadinessConfig,
    initial_balance: Decimal,
) -> dict[str, dict[str, Any]]:
    result = {}
    entry_decisions = _entry_decisions_by_trade_key(decisions)
    for regime in MarketRegime:
        group_trades = [
            trade
            for trade in trades
            if (decision := entry_decisions.get(_trade_entry_key(trade))) is not None
            and decision.market_regime == regime.value
        ]
        result[regime.value] = _readiness_group(
            group_trades,
            readiness_config=readiness_config,
            initial_balance=initial_balance,
        )
    return result


def _readiness_group(
    trades: list[TradeResult],
    *,
    readiness_config: ReadinessConfig,
    initial_balance: Decimal,
    invalid_for_sizing: bool = False,
) -> dict[str, Any]:
    metrics = calculate_performance_metrics(
        trades,
        initial_balance=initial_balance,
        min_sample_trades=readiness_config.min_trades,
    )
    if invalid_for_sizing:
        status = "INVALID_FOR_SIZING"
        reasons = ["Grupo nao foi avaliado porque o sizing configurado nao e tradavel."]
    elif not trades:
        status = "NOT_APPLICABLE"
        reasons = ["Sem trades neste grupo."]
    elif metrics.total_trades < readiness_config.min_trades:
        status = "NEEDS_MORE_DATA"
        reasons = [
            (
                "Amostra insuficiente no grupo: "
                f"{metrics.total_trades}/{readiness_config.min_trades} trades."
            )
        ]
    else:
        evaluation = StrategyReadinessEvaluator(readiness_config).evaluate(metrics)
        status = "BACKTEST_OK" if evaluation.passed else "REJECTED"
        reasons = evaluation.reasons
    return {
        "sample_sufficient": metrics.total_trades >= readiness_config.min_trades,
        "total_trades": metrics.total_trades,
        "net_pnl": str(metrics.net_pnl),
        "profit_factor": str(metrics.profit_factor),
        "expectancy": str(metrics.expectancy),
        "max_consecutive_losses": metrics.max_consecutive_losses,
        "readiness_status": status,
        "readiness_reasons": reasons,
    }


def _strategy_blockers(decisions: list[BacktestDecisionRecord]) -> dict[str, Any]:
    by_blocker: dict[str, dict[str, Any]] = {}
    blocked_setups_by_side: Counter = Counter()
    blocked_setups_by_regime: Counter = Counter()
    blocked_setups_count = 0
    for decision in decisions:
        blockers = _setup_blockers(decision.metadata)
        if not blockers:
            continue
        if decision.final_action != "HOLD" and decision.decision_action != "HOLD":
            continue
        blocked_setups_count += 1
        side = str(decision.metadata.get("setup_side") or "UNKNOWN").upper().strip()
        regime = str(decision.market_regime or "UNKNOWN").upper().strip()
        blocked_setups_by_side[side] += 1
        blocked_setups_by_regime[regime] += 1
        for blocker in blockers:
            payload = by_blocker.setdefault(
                blocker,
                {
                    "count": 0,
                    "symbols": set(),
                    "regimes": set(),
                    "side": set(),
                },
            )
            payload["count"] += 1
            payload["symbols"].add(decision.symbol)
            payload["regimes"].add(regime)
            payload["side"].add(side)
    serializable = {
        blocker: {
            "count": payload["count"],
            "symbols": sorted(payload["symbols"]),
            "regimes": sorted(payload["regimes"]),
            "side": sorted(payload["side"]),
        }
        for blocker, payload in sorted(by_blocker.items())
    }
    top = [
        {"blocker": blocker, **payload}
        for blocker, payload in sorted(
            serializable.items(),
            key=lambda item: (item[1]["count"], item[0]),
            reverse=True,
        )[:10]
    ]
    return {
        "by_blocker": serializable,
        "by_reason": {blocker: payload["count"] for blocker, payload in serializable.items()},
        "top_strategy_blockers": top,
        "blocked_setups_count": blocked_setups_count,
        "blocked_setups_by_side": dict(blocked_setups_by_side),
        "blocked_setups_by_regime": dict(blocked_setups_by_regime),
    }


def _setup_blockers(metadata: dict[str, Any]) -> list[str]:
    raw = metadata.get("setup_blockers")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.upper().strip()] if raw.strip() else []
    if isinstance(raw, list | tuple | set):
        return [str(item).upper().strip() for item in raw if str(item).strip()]
    return []


def _normalize_exit_reason(reason: str) -> str:
    normalized = str(reason or "UNKNOWN").upper().strip()
    return normalized or "UNKNOWN"


def _keyword_count(counts: Counter, keywords: tuple[str, ...]) -> int:
    total = 0
    normalized_keywords = tuple(keyword.upper() for keyword in keywords)
    for reason, count in counts.items():
        reason_text = str(reason).upper()
        if any(keyword in reason_text for keyword in normalized_keywords):
            total += int(count)
    return total


def _stop_to_take_ratio(counts: Counter) -> Decimal:
    stop_count = Decimal(str(_keyword_count(counts, EXIT_STOP_KEYWORDS)))
    take_count = Decimal(str(_keyword_count(counts, EXIT_TAKE_KEYWORDS)))
    return _safe_div(stop_count, take_count)


def _trend_reversal_exit_count(counts: Counter) -> int:
    return _keyword_count(counts, EXIT_TREND_REVERSAL_KEYWORDS)


def _avg_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(str(len(values)))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _top_failure_reasons(
    *,
    warnings: list[str],
    readiness_reasons: list[str],
    metrics: PerformanceMetrics,
    strategy_blockers: dict[str, Any],
    sizing_invalid: bool,
    extra_reasons: list[str] | None = None,
) -> list[str]:
    reasons: list[str] = []
    reasons.extend(extra_reasons or [])
    if sizing_invalid:
        reasons.append("SYMBOL_NOT_TRADABLE_WITH_CONFIGURED_NOTIONAL")
    reasons.extend(warnings)
    if not metrics.sample_sufficient:
        reasons.append("SAMPLE_TOO_SMALL")
    if metrics.net_pnl <= 0:
        reasons.append("STRATEGY_NOT_READY")
    if strategy_blockers.get("blocked_setups_count", 0):
        reasons.append("STRATEGY_INTERNAL_BLOCKERS_PRESENT")
    for reason in readiness_reasons:
        if "Amostra insuficiente" in reason:
            reasons.append("NEEDS_MORE_DATA")
        elif "PnL liquido" in reason:
            reasons.append("NEGATIVE_NET_PNL")
        elif "Profit factor" in reason:
            reasons.append("LOW_PROFIT_FACTOR")
        elif "Sequencia de perdas" in reason:
            reasons.append("LOSS_STREAK_TOO_HIGH")
    return _dedupe(reasons)[:10]


def _next_recommended_actions(
    *,
    failure_reasons: list[str],
    sizing_diagnostics: dict[str, Any],
    untradable_symbols: list[str],
    metrics: PerformanceMetrics,
) -> list[str]:
    actions: list[str] = []
    if "SYMBOL_NOT_TRADABLE_WITH_CONFIGURED_NOTIONAL" in failure_reasons:
        actions.append("INCREASE_POSITION_NOTIONAL")
        for symbol in untradable_symbols:
            diagnostic = sizing_diagnostics.get(symbol, {})
            suggested = diagnostic.get("suggested_min_position_notional_usdt")
            if suggested:
                actions.append(f"{symbol}:USE_NOTIONAL_AT_LEAST_{suggested}")
    if "SAMPLE_TOO_SMALL" in failure_reasons or not metrics.sample_sufficient:
        actions.append("COLLECT_MORE_DATA")
    if "NEGATIVE_GROSS_EDGE" in failure_reasons:
        actions.append("REVIEW_ENTRY_EXIT_EDGE_BEFORE_COSTS")
    if "COSTS_DOMINATE_RESULT" in failure_reasons:
        actions.append("REDUCE_COSTS_OR_REQUIRE_LARGER_AVG_MOVE")
    if "STOP_LOSS_DOMINATES" in failure_reasons:
        actions.append("REVIEW_STOP_DISTANCE_AND_ENTRY_CONFIRMATION")
    if "STRATEGY_INTERNAL_BLOCKERS_PRESENT" in failure_reasons:
        actions.append("INSPECT_TOP_STRATEGY_BLOCKERS")
    if not actions:
        actions.append("VALIDATE_ON_OUT_OF_SAMPLE_DATA")
    return _dedupe(actions)


def _verdict_explanation(verdict: str, failure_reasons: list[str]) -> str:
    if verdict == "INVALID_FOR_SIZING":
        return "Backtest invalido para avaliar edge: ao menos um simbolo nao era tradavel com o notional configurado."
    if verdict == "REJECTED":
        return "Backtest rejeitado pelos criterios atuais: " + ", ".join(failure_reasons[:5])
    if verdict == "WATCHLIST":
        return "Backtest ainda requer cautela e mais validacao antes de qualquer ativacao."
    if verdict == "RESEARCH_ONLY":
        return "Resultado economicamente interessante, mas reprovado nos gates de confiabilidade."
    if verdict == "APPROVED":
        return "Backtest atende criterios conservadores iniciais, mas ainda exige paper/testnet."
    return "Veredito operacional nao classificado."


def _ai_diagnostics(
    trades: list[TradeResult],
    decisions: list[BacktestDecisionRecord],
) -> dict[str, Any]:
    ai_decisions = [
        decision
        for decision in decisions
        if decision.model_source
        or decision.long_score is not None
        or decision.short_score is not None
    ]
    entry_by_symbol_time = {
        (decision.symbol, decision.timestamp, decision.final_action): decision
        for decision in ai_decisions
        if decision.final_action in {"OPEN_LONG", "OPEN_SHORT"}
        and decision.final_status == "EXECUTED"
    }
    long_winners: list[float] = []
    long_losers: list[float] = []
    short_winners: list[float] = []
    short_losers: list[float] = []

    for trade in trades:
        action = "OPEN_LONG" if trade.direction == PositionDirection.LONG else "OPEN_SHORT"
        decision = entry_by_symbol_time.get((trade.symbol, trade.entry_time, action))
        if decision is None:
            continue
        target = (
            long_winners
            if trade.net_pnl > 0 and trade.direction == PositionDirection.LONG
            else long_losers
            if trade.direction == PositionDirection.LONG
            else short_winners
            if trade.net_pnl > 0
            else short_losers
        )
        value = (
            decision.long_score
            if trade.direction == PositionDirection.LONG
            else decision.short_score
        )
        if value is not None:
            target.append(float(value))

    source_counts = Counter(str(item.model_source) for item in ai_decisions if item.model_source)
    return {
        "ai_global_signals": source_counts.get("GLOBAL", 0),
        "ai_refined_signals": source_counts.get("SYMBOL_REFINED", 0),
        "avg_long_score_winners": _avg(long_winners),
        "avg_long_score_losers": _avg(long_losers),
        "avg_short_score_winners": _avg(short_winners),
        "avg_short_score_losers": _avg(short_losers),
    }


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return numerator / denominator


def _safe_decimal_share(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return numerator / denominator


def _best_worst_symbol(by_symbol: dict[str, dict[str, Any]]) -> tuple[str | None, str | None]:
    if not by_symbol:
        return None, None

    ordered = sorted(
        by_symbol.items(),
        key=lambda item: Decimal(str(item[1].get("net_pnl", "0"))),
    )
    return ordered[-1][0], ordered[0][0]


def _symbols_by_net_pnl(by_symbol: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"symbol": symbol, "net_pnl": str(symbol_metrics.get("net_pnl", "0"))}
        for symbol, symbol_metrics in sorted(
            by_symbol.items(),
            key=lambda item: Decimal(str(item[1].get("net_pnl", "0"))),
            reverse=True,
        )
    ]


def _result_concentration(by_symbol: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not by_symbol:
        return {
            "top_symbol": None,
            "top_symbol_abs_pnl_share": "0",
            "depends_on_few_symbols": False,
        }
    abs_total = sum(
        (
            abs(Decimal(str(symbol_metrics.get("net_pnl", "0"))))
            for symbol_metrics in by_symbol.values()
        ),
        Decimal("0"),
    )
    if abs_total <= 0:
        return {
            "top_symbol": None,
            "top_symbol_abs_pnl_share": "0",
            "depends_on_few_symbols": False,
        }
    top_symbol, top_metrics = max(
        by_symbol.items(),
        key=lambda item: abs(Decimal(str(item[1].get("net_pnl", "0")))),
    )
    share = abs(Decimal(str(top_metrics.get("net_pnl", "0")))) / abs_total
    return {
        "top_symbol": top_symbol,
        "top_symbol_abs_pnl_share": str(share),
        "depends_on_few_symbols": share >= Decimal("0.80"),
    }


def _cycle_competition(decisions: list[BacktestDecisionRecord]) -> dict[str, Any]:
    candidate_counts = [
        int(decision.cycle_candidate_count or 0)
        for decision in decisions
        if int(decision.cycle_candidate_count or 0) > 0
    ]
    multi_candidate_timestamps = {
        decision.timestamp for decision in decisions if int(decision.cycle_candidate_count or 0) > 1
    }
    return {
        "timestamps_with_multiple_candidates": len(multi_candidate_timestamps),
        "max_candidates_same_timestamp": max(candidate_counts) if candidate_counts else 0,
        "ranked_entry_decisions": sum(
            1 for decision in decisions if decision.opportunity_rank is not None
        ),
    }


def _operational_verdict(
    *,
    metrics: PerformanceMetrics,
    readiness: ReadinessResult,
    min_sample_trades: int,
    min_profit_factor: Decimal,
    sizing_invalid: bool = False,
    reliability_passed: bool = False,
) -> str:
    if sizing_invalid:
        return "INVALID_FOR_SIZING"

    if metrics.total_trades == 0 or metrics.net_pnl <= 0:
        return "REJECTED"
    if not reliability_passed:
        return "RESEARCH_ONLY"

    enough_profit_factor = metrics.profit_factor >= min_profit_factor or (
        metrics.losing_trades == 0 and metrics.net_pnl > 0
    )
    if readiness.passed and metrics.total_trades >= min_sample_trades and enough_profit_factor:
        return "APPROVED"

    return "WATCHLIST"


def metrics_as_dict(metrics: PerformanceMetrics) -> dict[str, Any]:
    return asdict(metrics)
