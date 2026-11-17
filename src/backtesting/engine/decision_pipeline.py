import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from backtesting.adapters.decision_stages import HistoricalDecisionStages
from backtesting.adapters.planned_exit import HistoricalPlannedExitPort
from backtesting.adapters.signal import HistoricalSignalPort
from backtesting.domain.config import (
    BacktestDecisionRecord,
    BacktestRejectedSignal,
    BacktestStrategyConfig,
    BacktestSymbolConfig,
    CandleEvent,
)
from backtesting.engine.execution import BacktestLiquidityError, ExecutionSimulator
from backtesting.engine.portfolio import PortfolioSimulator
from backtesting.engine.strategy_runtime import BacktestStrategyRuntime
from config.regime_policy_modes import normalize_regime_policy_mode
from config.smart_settings import SmartExecutionSettings, SmartSizingSettings
from domain.barriers import limit_entry_touched
from domain.models import TradeAction
from futures.risk_manager import TradeNotExecutableError
from futures.trend import TrendContextEngine
from governor.opportunity_ranker import OpportunityRanker, OpportunityScore
from governor.portfolio_risk_engine import resolve_correlation_group
from governor.smart_execution import SmartExecutionDecision, SmartExecutionEngine
from governor.smart_sizing import SmartSizingDecision, SmartSizingEngine
from operational_module_modes import (
    module_modes_from_mapping,
)
from scanner.signal_score import SignalPriority, SignalScore
from trading_cadence import (
    DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES,
)


@dataclass
class _SignalCandidate:
    event: CandleEvent
    score: SignalScore
    action: TradeAction
    reason: str
    evaluation_metadata: dict[str, Any] = field(default_factory=dict)
    opportunity: OpportunityScore | None = None
    opportunity_rank: int | None = None
    tie_breaker: int = 0


@dataclass
class _PendingLimitEntry:
    event: CandleEvent
    score: SignalScore
    action: TradeAction
    strategy_reason: str
    evaluation_metadata: dict[str, Any]
    governor_decision: Any
    planned_exit: dict[str, Any]
    sizing_decision: SmartSizingDecision
    planned_quantity: Decimal | None
    smart_execution_decision: SmartExecutionDecision
    cycle_candidate_count: int
    opportunity_rank: int | None
    opportunity_score: int | None
    opportunity_reasons: list[str] | None
    opportunity_diagnostics: dict[str, Any] | None
    timeout_candles: int
    wait_candles: int = 0


@dataclass
class _PendingMarketEntry:
    event: CandleEvent
    score: SignalScore
    action: TradeAction
    strategy_reason: str
    evaluation_metadata: dict[str, Any]
    governor_decision: Any
    planned_exit: dict[str, Any]
    sizing_decision: SmartSizingDecision
    planned_quantity: Decimal | None
    smart_execution_decision: SmartExecutionDecision
    cycle_candidate_count: int
    opportunity_rank: int | None
    opportunity_score: int | None
    opportunity_reasons: list[str] | None
    opportunity_diagnostics: dict[str, Any] | None


