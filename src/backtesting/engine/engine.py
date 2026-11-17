from __future__ import annotations

from datetime import datetime, timezone

from backtesting.domain.config import BacktestConfig, BacktestRunResult
from backtesting.engine.decision_pipeline import GovernorSimulator
from backtesting.engine.execution import ExecutionSimulator
from backtesting.engine.metrics import build_backtest_summary, metrics_for_trades
from backtesting.engine.portfolio import PortfolioSimulator
from backtesting.engine.sizing import (
    build_sizing_diagnostics,
    sizing_diagnostics_as_dict,
    untradable_symbols,
)
from backtesting.engine.statistics import build_statistical_validation
from backtesting.engine.strategy_runtime import BacktestStrategyRuntime
from backtesting.engine.validation import (
    build_model_data_validation,
    enforce_model_data_isolation,
)
from backtesting.infrastructure.csv_feed import HistoricalDataFeed
from backtesting.infrastructure.report_writer import write_backtest_report
from futures.performance.readiness import ReadinessConfig, StrategyReadinessEvaluator
from futures.trend import TrendContextEngine
from utils.events import EventChannel, observe_event


def run_backtest(config: BacktestConfig, *, write_reports: bool = True) -> BacktestRunResult:
    started_at = datetime.now(timezone.utc).isoformat()
    observe_event(
        "backtest_started",
        component=__name__,
        category="backtesting",
        channel=EventChannel.AUDIT,
        symbols=config.symbol_names,
        strategy_mode=config.strategy.normalized_mode,
        output_dir=config.output_dir,
    )
    symbol_configs = list(config.symbols)
    feed = HistoricalDataFeed.from_symbol_configs(
        symbol_configs,
        start_timestamp=config.data_start_timestamp,
        end_timestamp=config.data_end_timestamp,
    )
    data_diagnostics = feed.diagnostics().as_dict()
    if config.fail_on_data_quality_warnings and data_diagnostics.get("quality_warnings"):
        raise ValueError(
            "Backtest bloqueado por qualidade de dados: "
            + ", ".join(data_diagnostics["quality_warnings"])
        )
    model_data_validation = build_model_data_validation(
        config,
        data_diagnostics=data_diagnostics,
    )
    enforce_model_data_isolation(config, model_data_validation)
    runtime = BacktestStrategyRuntime(config.strategy, config.symbol_names)
    timeframe_diagnostics = _build_timeframe_diagnostics(
        symbol_configs=symbol_configs,
        requirements_by_symbol=runtime.requirements_by_symbol,
        feed=feed,
    )
    _validate_timeframes(timeframe_diagnostics)
    sizing_diagnostics = build_sizing_diagnostics(
        symbol_configs,
        feed.candles_by_symbol,
    )
    blocked_by_sizing = set(untradable_symbols(sizing_diagnostics))
    if blocked_by_sizing:
        observe_event(
            "sizing_invalid",
            component=__name__,
            category="backtesting",
            channel=EventChannel.AUDIT,
            severity="WARNING",
            symbols=sorted(blocked_by_sizing),
            diagnostics=sizing_diagnostics_as_dict(sizing_diagnostics),
        )
    runnable_symbol_configs = [
        item
        for item in symbol_configs
        if config.allow_untradable_symbols or item.symbol not in blocked_by_sizing
    ]
    runnable_symbols = {item.symbol for item in runnable_symbol_configs}
    execution = ExecutionSimulator(
        symbols=runnable_symbol_configs,
        cost_model=config.cost_model,
        same_candle_exit_policy=config.same_candle_exit_policy,
        end_of_data_policy=config.end_of_data_policy,
        initial_balance=config.initial_balance,
        fill_settings=config.fill_settings,
        funding_rates_by_symbol=config.funding_rates_by_symbol,
    )
    portfolio = PortfolioSimulator(
        symbols=runnable_symbol_configs,
        strategy=config.strategy,
        settings=config.portfolio_settings,
        initial_balance=str(config.initial_balance),
    )
    governor = GovernorSimulator(
        strategy_runtime=runtime,
        portfolio=portfolio,
        execution=execution,
        strategy_config=config.strategy,
        symbol_configs=runnable_symbol_configs,
        initial_balance=config.initial_balance,
        rank_cycle_candidates=config.rank_cycle_candidates,
        trend_context_enabled=config.trend_context_enabled,
        trend_context_mode=config.trend_context_mode,
        trend_context_engine=TrendContextEngine.from_config(config),
        trend_timeframe=config.trend_timeframe,
        operational_module_modes=config.operational_module_modes,
        smart_sizing_settings=config.smart_sizing_settings,
        smart_execution_settings=config.smart_execution_settings,
        signal_timing_policy=config.signal_timing_policy,
        market_entry_policy=config.market_entry_policy,
        limit_activation_policy=config.limit_activation_policy,
        end_of_data_policy=config.end_of_data_policy,
        limit_entry_timeout_candles=config.limit_entry_timeout_candles,
        min_seconds_between_entries=config.min_seconds_between_entries,
        max_daily_trades=config.max_daily_trades,
    )

    for _timestamp, events in feed.iter_cycles(
        signal_timing_policy=config.signal_timing_policy,
        include_primary=True,
    ):
        events = [event for event in events if event.symbol in runnable_symbols]
        if not events:
            continue
        if not _events_in_evaluation_window(events, config):
            governor.ingest_warmup(events)
            continue
        governor.start_cycle()
        for event in events:
            governor.process_candle_open(event)
        if config.signal_timing_policy == "ON_OPEN":
            governor.process_signal_cycle(events)
        for event in events:
            governor.process_intrabar_exit(event)
        if config.signal_timing_policy == "ON_CLOSE":
            governor.process_signal_cycle(events)
        governor.finish_cycle(events)
        execution.mark_to_market(
            timestamp=_cycle_close_timestamp(events),
            prices={event.symbol: event.candle.close for event in events},
        )
    governor.flush_pending_entries()
    governor.finalize_end_of_data()
    if governor.last_event_by_symbol:
        execution.mark_to_market(
            timestamp=_cycle_close_timestamp(list(governor.last_event_by_symbol.values())),
            prices={
                symbol: event.candle.close
                for symbol, event in governor.last_event_by_symbol.items()
            },
        )

    readiness_evaluator = StrategyReadinessEvaluator(
        ReadinessConfig(
            min_trades=config.min_sample_trades,
            min_profit_factor=config.min_profit_factor,
            min_expectancy=config.min_expectancy,
            max_drawdown_pct=config.max_drawdown_pct,
            max_consecutive_losses=config.max_consecutive_losses,
        )
    )
    metrics = metrics_for_trades(
        execution.trades,
        initial_balance=config.initial_balance,
        min_sample_trades=config.min_sample_trades,
        equity_curve=execution.equity_curve,
    )
    readiness = readiness_evaluator.evaluate(metrics)
    statistical_validation = build_statistical_validation(
        execution.trades,
        initial_balance=config.initial_balance,
        settings=config.validation_settings,
    )
    summary = build_backtest_summary(
        trades=execution.trades,
        decisions=governor.decisions,
        rejected_signals=governor.rejected_signals,
        metrics=metrics,
        readiness=readiness,
        symbols_tested=config.symbol_names,
        strategy_mode=config.strategy.normalized_mode,
        initial_balance=config.initial_balance,
        min_sample_trades=config.min_sample_trades,
        min_profit_factor=config.min_profit_factor,
        min_expectancy=config.min_expectancy,
        max_drawdown_pct=config.max_drawdown_pct,
        max_consecutive_losses=config.max_consecutive_losses,
        data_diagnostics=data_diagnostics,
        execution_diagnostics=execution.execution_diagnostics(),
        sizing_diagnostics=sizing_diagnostics_as_dict(sizing_diagnostics),
        untradable_symbols=sorted(blocked_by_sizing),
        allow_untradable_symbols=config.allow_untradable_symbols,
        timeframe_diagnostics=timeframe_diagnostics,
        strategy_class=runtime.class_name,
        operational_module_modes=config.operational_module_modes,
        statistical_validation=statistical_validation,
        model_data_validation=model_data_validation,
        validation_settings=config.validation_settings,
        fill_settings=config.fill_settings,
    )
    result = BacktestRunResult(
        config=config,
        trades=execution.trades,
        decisions=governor.decisions,
        rejected_signals=governor.rejected_signals,
        metrics=metrics,
        readiness=readiness,
        summary=summary,
        governor_decisions=governor.governor_decisions,
        data_diagnostics=data_diagnostics,
        equity_curve=execution.equity_curve,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    if write_reports:
        write_backtest_report(result)
    observe_event(
        "backtest_finished",
        component=__name__,
        category="backtesting",
        channel=EventChannel.AUDIT,
        symbols=config.symbol_names,
        total_trades=result.summary.get("total_trades"),
        rejected_signals_count=result.summary.get("rejected_signals_count"),
        net_pnl=result.summary.get("net_pnl"),
        paths=result.paths,
    )
    return result


def _cycle_close_timestamp(events) -> str:
    close_time_ms = max(event.candle.close_time_ms for event in events)
    return datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc).isoformat()


