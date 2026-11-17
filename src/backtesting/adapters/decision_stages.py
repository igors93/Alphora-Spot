"""Historical ports for the Governor's deterministic sizing and order stages.

The strategy and trend inputs are historical data; fills remain the responsibility
of ExecutionSimulator.  A session runs the same stage classes, in the same order,
as the live plan pipeline, with simulated market, clock and account state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from backtesting.domain.config import BacktestSymbolConfig, CandleEvent
from backtesting.engine.execution import ExecutionSimulator
from backtesting.engine.portfolio import PortfolioSimulator
from config.symbol_context import SymbolTradingConfig
from domain.candle import Candle
from domain.models import MarketSnapshot, TradeAction
from domain.opportunity import TradeOpportunity
from futures.costs.model import CostModel as PlanningCostModel
from futures.planning.exit_planner import ExitPlanner
from futures.trading_policy import TradingPolicy
from governor.evaluation.outcomes import StageOutcomes
from governor.evaluation.runtime_board import SymbolRuntimeBoard
from governor.models import GovernorDecision
from governor.smart_execution import (
    SmartExecutionDecision,
    SmartExecutionEngine,
    SmartExecutionInput,
)
from governor.smart_sizing import SmartSizingDecision, SmartSizingEngine
from governor.stages.decision_kernel import build_decision_kernel_stages
from governor.stages.execution_planning import SmartExecutionStage
from governor.stages.sizing import SmartSizingStage, build_smart_sizing_input
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext, SymbolEvaluationInput
from governor.trade_pipeline import TradePipeline, TradePipelineResult
from market_data.market_snapshots import HeavyMarketSnapshot
from operational_module_modes import (
    MODULE_DYNAMIC_EXIT,
    MODULE_SMART_EXECUTION,
    MODULE_SMART_SIZING,
)
from scanner.signal_score import SignalPriority, SignalScore
from trading.intent import TradeIntent
from trading_cadence import (
    DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES,
    effective_max_daily_trades,
    effective_min_seconds_between_entries,
    entry_cadence_block,
)


class HistoricalClock:
    def __init__(self, signal_timing_policy: str) -> None:
        self.signal_timing_policy = signal_timing_policy

    def now(self, event: CandleEvent) -> float:
        if self.signal_timing_policy == "ON_OPEN":
            return event.candle.open_time_ms / 1000
        return (event.candle.close_time_ms + 1) / 1000

    def price(self, event: CandleEvent) -> Decimal:
        value = event.candle.open if self.signal_timing_policy == "ON_OPEN" else event.candle.close
        return Decimal(str(value))


class HistoricalMarketPort:
    def __init__(self, execution: ExecutionSimulator, clock: HistoricalClock) -> None:
        self.execution = execution
        self.clock = clock

    def _funding_rate(self, symbol: str, timestamp: str) -> Decimal | None:
        broker = self.execution.brokers.get(symbol)
        if broker is None or not broker.funding_rate_points:
            return None
        target_ms = _timestamp_ms(timestamp)
        if target_ms is None:
            return None
        latest_rate: Decimal | None = None
        latest_ms: int | None = None
        for point in broker.funding_rate_points:
            point_ms = _timestamp_ms(point.timestamp)
            if point_ms is None or point_ms > target_ms:
                continue
            if latest_ms is None or point_ms >= latest_ms:
                latest_ms = point_ms
                latest_rate = Decimal(str(point.rate))
        return latest_rate

    def heavy(self, event: CandleEvent, symbol_config: BacktestSymbolConfig) -> HeavyMarketSnapshot:
        price = self.clock.price(event)
        interval = str(getattr(event.candle, "interval", "15m") or "15m")
        candles: dict[str, tuple[Candle, ...]] = (
            {str(k): tuple(v) for k, v in event.candle_snapshot.items()}
            if event.candle_snapshot
            else {interval: (event.candle,)}
        )
        market = MarketSnapshot(
            symbol=event.symbol,
            price=price,
            prices=[price],
            indicators={"timestamp": event.timestamp},
            candles=candles,
            primary_timeframe=interval,
        )
        return HeavyMarketSnapshot(
            symbol=event.symbol,
            market=market,
            mark_price=price,
            exchange_rules=symbol_config.rules,
            position=self.execution.current_position(event.symbol),
            open_orders=[],
            open_algo_orders=[],
            account_info={},
            timestamp=self.clock.now(event),
            data_age_seconds=0.0,
            stale=False,
            funding_rate=self._funding_rate(event.symbol, event.timestamp),
        )


class SimulatedAccountPort:
    def __init__(
        self,
        execution: ExecutionSimulator,
        *,
        signal_timing_policy: str = "ON_CLOSE",
        min_seconds_between_entries: float = DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES,
        max_daily_trades: int = 0,
    ) -> None:
        self.execution = execution
        self.signal_timing_policy = signal_timing_policy
        self.min_seconds_between_entries = effective_min_seconds_between_entries(
            min_seconds_between_entries
        )
        self.max_daily_trades = effective_max_daily_trades(max_daily_trades)
        self._entry_times_ms_by_symbol: dict[str, list[int]] = {}
        self._indexed_trade_count = 0

    def cadence_metrics(self, event: CandleEvent) -> tuple[int, float | None]:
        trades = self.execution.trades
        for trade in trades[self._indexed_trade_count :]:
            entry_ms = _timestamp_ms(trade.entry_time)
            if entry_ms is not None:
                self._entry_times_ms_by_symbol.setdefault(trade.symbol, []).append(entry_ms)
        self._indexed_trade_count = len(trades)
        entries = self._entry_times_ms_by_symbol.get(event.symbol)
        if not entries:
            return 0, None
        signal_ms = (
            int(event.candle.open_time_ms)
            if self.signal_timing_policy == "ON_OPEN"
            else int(event.candle.close_time_ms) + 1
        )
        last_entry_ms = max(entries)
        elapsed_seconds = (signal_ms - last_entry_ms) / 1000
        day_start_ms = signal_ms - signal_ms % 86_400_000
        entries_today = sum(1 for entry_ms in entries if day_start_ms <= entry_ms < signal_ms)
        return entries_today, elapsed_seconds

    def cadence_block(self, event: CandleEvent) -> tuple[str, dict[str, Any]] | None:
        entries_today, elapsed_seconds = self.cadence_metrics(event)
        if elapsed_seconds is None:
            return None
        blocked = entry_cadence_block(
            opened_trades_today=entries_today,
            seconds_since_entry=elapsed_seconds,
            max_daily_trades=self.max_daily_trades,
            min_seconds_between_entries=self.min_seconds_between_entries,
        )
        if blocked == "min_seconds_between_entries":
            return (
                "MIN_SECONDS_BETWEEN_ENTRIES",
                {
                    "seconds_since_last_entry": elapsed_seconds,
                    "min_seconds_between_entries": self.min_seconds_between_entries,
                },
            )
        if blocked == "max_daily_trades":
            return (
                "MAX_DAILY_TRADES",
                {"entries_today": entries_today, "max_daily_trades": self.max_daily_trades},
            )
        return None

    def daily_performance(self, timestamp: str) -> tuple[Decimal, int, int]:
        day = str(timestamp)[:10]
        trades = [trade for trade in self.execution.trades if str(trade.exit_time)[:10] == day]
        realized = sum((trade.net_pnl for trade in trades), Decimal("0"))
        consecutive = 0
        for trade in reversed(trades):
            if trade.net_pnl < 0:
                consecutive += 1
            else:
                break
        return realized, len(trades), consecutive

    def snapshot(self, event: CandleEvent) -> dict[str, Any]:
        realized, trades, consecutive = self.daily_performance(event.timestamp)
        drawdown = self.execution.current_drawdown_pct()
        return {
            "daily_realized_pnl": str(realized),
            "daily_trades": trades,
            "consecutive_losses": consecutive,
            "daily_drawdown": {"available": True, "fraction": drawdown},
            "open_positions": self.execution.open_positions_snapshot(),
        }


class _NullJournal:
    def append(self, event_type: str, **data: Any) -> None:
        pass


@dataclass
class HistoricalDecisionSession:
    evaluation: SymbolEvaluationContext
    stages: tuple[SmartSizingStage, SmartExecutionStage]
    sizing_engine: SmartSizingEngine
    execution_engine: SmartExecutionEngine
    module_modes: dict[str, Any]
    execution_input_factory: Any
    sizing_block_reason: str | None = None

    def size(self) -> SmartSizingDecision:
        if self.module_modes[MODULE_SMART_SIZING].value == "OFF":
            decision = self.sizing_engine.evaluate(
                build_smart_sizing_input(self.evaluation, self.module_modes[MODULE_SMART_SIZING])
            )
            self.evaluation.smart_sizing_decision = decision
        else:
            outcome = self.stages[0](self.evaluation)
            if outcome is not None and outcome.terminal:
                self.sizing_block_reason = outcome.reason
            sized = self.evaluation.smart_sizing_decision
            if sized is None:
                raise RuntimeError("SmartSizingStage did not produce a decision")
            decision = sized
        return decision

    def approved_notional(self, sizing: SmartSizingDecision) -> Decimal:
        if sizing.affected_result and self.evaluation.planned_quantity is not None:
            price = Decimal(str(self.evaluation.intent.market_price))  # type: ignore[union-attr]
            return self.evaluation.planned_quantity * price
        return sizing.final_notional_usdt

    def choose_execution(self, sizing: SmartSizingDecision) -> SmartExecutionDecision:
        execution_input = self.execution_input_factory(sizing)
        if self.module_modes[MODULE_SMART_EXECUTION].value == "OFF":
            decision = self.execution_engine.evaluate(execution_input)
            self.evaluation.smart_execution_decision = decision
        else:
            self.stages[1](self.evaluation)
            chosen = self.evaluation.smart_execution_decision
            if chosen is None:
                raise RuntimeError("SmartExecutionStage did not produce a decision")
            decision = chosen
        return decision


def _build_backtest_trading_policy(
    *,
    execution: ExecutionSimulator,
    symbol_configs: dict[str, BacktestSymbolConfig],
    module_modes: dict[str, Any],
    execution_engine: SmartExecutionEngine,
    signal_timing_policy: str,
    market_entry_policy: str,
    limit_activation_policy: str,
    limit_entry_timeout_candles: int,
    min_seconds_between_entries: float,
    max_daily_trades: int,
) -> TradingPolicy:
    first_cfg = next(iter(symbol_configs.values()), None)
    stop_pct = first_cfg.stop_loss_pct if first_cfg is not None else Decimal("0.01")
    take_pct = first_cfg.take_profit_pct if first_cfg is not None else Decimal("0.02")
    bt_cost = execution.cost_model
    cost_model = PlanningCostModel(
        maker_fee_pct=bt_cost.maker_fee_pct,
        taker_fee_pct=bt_cost.taker_fee_pct,
        estimated_spread_bps=bt_cost.spread_bps,
        estimated_slippage_bps=bt_cost.slippage_bps,
        estimated_funding_rate=bt_cost.default_funding_rate,
    )
    dynamic_exit_mode = module_modes.get(MODULE_DYNAMIC_EXIT)
    dynamic_exit_enabled = getattr(dynamic_exit_mode, "value", str(dynamic_exit_mode)) not in {
        "OFF",
        "SHADOW",
    }
    smart_sizing_mode = module_modes.get(MODULE_SMART_SIZING)
    dynamic_sizing_enabled = getattr(smart_sizing_mode, "value", str(smart_sizing_mode)) not in {
        "OFF",
        "SHADOW",
    }
    return TradingPolicy(
        signal_timing_policy=signal_timing_policy,
        market_entry_policy=market_entry_policy,
        entry_order_type="LIMIT" if execution_engine.settings.limit_entry_enabled else "MARKET",
        limit_activation_policy=limit_activation_policy,
        limit_entry_timeout_candles=limit_entry_timeout_candles,
        limit_fallback_to_market=execution_engine.settings.limit_entry_fallback_to_market,
        require_limit_trade_through=execution.fill_settings.require_limit_trade_through,
        assume_limit_maker=execution.fill_settings.assume_limit_maker,
        exit_planner=ExitPlanner(stop_loss_pct=stop_pct, take_profit_pct=take_pct),
        cost_model=cost_model,
        dynamic_exit_enabled=dynamic_exit_enabled,
        dynamic_sizing_enabled=dynamic_sizing_enabled,
        ambiguity_policy=execution.same_candle_exit_policy,
        min_seconds_between_entries=effective_min_seconds_between_entries(
            min_seconds_between_entries
        ),
        max_daily_trades=effective_max_daily_trades(max_daily_trades),
    )


class HistoricalDecisionStages:
    def __init__(
        self,
        *,
        execution: ExecutionSimulator,
        portfolio: PortfolioSimulator,
        symbol_configs: dict[str, BacktestSymbolConfig],
        module_modes: dict[str, Any],
        sizing_engine: SmartSizingEngine,
        execution_engine: SmartExecutionEngine,
        signal_timing_policy: str,
        strategy_name: str,
        min_seconds_between_entries: float = DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES,
        max_daily_trades: int = 0,
        market_entry_policy: str = "NEXT_OPEN",
        limit_activation_policy: str = "NEXT_CANDLE",
        limit_entry_timeout_candles: int = 1,
        trading_policy: TradingPolicy | None = None,
    ) -> None:
        self.execution = execution
        self.portfolio = portfolio
        self.symbol_configs = symbol_configs
        self.module_modes = module_modes
        self.sizing_engine = sizing_engine
        self.execution_engine = execution_engine
        self.strategy_name = strategy_name
        self.clock = HistoricalClock(signal_timing_policy)
        self.market = HistoricalMarketPort(execution, self.clock)
        self.account = SimulatedAccountPort(
            execution,
            signal_timing_policy=signal_timing_policy,
            min_seconds_between_entries=min_seconds_between_entries,
            max_daily_trades=max_daily_trades,
        )
        self._context_by_symbol = {item.symbol: item for item in portfolio.symbol_contexts}
        self.trading_policy = trading_policy or _build_backtest_trading_policy(
            execution=execution,
            symbol_configs=symbol_configs,
            module_modes=module_modes,
            execution_engine=execution_engine,
            signal_timing_policy=signal_timing_policy,
            market_entry_policy=market_entry_policy,
            limit_activation_policy=limit_activation_policy,
            limit_entry_timeout_candles=limit_entry_timeout_candles,
            min_seconds_between_entries=min_seconds_between_entries,
            max_daily_trades=max_daily_trades,
        )
        self.trade_pipeline = TradePipeline(trading_policy=self.trading_policy)
        self.opportunities: list[TradeOpportunity] = []

    def _symbol_trading_config(self, symbol: str) -> SymbolTradingConfig:
        max_exposure = getattr(self.portfolio.settings, "max_symbol_exposure_usdt", 0)
        if not max_exposure:
            max_exposure = getattr(self.portfolio.settings, "max_total_exposure_usdt", 0)
        return replace(
            self._context_by_symbol[symbol],
            max_exposure_usdt=max_exposure,
            max_daily_loss_usdt=getattr(self.portfolio.settings, "max_daily_loss_usdt", 0),
        )

    def build_pipeline_result(
        self,
        *,
        event: CandleEvent,
        score: SignalScore,
        action: TradeAction,
        reason: str,
        heavy: HeavyMarketSnapshot | None = None,
        account_snapshot: dict[str, Any] | None = None,
    ) -> TradePipelineResult:
        """Build the ``TradePipelineResult`` (with ``TradeOpportunity``) for ``event``."""

        bt_config = self.symbol_configs[event.symbol]
        symbol_trading_config = self._symbol_trading_config(event.symbol)
        heavy_snap = heavy or self.market.heavy(event, bt_config)
        account = account_snapshot or self.account.snapshot(event)
        price = self.clock.price(event)
        entries_today, elapsed_seconds = self.account.cadence_metrics(event)
        intent = TradeIntent(
            symbol=event.symbol,
            action=action.value,
            score=score.score,
            priority=score.priority,
            confidence=score.confidence,
            reason=reason,
            strategy_name=score.strategy or self.strategy_name,
            market_price=price,
            created_at=self.clock.now(event),
        )
        result = self.trade_pipeline.build_plan(
            intent=intent,
            symbol_config=symbol_trading_config,
            heavy_snapshot=heavy_snap,
            portfolio_snapshot=account,
            score=score,
            timeframe=str(getattr(event.candle, "interval", "15m") or "15m"),
            opened_trades_today=entries_today,
            seconds_since_last_entry=elapsed_seconds,
        )
        if result.opportunity is not None:
            self.opportunities.append(result.opportunity)
        return result

    def opportunity_for_open(
        self,
        *,
        event: CandleEvent,
        score: SignalScore,
        action: TradeAction,
        reason: str,
    ) -> TradeOpportunity | None:
        return self.build_pipeline_result(
            event=event,
            score=score,
            action=action,
            reason=reason,
        ).opportunity

    def evaluate_intent(
        self,
        *,
        event: CandleEvent,
        score: SignalScore,
        action: str,
        reason: str,
        override_notional: Decimal | None = None,
    ) -> GovernorDecision:
        """Supply simulated account state to the same portfolio risk engine as live."""
        approved_notional = (
            Decimal(str(override_notional))
            if override_notional is not None
            else self.symbol_configs[event.symbol].position_notional_usdt
        )
        price = self.clock.price(event)
        intent = TradeIntent(
            symbol=event.symbol,
            action=action,
            score=score.score,
            priority=score.priority or SignalPriority.COLD,
            confidence=score.confidence,
            reason=reason,
            strategy_name=self.strategy_name,
            created_at=self.clock.now(event),
            market_price=price,
            suggested_next_check_seconds=score.suggested_next_check_seconds,
            metadata={
                "score_metadata": dict(score.metadata),
                "notional": str(approved_notional),
                "risk_notional_source": (
                    "SMART_SIZING_FINAL_NOTIONAL"
                    if override_notional is not None
                    else "SYMBOL_CONFIG"
                ),
            },
        )
        realized, trades, consecutive = self.account.daily_performance(event.timestamp)
        decision = self.portfolio.evaluate(
            intent,
            open_positions=self.execution.open_positions_snapshot(),
            available_balance_usdt=str(self.execution.available_balance({event.symbol: price})),
            daily_realized_pnl=str(realized),
            daily_trades=trades,
            consecutive_losses=consecutive,
            override_notional=approved_notional,
        )
        if decision.allowed:
            decision.checks["candidate_id"] = intent.intent_id
            decision.checks["candidate_revision"] = 0
            decision.checks["approved_notional"] = str(approved_notional)
            decision.checks["approved_quantity"] = str(
                approved_notional / price if price > 0 else Decimal("0")
            )
        return decision

    def for_open(
        self,
        *,
        event: CandleEvent,
        score: SignalScore,
        action: TradeAction,
        reason: str,
        planned_exit: dict[str, Any],
    ) -> HistoricalDecisionSession:
        config = self.symbol_configs[event.symbol]
        price = self.clock.price(event)
        account = self.account.snapshot(event)
        heavy = self.market.heavy(event, config)
        symbol_trading_config = self._symbol_trading_config(event.symbol)
        pipeline_result = self.build_pipeline_result(
            event=event,
            score=score,
            action=action,
            reason=reason,
            heavy=heavy,
            account_snapshot=account,
        )

        def execution_input(sizing: SmartSizingDecision) -> SmartExecutionInput:
            cost = self.execution.cost_model
            settings = self.execution_engine.settings
            half_spread = cost.spread_bps / Decimal("20000")
            return SmartExecutionInput(
                symbol=event.symbol,
                side=action.value,
                entry_price=price,
                current_price=price,
                mark_price=price,
                bid=price * (Decimal("1") - half_spread),
                ask=price * (Decimal("1") + half_spread),
                estimated_spread_bps=cost.spread_bps,
                estimated_slippage_bps=cost.slippage_bps,
                limit_entry_enabled=settings.limit_entry_enabled,
                limit_entry_timeout_seconds=settings.limit_entry_timeout_seconds,
                limit_entry_max_price_drift_bps=settings.max_price_drift_bps,
                limit_entry_fallback_to_market=settings.limit_entry_fallback_to_market,
                limit_entry_post_only=settings.limit_entry_post_only,
                planned_exit=planned_exit,
                notional=(
                    Decimal(str(evaluation.planned_quantity)) * price
                    if evaluation.planned_quantity is not None
                    else sizing.final_notional_usdt
                ),
                mode=self.module_modes[MODULE_SMART_EXECUTION],
                market_data_age_seconds=Decimal("0"),
                market_data_stale=False,
                settings=settings,
            )

        journal = _NullJournal()
        board = SymbolRuntimeBoard(
            {}, strategy=_HistoricalStrategyName(score.strategy or "backtest")
        )
        outcomes = StageOutcomes(journal=journal, trace_payload=lambda: {}, board=board)
        evaluation = SymbolEvaluationContext(
            evaluation_input=SymbolEvaluationInput(
                symbol_config=symbol_trading_config,
                now=self.clock.now(event),
                cycle_id=event.timestamp,
                symbol_eval_id=f"{event.symbol}:{event.timestamp}",
            ),
            score=score,
            heavy=heavy,
            intent=pipeline_result.intent,
            trade_pipeline_result=pipeline_result,
            opportunity=pipeline_result.opportunity,
            trade_plan_id=pipeline_result.plan.plan_id,
            account_snapshot=account,
            planned_exit=planned_exit,
            planned_quantity=config.position_notional_usdt / price,
        )
        stages = build_decision_kernel_stages(
            outcomes=outcomes,
            module_modes=self.module_modes,
            sizing_engine=lambda: self.sizing_engine,
            execution_engine=lambda: self.execution_engine,
            config=None,
            journal=journal,
            trace_payload=lambda: {},
            record_transition=lambda *args, **kwargs: None,
            execution_input_factory=lambda evaluation: execution_input(
                evaluation.smart_sizing_decision  # type: ignore[arg-type]
            ),
            limit_entry_supported=True,
            audit_category="backtesting",
        )
        return HistoricalDecisionSession(
            evaluation=evaluation,
            stages=stages,
            sizing_engine=self.sizing_engine,
            execution_engine=self.execution_engine,
            module_modes=self.module_modes,
            execution_input_factory=execution_input,
        )


@dataclass(frozen=True)
class _HistoricalStrategyName:
    global_strategy_name: str


def _timestamp_ms(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = Decimal(text)
    except ArithmeticError:
        numeric = None
    if numeric is not None and numeric.is_finite():
        return int(numeric if numeric > Decimal("100000000000") else numeric * 1000)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)