class GovernorSimulator:
    def __init__(
        self,
        *,
        strategy_runtime: BacktestStrategyRuntime,
        portfolio: PortfolioSimulator,
        execution: ExecutionSimulator,
        strategy_config: BacktestStrategyConfig,
        symbol_configs: list[BacktestSymbolConfig],
        initial_balance: Decimal,
        rank_cycle_candidates: bool = True,
        trend_context_enabled: bool = True,
        trend_context_mode: str = "OBSERVE",
        trend_context_engine: TrendContextEngine | None = None,
        trend_timeframe: str = "",
        operational_module_modes: dict[str, str] | None = None,
        smart_sizing_settings: SmartSizingSettings | None = None,
        smart_execution_settings: SmartExecutionSettings | None = None,
        signal_timing_policy: str = "ON_CLOSE",
        market_entry_policy: str = "NEXT_OPEN",
        limit_activation_policy: str = "NEXT_CANDLE",
        end_of_data_policy: str = "CLOSE_AT_LAST_PRICE",
        limit_entry_timeout_candles: int = 1,
        min_seconds_between_entries: float = DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES,
        max_daily_trades: int = 0,
    ):
        self.strategy_runtime = strategy_runtime
        self.portfolio = portfolio
        self.execution = execution
        self.strategy_config = strategy_config
        self.rank_cycle_candidates = bool(rank_cycle_candidates)
        self.symbol_configs = {item.symbol: item for item in symbol_configs}
        self.initial_balance = initial_balance
        self.trend_context_enabled = bool(trend_context_enabled)
        self.trend_context_mode = normalize_regime_policy_mode(trend_context_mode)
        self.trend_context_engine = trend_context_engine or TrendContextEngine()
        self.strategy_price_window = max(
            300,
            self.strategy_runtime.required_history,
            self.trend_context_engine.required_price_window(),
        )
        self.prices: dict[str, list[Decimal]] = {item.symbol: [] for item in symbol_configs}
        self.decisions: list[BacktestDecisionRecord] = []
        self.rejected_signals: list[BacktestRejectedSignal] = []
        self.governor_decisions: list[Any] = []
        self.opportunity_ranker = OpportunityRanker()
        mode_resolution = module_modes_from_mapping(operational_module_modes or {})
        self.operational_module_modes = mode_resolution.modes
        self.smart_sizing_settings = smart_sizing_settings or SmartSizingSettings()
        self.smart_execution_settings = smart_execution_settings or SmartExecutionSettings()
        self.signal_timing_policy = str(signal_timing_policy or "ON_CLOSE").upper().strip()
        self.market_entry_policy = str(market_entry_policy or "NEXT_OPEN").upper().strip()
        self.limit_activation_policy = str(limit_activation_policy or "NEXT_CANDLE").upper().strip()
        self.end_of_data_policy = str(end_of_data_policy or "CLOSE_AT_LAST_PRICE").upper().strip()
        self.limit_entry_timeout_candles = max(0, int(limit_entry_timeout_candles))
        self.smart_sizing_engine = SmartSizingEngine(settings=self.smart_sizing_settings)
        self.smart_execution_engine = SmartExecutionEngine(settings=self.smart_execution_settings)
        self.decision_stages = HistoricalDecisionStages(
            execution=execution,
            portfolio=portfolio,
            symbol_configs=self.symbol_configs,
            module_modes=self.operational_module_modes,
            sizing_engine=self.smart_sizing_engine,
            execution_engine=self.smart_execution_engine,
            signal_timing_policy=self.signal_timing_policy,
            strategy_name=(f"{strategy_runtime.module_name}.{strategy_runtime.class_name}"),
            min_seconds_between_entries=min_seconds_between_entries,
            max_daily_trades=max_daily_trades,
            market_entry_policy=self.market_entry_policy,
            limit_activation_policy=self.limit_activation_policy,
            limit_entry_timeout_candles=self.limit_entry_timeout_candles,
        )
        self.trading_policy = self.decision_stages.trading_policy
        self.opportunities = self.decision_stages.opportunities
        self.signal_port = HistoricalSignalPort(
            runtime=strategy_runtime,
            execution=execution,
            clock=self.decision_stages.clock,
            trend_engine=self.trend_context_engine,
            trend_enabled=self.trend_context_enabled,
            trend_mode=self.trend_context_mode,
            price_window=self.strategy_price_window,
            trend_timeframe=trend_timeframe,
        )
        self.planned_exit_port = HistoricalPlannedExitPort(
            self.symbol_configs, price_for_event=self.decision_stages.clock.price
        )
        self.pending_limit_entries: dict[str, _PendingLimitEntry] = {}
        self.pending_market_entries: dict[str, _PendingMarketEntry] = {}
        self.last_event_by_symbol: dict[str, CandleEvent] = {}
        self.closed_intrabar_symbols: set[str] = set()
        self.opened_at_close_symbols: set[str] = set()

    def start_cycle(self) -> None:
        self.portfolio.start_cycle()
        self.closed_intrabar_symbols.clear()
        self.opened_at_close_symbols.clear()

    def process_candle_open(self, event: CandleEvent) -> None:
        self.last_event_by_symbol[event.symbol] = event
        self._process_pending_market_entry(event)
        result = self.execution.maybe_close_at_open(
            symbol=event.symbol,
            candle=event.candle,
        )
        self._record_automatic_exit(event, result)

    def process_intrabar_exit(self, event: CandleEvent) -> None:
        self.last_event_by_symbol[event.symbol] = event
        had_position = self.execution.current_position(event.symbol).is_open
        processed_limit = self._process_pending_limit_entry(event)
        if event.symbol in self.opened_at_close_symbols:
            return

        if (
            processed_limit
            and not had_position
            and self.execution.current_position(event.symbol).is_open
        ):
            result = self.execution.maybe_close_after_limit_fill(
                symbol=event.symbol,
                candle=event.candle,
            )
            self._record_automatic_exit(event, result)
            return

        result = self.execution.maybe_close_by_stop_or_take(
            symbol=event.symbol,
            candle=event.candle,
            include_open_gap=False,
        )
        self._record_automatic_exit(event, result)

    def _record_automatic_exit(self, event: CandleEvent, result) -> None:
        if result is None or result.trade is None:
            return
        self.closed_intrabar_symbols.add(event.symbol)

        self.decisions.append(
            BacktestDecisionRecord(
                timestamp=event.timestamp,
                symbol=event.symbol,
                price=result.trade.exit_price,
                strategy_mode=self.strategy_config.normalized_mode,
                score=None,
                priority=None,
                score_action=None,
                decision_action=TradeAction.CLOSE_POSITION.value,
                final_action=TradeAction.CLOSE_POSITION.value,
                final_status="EXECUTED",
                reason=result.reason,
            )
        )

    def finish_cycle(self, events: list[CandleEvent]) -> None:
        if self.signal_timing_policy != "ON_OPEN":
            return
        for event in events:
            prices = self.prices.setdefault(event.symbol, [])
            prices.append(Decimal(str(event.candle.close)))
            if len(prices) > self.strategy_price_window:
                del prices[: -self.strategy_price_window]

    def ingest_warmup(self, events: list[CandleEvent]) -> None:
        for event in events:
            prices = self.prices.setdefault(event.symbol, [])
            prices.append(Decimal(str(event.candle.close)))
            if len(prices) > self.strategy_price_window:
                del prices[: -self.strategy_price_window]
            self.last_event_by_symbol[event.symbol] = event

    def flush_pending_entries(self) -> None:
        for symbol, pending_market in list(self.pending_market_entries.items()):
            event = self.last_event_by_symbol.get(symbol, pending_market.event)
            self.pending_market_entries.pop(symbol, None)
            self._record_pending_market_expired(pending_market, event=event)
        for symbol, pending_limit in list(self.pending_limit_entries.items()):
            event = self.last_event_by_symbol.get(symbol, pending_limit.event)
            self._expire_pending_limit_entry(
                pending_limit,
                event=event,
                reason="SMART_EXECUTION_LIMIT_EXPIRED",
            )

    def finalize_end_of_data(self) -> None:
        for result in self.execution.close_open_positions_at_end(self.last_event_by_symbol):
            if result.trade is None:
                continue
            self.decisions.append(
                BacktestDecisionRecord(
                    timestamp=result.trade.exit_time,
                    symbol=result.trade.symbol,
                    price=result.trade.exit_price,
                    strategy_mode=self.strategy_config.normalized_mode,
                    score=None,
                    priority=None,
                    score_action=None,
                    decision_action=TradeAction.CLOSE_POSITION.value,
                    final_action=TradeAction.CLOSE_POSITION.value,
                    final_status="EXECUTED",
                    reason=result.reason,
                )
            )

    def process_signal(self, event: CandleEvent) -> None:
        self.process_signal_cycle([event])

    def process_signal_cycle(self, events: list[CandleEvent]) -> None:
        candidates = []
        for event in events:
            signal = self.signal_port.evaluate(
                event,
                prices_by_symbol=self.prices,
                closed_intrabar=event.symbol in self.closed_intrabar_symbols,
            )
            candidates.append(
                _SignalCandidate(
                    event=event,
                    score=signal.score,
                    action=signal.action,
                    reason=signal.reason,
                    evaluation_metadata=signal.metadata,
                )
            )
        open_candidates = [
            candidate
            for candidate in candidates
            if candidate.action in {TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT}
        ]
        close_candidates = [
            candidate for candidate in candidates if candidate.action == TradeAction.CLOSE_POSITION
        ]
        hold_candidates = [
            candidate for candidate in candidates if candidate.action == TradeAction.HOLD
        ]
        open_candidate_count = len(open_candidates)

        for candidate in hold_candidates:
            self._record_decision(
                event=candidate.event,
                score=candidate.score,
                decision_action=candidate.action.value,
                final_action=TradeAction.HOLD.value,
                final_status="HOLD",
                reason=candidate.reason,
                evaluation_metadata=candidate.evaluation_metadata,
            )

        for candidate in close_candidates:
            self._process_close(
                candidate.event,
                candidate.score,
                candidate.reason,
                evaluation_metadata=candidate.evaluation_metadata,
            )

        for candidate in self._rank_open_candidates(open_candidates):
            self._process_open(
                candidate.event,
                candidate.score,
                candidate.action,
                candidate.reason,
                evaluation_metadata=candidate.evaluation_metadata,
                cycle_candidate_count=open_candidate_count,
                opportunity_rank=candidate.opportunity_rank,
                opportunity_score=(
                    candidate.opportunity.opportunity_score
                    if candidate.opportunity is not None
                    else None
                ),
                opportunity_reasons=(
                    candidate.opportunity.reasons if candidate.opportunity is not None else []
                ),
                opportunity_diagnostics=(
                    {
                        **candidate.opportunity.diagnostics,
                        "tie_breaker": candidate.tie_breaker,
                    }
                    if candidate.opportunity is not None
                    else {"tie_breaker": candidate.tie_breaker}
                ),
            )

    def _rank_open_candidates(
        self,
        candidates: list[_SignalCandidate],
    ) -> list[_SignalCandidate]:
        if not candidates:
            return []

        open_symbols = {item["symbol"] for item in self.execution.open_positions_snapshot()}
        exposed_groups = {
            resolve_correlation_group(symbol, self.portfolio.settings)[0] for symbol in open_symbols
        }
        ranked: list[_SignalCandidate] = []
        for candidate in candidates:
            symbol = candidate.event.symbol
            group = resolve_correlation_group(symbol, self.portfolio.settings)[0]
            opportunity = self.opportunity_ranker.score(
                symbol=symbol,
                base_score=candidate.score.score,
                priority=candidate.score.priority or SignalPriority.COLD,
                fast_lane=False,
                position_open=symbol in open_symbols,
                group_has_exposure=group in exposed_groups,
            )
            candidate.opportunity = opportunity
            candidate.tie_breaker = _stable_tie_breaker(
                candidate.event.timestamp,
                symbol,
            )
            ranked.append(candidate)

        if not self.rank_cycle_candidates:
            for index, candidate in enumerate(ranked, start=1):
                candidate.opportunity_rank = index
            return ranked

        ranked = sorted(
            ranked,
            key=lambda candidate: (
                candidate.opportunity.opportunity_score if candidate.opportunity is not None else 0,
                int(candidate.score.score),
                candidate.tie_breaker,
            ),
            reverse=True,
        )
        for index, candidate in enumerate(ranked, start=1):
            candidate.opportunity_rank = index
        return ranked

    def _process_open(
        self,
        event: CandleEvent,
        score: SignalScore,
        action: TradeAction,
        reason: str,
        evaluation_metadata: dict[str, Any],
        cycle_candidate_count: int = 0,
        opportunity_rank: int | None = None,
        opportunity_score: int | None = None,
        opportunity_reasons: list[str] | None = None,
        opportunity_diagnostics: dict[str, Any] | None = None,
    ) -> None:
        current_position = self.execution.current_position(event.symbol)
        if current_position.is_open:
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="GOVERNOR",
                reason="POSITION_ALREADY_OPEN",
                strategy_reason=reason,
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="BLOCKED",
                reason=reason,
                block_layer="GOVERNOR",
                block_reason="POSITION_ALREADY_OPEN",
                evaluation_metadata=evaluation_metadata,
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return
        if (
            event.symbol in self.pending_limit_entries
            or event.symbol in self.pending_market_entries
        ):
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="SMART_EXECUTION",
                reason="ENTRY_ORDER_PENDING",
                strategy_reason=reason,
                checks={
                    "pending_limit_entry": event.symbol in self.pending_limit_entries,
                    "pending_market_entry": event.symbol in self.pending_market_entries,
                },
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="BLOCKED",
                reason=reason,
                block_layer="SMART_EXECUTION",
                block_reason="ENTRY_ORDER_PENDING",
                evaluation_metadata=evaluation_metadata,
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return

        cadence_block = self.decision_stages.account.cadence_block(event)
        if cadence_block is not None:
            cadence_reason, cadence_checks = cadence_block
            self.decision_stages.opportunity_for_open(
                event=event, score=score, action=action, reason=reason
            )
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="CADENCE_POLICY",
                reason=cadence_reason,
                strategy_reason=reason,
                checks=cadence_checks,
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="BLOCKED",
                reason=reason,
                block_layer="CADENCE_POLICY",
                block_reason=cadence_reason,
                evaluation_metadata=evaluation_metadata,
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return

        planned_exit = self.planned_exit_port.resolve(
            score=score,
            action=action,
            event=event,
        )
        decision_session = self.decision_stages.for_open(
            event=event,
            score=score,
            action=action,
            reason=reason,
            planned_exit=planned_exit,
        )
        sizing_decision = decision_session.size()
        if sizing_decision.blocked or decision_session.sizing_block_reason is not None:
            sizing_block_reason = decision_session.sizing_block_reason or "SMART_SIZING_BLOCKED"
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="SMART_SIZING",
                reason=sizing_block_reason,
                strategy_reason=reason,
                checks=sizing_decision.to_dict(),
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="BLOCKED",
                reason=reason,
                block_layer="SMART_SIZING",
                block_reason=sizing_block_reason,
                evaluation_metadata={
                    **evaluation_metadata,
                    **_planned_exit_decision_metadata(planned_exit),
                    "smart_sizing": sizing_decision.to_dict(),
                    "smart_sizing_reason": sizing_decision.reason,
                },
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return

        governor_decision = self.decision_stages.evaluate_intent(
            event=event,
            score=score,
            action=action.value,
            reason=reason,
            override_notional=decision_session.approved_notional(sizing_decision),
        )
        self.governor_decisions.append(governor_decision)
        if not governor_decision.allowed:
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="RISK",
                reason=governor_decision.reason,
                strategy_reason=reason,
                checks=governor_decision.checks,
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="BLOCKED",
                reason=reason,
                governor_allowed=False,
                governor_reason=governor_decision.reason,
                block_layer="RISK",
                block_reason=governor_decision.reason,
                governor_checks=governor_decision.checks,
                evaluation_metadata={
                    **evaluation_metadata,
                    **_planned_exit_decision_metadata(planned_exit),
                    "smart_sizing": sizing_decision.to_dict(),
                    "risk_evaluated_after_smart_sizing": True,
                },
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return

        smart_execution_decision = decision_session.choose_execution(sizing_decision)
        if smart_execution_decision.blocked:
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="SMART_EXECUTION",
                reason=smart_execution_decision.reason,
                strategy_reason=reason,
                checks=smart_execution_decision.to_dict(),
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="BLOCKED",
                reason=reason,
                governor_allowed=True,
                governor_reason=governor_decision.reason,
                block_layer="SMART_EXECUTION",
                block_reason=smart_execution_decision.reason,
                governor_checks=governor_decision.checks,
                evaluation_metadata={
                    **evaluation_metadata,
                    **_planned_exit_decision_metadata(planned_exit),
                    "smart_sizing": sizing_decision.to_dict(),
                    "smart_execution": smart_execution_decision.to_dict(),
                    "smart_execution_reason": smart_execution_decision.reason,
                },
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return

        if smart_execution_decision.entry_order_type == "LIMIT":
            self._handle_limit_entry(
                event=event,
                score=score,
                action=action,
                reason=reason,
                evaluation_metadata=evaluation_metadata,
                governor_decision=governor_decision,
                planned_exit=planned_exit,
                sizing_decision=sizing_decision,
                planned_quantity=decision_session.evaluation.planned_quantity,
                smart_execution_decision=smart_execution_decision,
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return

        market_decision = _execution_decision_with_limit_status(
            smart_execution_decision,
            status="MARKET_SELECTED",
            wait_candles=0,
            fallback_to_market_used=False,
        )
        if self.market_entry_policy == "NEXT_OPEN":
            self._queue_market_entry(
                event=event,
                score=score,
                action=action,
                reason=reason,
                evaluation_metadata=evaluation_metadata,
                governor_decision=governor_decision,
                planned_exit=planned_exit,
                sizing_decision=sizing_decision,
                planned_quantity=decision_session.evaluation.planned_quantity,
                smart_execution_decision=market_decision,
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return
        self._execute_open_position(
            event=event,
            score=score,
            action=action,
            reason=reason,
            evaluation_metadata=evaluation_metadata,
            governor_decision=governor_decision,
            planned_exit=planned_exit,
            sizing_decision=sizing_decision,
            planned_quantity=decision_session.evaluation.planned_quantity,
            smart_execution_decision=market_decision,
            entry_price=_signal_price(event, self.signal_timing_policy),
            cycle_candidate_count=cycle_candidate_count,
            opportunity_rank=opportunity_rank,
            opportunity_score=opportunity_score,
            opportunity_reasons=opportunity_reasons,
            opportunity_diagnostics=opportunity_diagnostics,
        )

    def _queue_market_entry(
        self,
        *,
        event: CandleEvent,
        score: SignalScore,
        action: TradeAction,
        reason: str,
        evaluation_metadata: dict[str, Any],
        governor_decision,
        planned_exit: dict[str, Any],
        sizing_decision: SmartSizingDecision,
        planned_quantity: Decimal | None,
        smart_execution_decision: SmartExecutionDecision,
        cycle_candidate_count: int,
        opportunity_rank: int | None,
        opportunity_score: int | None,
        opportunity_reasons: list[str] | None,
        opportunity_diagnostics: dict[str, Any] | None,
    ) -> None:
        self.pending_market_entries[event.symbol] = _PendingMarketEntry(
            event=event,
            score=score,
            action=action,
            strategy_reason=reason,
            evaluation_metadata=evaluation_metadata,
            governor_decision=governor_decision,
            planned_exit=planned_exit,
            sizing_decision=sizing_decision,
            planned_quantity=planned_quantity,
            smart_execution_decision=smart_execution_decision,
            cycle_candidate_count=cycle_candidate_count,
            opportunity_rank=opportunity_rank,
            opportunity_score=opportunity_score,
            opportunity_reasons=opportunity_reasons,
            opportunity_diagnostics=opportunity_diagnostics,
        )
        self._record_decision(
            event=event,
            score=score,
            decision_action=action.value,
            final_action=TradeAction.HOLD.value,
            final_status="PENDING",
            reason=reason,
            governor_allowed=True,
            governor_reason=governor_decision.reason,
            governor_checks=governor_decision.checks,
            evaluation_metadata={
                **evaluation_metadata,
                **_planned_exit_decision_metadata(planned_exit),
                "smart_sizing": sizing_decision.to_dict(),
                "smart_execution": smart_execution_decision.to_dict(),
                "market_entry_policy": self.market_entry_policy,
            },
            cycle_candidate_count=cycle_candidate_count,
            opportunity_rank=opportunity_rank,
            opportunity_score=opportunity_score,
            opportunity_reasons=opportunity_reasons,
            opportunity_diagnostics=opportunity_diagnostics,
        )

    def _handle_limit_entry(
        self,
        *,
        event: CandleEvent,
        score: SignalScore,
        action: TradeAction,
        reason: str,
        evaluation_metadata: dict[str, Any],
        governor_decision,
        planned_exit: dict[str, Any],
        sizing_decision: SmartSizingDecision,
        planned_quantity: Decimal | None,
        smart_execution_decision: SmartExecutionDecision,
        cycle_candidate_count: int,
        opportunity_rank: int | None,
        opportunity_score: int | None,
        opportunity_reasons: list[str] | None,
        opportunity_diagnostics: dict[str, Any] | None,
    ) -> None:
        limit_price = smart_execution_decision.limit_price
        if limit_price is None or limit_price <= 0:
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="SMART_EXECUTION",
                reason="SMART_EXECUTION_LIMIT_PRICE_UNAVAILABLE",
                strategy_reason=reason,
                checks=smart_execution_decision.to_dict(),
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="REJECTED",
                reason=reason,
                governor_allowed=True,
                governor_reason=governor_decision.reason,
                block_layer="SMART_EXECUTION",
                block_reason="SMART_EXECUTION_LIMIT_PRICE_UNAVAILABLE",
                governor_checks=governor_decision.checks,
                evaluation_metadata={
                    **evaluation_metadata,
                    **_planned_exit_decision_metadata(planned_exit),
                    "smart_sizing": sizing_decision.to_dict(),
                    "smart_execution": smart_execution_decision.to_dict(),
                },
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return

        pending = _PendingLimitEntry(
            event=event,
            score=score,
            action=action,
            strategy_reason=reason,
            evaluation_metadata=evaluation_metadata,
            governor_decision=governor_decision,
            planned_exit=planned_exit,
            sizing_decision=sizing_decision,
            planned_quantity=planned_quantity,
            smart_execution_decision=smart_execution_decision,
            cycle_candidate_count=cycle_candidate_count,
            opportunity_rank=opportunity_rank,
            opportunity_score=opportunity_score,
            opportunity_reasons=opportunity_reasons,
            opportunity_diagnostics=opportunity_diagnostics,
            timeout_candles=self.limit_entry_timeout_candles,
        )
        if pending.timeout_candles <= 0:
            expired_decision = _execution_decision_with_limit_status(
                smart_execution_decision,
                status="LIMIT_EXPIRED",
                wait_candles=0,
                fallback_to_market_used=False,
            )
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="SMART_EXECUTION",
                reason="SMART_EXECUTION_LIMIT_EXPIRED",
                strategy_reason=reason,
                checks=expired_decision.to_dict(),
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="REJECTED",
                reason=reason,
                governor_allowed=True,
                governor_reason=governor_decision.reason,
                block_layer="SMART_EXECUTION",
                block_reason="SMART_EXECUTION_LIMIT_EXPIRED",
                governor_checks=governor_decision.checks,
                evaluation_metadata={
                    **evaluation_metadata,
                    **_planned_exit_decision_metadata(planned_exit),
                    "smart_sizing": sizing_decision.to_dict(),
                    "smart_execution": expired_decision.to_dict(),
                },
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return

        self.pending_limit_entries[event.symbol] = pending
        pending_decision = _execution_decision_with_limit_status(
            smart_execution_decision,
            status="LIMIT_PENDING",
            wait_candles=0,
            fallback_to_market_used=False,
        )
        self._record_decision(
            event=event,
            score=score,
            decision_action=action.value,
            final_action=TradeAction.HOLD.value,
            final_status="PENDING",
            reason=reason,
            governor_allowed=True,
            governor_reason=governor_decision.reason,
            governor_checks=governor_decision.checks,
            evaluation_metadata={
                **evaluation_metadata,
                **_planned_exit_decision_metadata(planned_exit),
                "smart_sizing": sizing_decision.to_dict(),
                "smart_execution": pending_decision.to_dict(),
            },
            cycle_candidate_count=cycle_candidate_count,
            opportunity_rank=opportunity_rank,
            opportunity_score=opportunity_score,
            opportunity_reasons=opportunity_reasons,
            opportunity_diagnostics=opportunity_diagnostics,
        )

    def _process_pending_market_entry(self, event: CandleEvent) -> bool:
        pending = self.pending_market_entries.get(event.symbol)
        if pending is None:
            return False

        self.pending_market_entries.pop(event.symbol, None)
        self._execute_open_position(
            event=event,
            score=pending.score,
            action=pending.action,
            reason=pending.strategy_reason,
            evaluation_metadata=pending.evaluation_metadata,
            governor_decision=pending.governor_decision,
            planned_exit=pending.planned_exit,
            sizing_decision=pending.sizing_decision,
            planned_quantity=pending.planned_quantity,
            smart_execution_decision=pending.smart_execution_decision,
            entry_price=event.candle.open,
            entry_timestamp=_candle_open_timestamp(event),
            cycle_candidate_count=pending.cycle_candidate_count,
            opportunity_rank=pending.opportunity_rank,
            opportunity_score=pending.opportunity_score,
            opportunity_reasons=pending.opportunity_reasons,
            opportunity_diagnostics=pending.opportunity_diagnostics,
        )
        return True

    def _record_pending_market_expired(
        self,
        pending: _PendingMarketEntry,
        *,
        event: CandleEvent,
    ) -> None:
        self._reject(
            event=event,
            score=pending.score,
            action=pending.action.value,
            layer="EXECUTION",
            reason="MARKET_ENTRY_NEXT_OPEN_UNAVAILABLE",
            strategy_reason=pending.strategy_reason,
            checks={"market_entry_policy": self.market_entry_policy},
        )
        self._record_decision(
            event=event,
            score=pending.score,
            decision_action=pending.action.value,
            final_action=TradeAction.HOLD.value,
            final_status="REJECTED",
            reason=pending.strategy_reason,
            governor_allowed=True,
            governor_reason=pending.governor_decision.reason,
            block_layer="EXECUTION",
            block_reason="MARKET_ENTRY_NEXT_OPEN_UNAVAILABLE",
            governor_checks=pending.governor_decision.checks,
            evaluation_metadata={
                **pending.evaluation_metadata,
                **_planned_exit_decision_metadata(pending.planned_exit),
                "smart_sizing": pending.sizing_decision.to_dict(),
                "smart_execution": pending.smart_execution_decision.to_dict(),
                "market_entry_policy": self.market_entry_policy,
            },
            cycle_candidate_count=pending.cycle_candidate_count,
            opportunity_rank=pending.opportunity_rank,
            opportunity_score=pending.opportunity_score,
            opportunity_reasons=pending.opportunity_reasons,
            opportunity_diagnostics=pending.opportunity_diagnostics,
        )

    def _process_pending_limit_entry(self, event: CandleEvent) -> bool:
        pending = self.pending_limit_entries.get(event.symbol)
        if pending is None:
            return False
        if event.candle.open_time_ms <= pending.event.candle.open_time_ms:
            return False

        pending.wait_candles += 1
        limit_price = pending.smart_execution_decision.limit_price
        symbol_rules = self.symbol_configs[event.symbol].rules
        if limit_price is not None and _limit_touched(
            pending.action,
            event.candle,
            limit_price,
            require_trade_through=self.execution.fill_settings.require_limit_trade_through,
            price_tick=symbol_rules.price_tick,
        ):
            self.pending_limit_entries.pop(event.symbol, None)
            filled_decision = _execution_decision_with_limit_status(
                pending.smart_execution_decision,
                status="LIMIT_FILLED",
                wait_candles=pending.wait_candles,
                fallback_to_market_used=False,
            )
            self._execute_open_position(
                event=event,
                score=pending.score,
                action=pending.action,
                reason=pending.strategy_reason,
                evaluation_metadata=pending.evaluation_metadata,
                governor_decision=pending.governor_decision,
                planned_exit=pending.planned_exit,
                sizing_decision=pending.sizing_decision,
                planned_quantity=pending.planned_quantity,
                smart_execution_decision=filled_decision,
                entry_price=limit_price,
                entry_timestamp=_candle_open_timestamp(event),
                cycle_candidate_count=pending.cycle_candidate_count,
                opportunity_rank=pending.opportunity_rank,
                opportunity_score=pending.opportunity_score,
                opportunity_reasons=pending.opportunity_reasons,
                opportunity_diagnostics=pending.opportunity_diagnostics,
            )
            return True

        if pending.wait_candles >= pending.timeout_candles:
            self._expire_pending_limit_entry(
                pending,
                event=event,
                reason="SMART_EXECUTION_LIMIT_EXPIRED",
            )
            return True
        return False

    def _expire_pending_limit_entry(
        self,
        pending: _PendingLimitEntry,
        *,
        event: CandleEvent,
        reason: str,
    ) -> None:
        self.pending_limit_entries.pop(pending.event.symbol, None)
        if pending.smart_execution_decision.fallback_allowed:
            fallback_decision = _execution_decision_with_limit_status(
                pending.smart_execution_decision,
                status="LIMIT_FALLBACK_TO_MARKET",
                wait_candles=pending.wait_candles,
                fallback_to_market_used=True,
                entry_order_type="MARKET",
            )
            self._execute_open_position(
                event=event,
                score=pending.score,
                action=pending.action,
                reason=pending.strategy_reason,
                evaluation_metadata=pending.evaluation_metadata,
                governor_decision=pending.governor_decision,
                planned_exit=pending.planned_exit,
                sizing_decision=pending.sizing_decision,
                planned_quantity=pending.planned_quantity,
                smart_execution_decision=fallback_decision,
                entry_price=_market_fallback_price(event, self.market_entry_policy),
                entry_timestamp=_candle_close_timestamp(event),
                cycle_candidate_count=pending.cycle_candidate_count,
                opportunity_rank=pending.opportunity_rank,
                opportunity_score=pending.opportunity_score,
                opportunity_reasons=pending.opportunity_reasons,
                opportunity_diagnostics=pending.opportunity_diagnostics,
            )
            if self.execution.current_position(event.symbol).is_open:
                self.opened_at_close_symbols.add(event.symbol)
            return

        expired_decision = _execution_decision_with_limit_status(
            pending.smart_execution_decision,
            status="LIMIT_EXPIRED",
            wait_candles=pending.wait_candles,
            fallback_to_market_used=False,
        )
        self._reject(
            event=event,
            score=pending.score,
            action=pending.action.value,
            layer="SMART_EXECUTION",
            reason=reason,
            strategy_reason=pending.strategy_reason,
            checks=expired_decision.to_dict(),
        )
        self._record_decision(
            event=event,
            score=pending.score,
            decision_action=pending.action.value,
            final_action=TradeAction.HOLD.value,
            final_status="REJECTED",
            reason=pending.strategy_reason,
            governor_allowed=True,
            governor_reason=pending.governor_decision.reason,
            block_layer="SMART_EXECUTION",
            block_reason=reason,
            governor_checks=pending.governor_decision.checks,
            evaluation_metadata={
                **pending.evaluation_metadata,
                **_planned_exit_decision_metadata(pending.planned_exit),
                "smart_sizing": pending.sizing_decision.to_dict(),
                "smart_execution": expired_decision.to_dict(),
            },
            cycle_candidate_count=pending.cycle_candidate_count,
            opportunity_rank=pending.opportunity_rank,
            opportunity_score=pending.opportunity_score,
            opportunity_reasons=pending.opportunity_reasons,
            opportunity_diagnostics=pending.opportunity_diagnostics,
        )

    def _execute_open_position(
        self,
        *,
        event: CandleEvent,
        score: SignalScore,
        action: TradeAction,
        reason: str,
        evaluation_metadata: dict[str, Any],
        governor_decision,
        planned_exit: dict[str, Any],
        sizing_decision: SmartSizingDecision,
        planned_quantity: Decimal | None,
        smart_execution_decision: SmartExecutionDecision,
        entry_price: Decimal,
        entry_timestamp: str | None = None,
        cycle_candidate_count: int,
        opportunity_rank: int | None,
        opportunity_score: int | None,
        opportunity_reasons: list[str] | None,
        opportunity_diagnostics: dict[str, Any] | None,
    ) -> None:
        quantity = planned_quantity if sizing_decision.affected_result else None
        limit_status = _limit_status_from_decision(smart_execution_decision)
        available_volume = Decimal(str(event.candle.volume))
        if limit_status["limit_fill_status"] == "LIMIT_FILLED":
            available_volume *= self.execution.fill_settings.intrabar_limit_volume_fraction
        try:
            execution_result = self.execution.open_position(
                symbol=event.symbol,
                action=action,
                timestamp=entry_timestamp or event.timestamp,
                price=entry_price,
                reason=reason,
                quantity=quantity,
                stop_price=(
                    planned_exit["stop_price"] if planned_exit["dynamic_exit_used"] else None
                ),
                take_profit_price=(
                    planned_exit["take_profit_price"] if planned_exit["dynamic_exit_used"] else None
                ),
                dynamic_exit_used=planned_exit["dynamic_exit_used"],
                exit_source=planned_exit["exit_source"],
                recommended_stop_price=planned_exit["recommended_stop_price"],
                recommended_take_profit_price=planned_exit["recommended_take_profit_price"],
                reward_risk_liquido=planned_exit["reward_risk_liquido"],
                raw_reward_risk=planned_exit["raw_reward_risk"],
                cost_to_reward_ratio=planned_exit["cost_to_reward_ratio"],
                risk_pct=planned_exit["risk_pct"],
                reward_pct=planned_exit["reward_pct"],
                planned_exit_rejection_reason=planned_exit["planned_exit_rejection_reason"],
                planned_exit_diagnostics=planned_exit["diagnostics"],
                smart_sizing_mode=sizing_decision.mode.value,
                smart_sizing_used=sizing_decision.smart_sizing_used,
                smart_sizing_affected_result=sizing_decision.affected_result,
                base_notional_usdt=sizing_decision.base_notional_usdt,
                final_notional_usdt=sizing_decision.final_notional_usdt,
                sizing_multiplier=sizing_decision.sizing_multiplier,
                sizing_reason=sizing_decision.reason,
                sizing_diagnostics=sizing_decision.to_dict(),
                smart_execution_mode=smart_execution_decision.mode.value,
                smart_execution_used=smart_execution_decision.smart_execution_used,
                smart_execution_affected_result=(smart_execution_decision.affected_result),
                entry_order_type=smart_execution_decision.entry_order_type,
                limit_price=smart_execution_decision.limit_price,
                limit_fill_status=limit_status["limit_fill_status"],
                limit_wait_candles=limit_status["limit_wait_candles"],
                fallback_to_market_used=limit_status["fallback_to_market_used"],
                smart_execution_reason=smart_execution_decision.reason,
                smart_execution_diagnostics=smart_execution_decision.to_dict(),
                candle_volume=available_volume,
            )
        except BacktestLiquidityError as error:
            liquidity_reason = str(error).split(":", 1)[0]
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="LIQUIDITY",
                reason=liquidity_reason,
                strategy_reason=reason,
                checks={"error": str(error)},
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="REJECTED",
                reason=reason,
                governor_allowed=True,
                governor_reason=governor_decision.reason,
                block_layer="LIQUIDITY",
                block_reason=liquidity_reason,
                governor_checks=governor_decision.checks,
                evaluation_metadata=evaluation_metadata,
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return
        except TradeNotExecutableError as error:
            self._reject(
                event=event,
                score=score,
                action=action.value,
                layer="EXCHANGE_RULE",
                reason=error.reason,
                strategy_reason=reason,
                checks=_quantity_checks(error),
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=action.value,
                final_action=TradeAction.HOLD.value,
                final_status="REJECTED",
                reason=reason,
                governor_allowed=True,
                governor_reason=governor_decision.reason,
                block_layer="EXCHANGE_RULE",
                block_reason=error.reason,
                governor_checks=governor_decision.checks,
                evaluation_metadata={
                    **evaluation_metadata,
                    **_planned_exit_decision_metadata(planned_exit),
                    "smart_sizing": sizing_decision.to_dict(),
                    "smart_execution": smart_execution_decision.to_dict(),
                },
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=opportunity_reasons,
                opportunity_diagnostics=opportunity_diagnostics,
            )
            return

        self.portfolio.record_new_position()
        position = execution_result.position
        self._record_decision(
            event=event,
            score=score,
            decision_action=action.value,
            final_action=action.value,
            final_status="EXECUTED",
            reason=reason,
            governor_allowed=True,
            governor_reason=governor_decision.reason,
            governor_checks=governor_decision.checks,
            evaluation_metadata={
                **evaluation_metadata,
                **_planned_exit_decision_metadata(planned_exit, position=position),
                "smart_sizing": sizing_decision.to_dict(),
                "smart_execution": smart_execution_decision.to_dict(),
            },
            cycle_candidate_count=cycle_candidate_count,
            opportunity_rank=opportunity_rank,
            opportunity_score=opportunity_score,
            opportunity_reasons=opportunity_reasons,
            opportunity_diagnostics=opportunity_diagnostics,
        )

    def _process_close(
        self,
        event: CandleEvent,
        score: SignalScore,
        reason: str,
        evaluation_metadata: dict[str, Any],
    ) -> None:
        current_position = self.execution.current_position(event.symbol)
        if not current_position.is_open:
            self._reject(
                event=event,
                score=score,
                action=TradeAction.CLOSE_POSITION.value,
                layer="GOVERNOR",
                reason="NO_OPEN_POSITION",
                strategy_reason=reason,
            )
            self._record_decision(
                event=event,
                score=score,
                decision_action=TradeAction.CLOSE_POSITION.value,
                final_action=TradeAction.HOLD.value,
                final_status="IGNORED",
                reason=reason,
                block_layer="GOVERNOR",
                block_reason="NO_OPEN_POSITION",
                evaluation_metadata=evaluation_metadata,
            )
            return

        governor_decision = self.decision_stages.evaluate_intent(
            event=event,
            score=score,
            action=TradeAction.CLOSE_POSITION.value,
            reason=reason,
        )
        self.governor_decisions.append(governor_decision)
        result = self.execution.close_position(
            symbol=event.symbol,
            timestamp=event.timestamp,
            price=_signal_price(event, self.signal_timing_policy),
            exit_reason="strategy_close",
            strategy_reason_exit=reason,
            candle_volume=Decimal(str(event.candle.volume)),
        )
        self._record_decision(
            event=event,
            score=score,
            decision_action=TradeAction.CLOSE_POSITION.value,
            final_action=TradeAction.CLOSE_POSITION.value,
            final_status="EXECUTED" if result.trade is not None else "IGNORED",
            reason=reason,
            governor_allowed=governor_decision.allowed,
            governor_reason=governor_decision.reason,
            governor_checks=governor_decision.checks,
            evaluation_metadata=evaluation_metadata,
        )

    def _reject(
        self,
        *,
        event: CandleEvent,
        score: SignalScore,
        action: str,
        layer: str,
        reason: str,
        strategy_reason: str,
        checks: dict[str, Any] | None = None,
    ) -> None:
        ai = _ai_metadata(score)
        self.rejected_signals.append(
            BacktestRejectedSignal(
                timestamp=event.timestamp,
                symbol=event.symbol,
                action=action,
                price=_signal_price(event, self.signal_timing_policy),
                layer=layer,
                reason=reason,
                strategy_reason=strategy_reason,
                checks=checks or {},
                **ai,
            )
        )

    def _record_decision(
        self,
        *,
        event: CandleEvent,
        score: SignalScore | None,
        decision_action: str,
        final_action: str,
        final_status: str,
        reason: str,
        governor_allowed: bool | None = None,
        governor_reason: str | None = None,
        block_layer: str | None = None,
        block_reason: str | None = None,
        governor_checks: dict[str, Any] | None = None,
        evaluation_metadata: dict[str, Any] | None = None,
        cycle_candidate_count: int = 0,
        opportunity_rank: int | None = None,
        opportunity_score: int | None = None,
        opportunity_reasons: list[str] | None = None,
        opportunity_diagnostics: dict[str, Any] | None = None,
    ) -> None:
        ai = _ai_metadata(score)
        metadata = dict(score.metadata) if score is not None else {}
        if evaluation_metadata:
            metadata.update(evaluation_metadata)
        trend_fields = _trend_decision_fields(metadata)
        self.decisions.append(
            BacktestDecisionRecord(
                timestamp=event.timestamp,
                symbol=event.symbol,
                price=_signal_price(event, self.signal_timing_policy),
                strategy_mode=self.strategy_config.normalized_mode,
                score=int(score.score) if score is not None else None,
                priority=score.priority.value if score and score.priority else None,
                score_action=score.action if score is not None else None,
                decision_action=decision_action,
                final_action=final_action,
                final_status=final_status,
                reason=reason,
                governor_allowed=governor_allowed,
                governor_reason=governor_reason,
                block_layer=block_layer,
                block_reason=block_reason,
                trend_direction=trend_fields["trend_direction"],
                trend_strength=trend_fields["trend_strength"],
                market_regime=trend_fields["market_regime"],
                primary_regime=trend_fields["primary_regime"],
                directional_regime=trend_fields["directional_regime"],
                volatility_state=trend_fields["volatility_state"],
                trend_confidence=trend_fields["trend_confidence"],
                is_with_trend=trend_fields["is_with_trend"],
                is_against_trend=trend_fields["is_against_trend"],
                trend_alignment=trend_fields["trend_alignment"],
                governor_checks=governor_checks or {},
                cycle_candidate_count=cycle_candidate_count,
                opportunity_rank=opportunity_rank,
                opportunity_score=opportunity_score,
                opportunity_reasons=list(opportunity_reasons or []),
                opportunity_diagnostics=dict(opportunity_diagnostics or {}),
                metadata=metadata,
                **ai,
            )
        )


def _limit_touched(
    action: TradeAction,
    candle,
    limit_price: Decimal,
    *,
    require_trade_through: bool = False,
    price_tick: Decimal = Decimal("0"),
) -> bool:
    offset = max(Decimal("0"), Decimal(str(price_tick))) if require_trade_through else Decimal("0")
    return limit_entry_touched(action.value, candle, limit_price, trade_through_offset=offset)


def _market_fallback_price(event: CandleEvent, market_entry_policy: str) -> Decimal:
    # Expiration is only knowable after the candle did not trade through the
    # limit.  Filling retrospectively at its open would be lookahead.
    return Decimal(str(event.candle.close))


def _signal_price(event: CandleEvent, signal_timing_policy: str) -> Decimal:
    if str(signal_timing_policy or "").upper().strip() == "ON_OPEN":
        return Decimal(str(event.candle.open))
    return Decimal(str(event.candle.close))


def _candle_open_timestamp(event: CandleEvent) -> str:
    return datetime.fromtimestamp(
        event.candle.open_time_ms / 1000,
        tz=timezone.utc,
    ).isoformat()


def _candle_close_timestamp(event: CandleEvent) -> str:
    return datetime.fromtimestamp(
        event.candle.close_time_ms / 1000,
        tz=timezone.utc,
    ).isoformat()


def _execution_decision_with_limit_status(
    decision: SmartExecutionDecision,
    *,
    status: str,
    wait_candles: int,
    fallback_to_market_used: bool,
    entry_order_type: str | None = None,
) -> SmartExecutionDecision:
    diagnostics = {
        **decision.diagnostics,
        "limit_fill_status": status,
        "limit_wait_candles": max(0, int(wait_candles)),
        "fallback_to_market_used": bool(fallback_to_market_used),
    }
    return replace(
        decision,
        entry_order_type=entry_order_type or decision.entry_order_type,
        reason=_smart_execution_reason_for_status(status, decision.reason),
        affected_result=(
            decision.affected_result
            or status
            in {
                "LIMIT_FILLED",
                "LIMIT_EXPIRED",
                "LIMIT_FALLBACK_TO_MARKET",
                "LIMIT_PENDING",
            }
        ),
        diagnostics=diagnostics,
    )


def _smart_execution_reason_for_status(status: str, fallback: str) -> str:
    if status == "LIMIT_FILLED":
        return "SMART_EXECUTION_LIMIT_FILLED"
    if status == "LIMIT_EXPIRED":
        return "SMART_EXECUTION_LIMIT_EXPIRED"
    if status == "LIMIT_FALLBACK_TO_MARKET":
        return "SMART_EXECUTION_LIMIT_FALLBACK_TO_MARKET"
    if status == "MARKET_SELECTED":
        return "SMART_EXECUTION_MARKET_SELECTED"
    if status == "LIMIT_PENDING":
        return "SMART_EXECUTION_LIMIT_PENDING"
    return fallback


def _limit_status_from_decision(decision: SmartExecutionDecision) -> dict[str, Any]:
    diagnostics = decision.diagnostics if isinstance(decision.diagnostics, dict) else {}
    return {
        "limit_fill_status": diagnostics.get("limit_fill_status"),
        "limit_wait_candles": int(diagnostics.get("limit_wait_candles", 0) or 0),
        "fallback_to_market_used": bool(diagnostics.get("fallback_to_market_used", False)),
    }


def _planned_exit_decision_metadata(
    planned_exit: dict[str, Any],
    *,
    position=None,
) -> dict[str, Any]:
    return {
        "dynamic_exit_used": planned_exit["dynamic_exit_used"],
        "exit_source": planned_exit["exit_source"],
        "recommended_stop_price": _decimal_str(planned_exit["recommended_stop_price"]),
        "recommended_take_profit_price": _decimal_str(
            planned_exit["recommended_take_profit_price"]
        ),
        "actual_stop_price_used": _decimal_str(
            getattr(position, "stop_price", planned_exit["stop_price"])
        ),
        "actual_take_profit_price_used": _decimal_str(
            getattr(position, "take_profit_price", planned_exit["take_profit_price"])
        ),
        "reward_risk_liquido": _decimal_str(planned_exit["reward_risk_liquido"]),
        "raw_reward_risk": _decimal_str(planned_exit["raw_reward_risk"]),
        "cost_to_reward_ratio": _decimal_str(planned_exit["cost_to_reward_ratio"]),
        "risk_pct": _decimal_str(planned_exit["risk_pct"]),
        "reward_pct": _decimal_str(planned_exit["reward_pct"]),
        "planned_exit_rejection_reason": planned_exit["planned_exit_rejection_reason"],
        "planned_exit_diagnostics": dict(planned_exit["diagnostics"]),
    }


def _trend_decision_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "trend_direction": metadata.get("trend_direction"),
        "trend_strength": metadata.get("trend_strength"),
        "market_regime": metadata.get("market_regime"),
        "primary_regime": metadata.get("primary_regime"),
        "directional_regime": metadata.get("directional_regime"),
        "volatility_state": metadata.get("volatility_state"),
        "trend_confidence": metadata.get("trend_confidence"),
        "is_with_trend": bool(metadata.get("is_with_trend", False)),
        "is_against_trend": bool(metadata.get("is_against_trend", False)),
        "trend_alignment": str(metadata.get("trend_alignment") or "NEUTRAL_OR_UNKNOWN"),
    }


def _ai_metadata(score: SignalScore | None) -> dict[str, Any]:
    metadata = score.metadata if score is not None else {}
    return {
        "model_source": metadata.get("model_source"),
        "model_path": metadata.get("model_path"),
        "long_score": metadata.get("long_score"),
        "short_score": metadata.get("short_score"),
        "action_candidate": metadata.get("action_candidate"),
        "direction_candidate": metadata.get("direction_candidate"),
    }


def _quantity_checks(error: TradeNotExecutableError) -> dict[str, str]:
    calculation = error.calculation
    return {
        "requested_notional": str(calculation.requested_notional),
        "raw_quantity": str(calculation.raw_quantity),
        "rounded_quantity": str(calculation.rounded_quantity),
        "minimum_required_notional": str(calculation.minimum_required_notional),
        "quantity_step": str(calculation.quantity_step),
        "min_quantity": str(calculation.min_quantity),
        "min_notional": str(calculation.min_notional),
    }


def _decimal_str(value) -> str | None:
    if value is None:
        return None
    return str(value)


def _stable_tie_breaker(timestamp: str, symbol: str) -> int:
    digest = hashlib.sha256(f"{timestamp}:{symbol}".encode()).hexdigest()
    return int(digest[:12], 16)