def _events_in_evaluation_window(events, config: BacktestConfig) -> bool:
    first_open_ms = min(event.candle.open_time_ms for event in events)
    if config.evaluation_start_timestamp:
        start = _evaluation_boundary(config.evaluation_start_timestamp)
        if first_open_ms < int(start.timestamp() * 1000):
            return False
    if config.evaluation_end_timestamp:
        end = _evaluation_boundary(config.evaluation_end_timestamp, end_of_day=True)
        if first_open_ms > int(end.timestamp() * 1000):
            return False
    return True


def _evaluation_boundary(value: str, *, end_of_day: bool = False) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if end_of_day and len(value.strip()) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed.astimezone(timezone.utc)


def _build_timeframe_diagnostics(
    *,
    symbol_configs,
    requirements_by_symbol,
    feed,
) -> dict[str, object]:
    required_normalized = sorted(
        {
            timeframe
            for requirement in requirements_by_symbol.values()
            for timeframe in requirement.required_candles
        }
    ) or ["1m"]
    diagnostics_by_symbol = {}
    warnings = []
    for symbol_config in symbol_configs:
        timeframe = str(getattr(symbol_config, "timeframe", "1m") or "1m").strip()
        requirement = requirements_by_symbol.get(symbol_config.symbol)
        required_counts = (
            dict(requirement.required_candles) if requirement is not None else {timeframe: 1}
        )
        primary_timeframe = requirement.primary_timeframe if requirement is not None else timeframe
        available_timeframes = {
            timeframe,
            *dict(symbol_config.csv_paths_by_timeframe or {}).keys(),
        }
        missing_timeframes = sorted(set(required_counts) - available_timeframes)
        available_counts = {
            tf: (
                len(feed.candles_by_symbol.get(symbol_config.symbol, []))
                if tf == timeframe
                else len(feed._secondary_candles.get(symbol_config.symbol, {}).get(tf, []))
            )
            for tf in available_timeframes
        }
        insufficient_history = {
            tf: {"required": count, "available": available_counts.get(tf, 0)}
            for tf, count in required_counts.items()
            if available_counts.get(tf, 0) < count
        }
        match = (
            timeframe == primary_timeframe and not missing_timeframes and not insufficient_history
        )
        symbol_warnings = []
        if not match:
            warning = (
                f"TIMEFRAME_MISMATCH:{symbol_config.symbol}:{timeframe}:"
                f"primary={primary_timeframe}:missing={','.join(missing_timeframes)}:"
                f"insufficient={','.join(insufficient_history)}"
            )
            symbol_warnings.append(warning)
            warnings.append(warning)
        diagnostics_by_symbol[symbol_config.symbol] = {
            "timeframe": timeframe,
            "strategy_required_timeframes": list(required_normalized),
            "strategy_primary_timeframe": primary_timeframe,
            "required_candles": required_counts,
            "available_timeframes": sorted(available_timeframes),
            "available_candles": available_counts,
            "missing_timeframes": missing_timeframes,
            "insufficient_history": insufficient_history,
            "timeframe_match": match,
            "timeframe_warnings": symbol_warnings,
        }
    return {
        "symbols": diagnostics_by_symbol,
        "strategy_required_timeframes": list(required_normalized),
        "timeframe_warnings": warnings,
        "timeframe_match": not warnings,
    }


def _validate_timeframes(timeframe_diagnostics: dict[str, object]) -> None:
    if bool(timeframe_diagnostics.get("timeframe_match", False)):
        return
    raw_warnings = timeframe_diagnostics.get("timeframe_warnings", [])
    warnings = raw_warnings if isinstance(raw_warnings, list) else [raw_warnings]
    observe_event(
        "timeframe_mismatch",
        component=__name__,
        category="backtesting",
        channel=EventChannel.AUDIT,
        severity="ERROR",
        warnings=warnings,
        diagnostics=timeframe_diagnostics,
    )
    raise ValueError(
        "Backtest timeframe incompatível com a estratégia: "
        + ", ".join(str(item) for item in warnings)
    )
