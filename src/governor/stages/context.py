"""Context stages: the light snapshot and score (1) and the heavy snapshot (9).

Both stages gather data from several Governor collaborators. Their ports are
grouped in frozen dataclasses so each dependency is named and typed in one
place; the Governor fills them with its current collaborators per evaluation.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from config.symbol_context import SymbolTradingConfig
from domain.models import FuturesPosition, MarketSnapshot
from futures.operational import manual_reconcile_required
from futures.state import RuntimeState
from futures.trend import TrendContext
from governor.cycle_context import CycleContext
from governor.evaluation.outcomes import EventJournal
from governor.evaluation.runtime_board import SymbolRuntimeBoard
from governor.events import HEAVY_CHECK_STARTED, SCANNER_SCORE
from governor.governor_helpers import _is_warmup_score
from governor.governor_observability import elapsed_ms, monotonic_now
from governor.governor_trade_helpers import _ai_signal_audit_fields, _status_from_score
from governor.models import SymbolRuntimeSnapshot, SymbolRuntimeStatus
from governor.services import AIGateService
from governor.stages.ports import StateTransitionRecorder, TracePayload, required
from governor.symbol_evaluation_pipeline import (
    HeavyEvaluation,
    LightEvaluation,
    SymbolEvaluationContext,
    SymbolEvaluationResult,
)
from governor.symbol_state_machine import SymbolOperationalState
from market_data.market_snapshots import HeavyMarketSnapshot, LightMarketSnapshot
from scanner.priority_scheduler import ScheduledSymbol
from scanner.signal_score import SignalScore
from trading.learning_advisory import LearningAdvisoryEvaluation
from utils.events import EventChannel, observe_event

AttachTrend = Callable[[MarketSnapshot, TrendContext], MarketSnapshot]


class Scheduler(Protocol):
    def update(self, score: SignalScore, now: float) -> ScheduledSymbol: ...


class TrendForMarket(Protocol):
    def __call__(
        self, *, symbol: str, market: MarketSnapshot, timestamp: float
    ) -> TrendContext | None: ...


class SkipScore(Protocol):
    def __call__(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        market: MarketSnapshot,
        reason: str,
        now: float,
    ) -> SignalScore: ...


class LearningAdvisoryRecorder(Protocol):
    def __call__(
        self, *, symbol_config: SymbolTradingConfig, action: str | None
    ) -> LearningAdvisoryEvaluation | None: ...


@dataclass(frozen=True)
class LightContextPorts:
    """Everything stage 1 reads or notifies while it scores the light snapshot."""

    router: Any
    remember_operational_snapshot: Callable[[CycleContext | None], None]
    trend_for_market: TrendForMarket
    attach_trend: AttachTrend
    record_trend: Callable[[TrendContext], None]
    position_for_symbol: Callable[..., FuturesPosition]
    runtime_state_for: Callable[[SymbolTradingConfig], RuntimeState | None] | None
    strategy: Any
    skip_score: SkipScore
    warmup_interval_seconds: Callable[[], float]
    ai_gate: AIGateService
    scheduler: Scheduler
    score_journal: EventJournal
    trace_payload: TracePayload
    execution_mode: str
    environment: str
    record_learning_advisory: LearningAdvisoryRecorder
    board: SymbolRuntimeBoard
    record_transition: StateTransitionRecorder


class LightContextStage:
    """Stage 1: light snapshot, trend context, position, strategy score and board."""

    name = "light_snapshot_score"

    def __init__(self, ports: LightContextPorts) -> None:
        self.ports = ports

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        ports = self.ports
        symbol_config = evaluation.evaluation_input.symbol_config
        now = evaluation.evaluation_input.now
        context = evaluation.cycle_context
        strategy = ports.strategy

        ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.LIGHT_EVALUATED,
            reason="LIGHT_EVALUATION_STARTED",
            stage="LIGHT_SNAPSHOT",
        )
        light = self._light_snapshot(symbol_config.symbol, now=now, context=context)
        evaluation.light = light
        ports.remember_operational_snapshot(context)
        evaluation.trend_context = ports.trend_for_market(
            symbol=symbol_config.symbol,
            market=light.market,
            timestamp=now,
        )
        if evaluation.trend_context is not None:
            light = replace(
                light,
                market=ports.attach_trend(light.market, evaluation.trend_context),
            )
            ports.record_trend(evaluation.trend_context)
        evaluation.light = light
        position = ports.position_for_symbol(symbol_config, context=context)
        evaluation.position = position
        evaluation.runtime_state = (
            ports.runtime_state_for(symbol_config) if ports.runtime_state_for else None
        )
        score = self._score_or_skip(evaluation, light, position)
        evaluation.warmup = _is_warmup_score(score)
        if evaluation.warmup:
            score = replace(
                score,
                suggested_next_check_seconds=ports.warmup_interval_seconds(),
            )
        evaluation.score = score
        evaluation.ai_audit = _ai_signal_audit_fields(score)
        ports.ai_gate.record_signal(symbol_config=symbol_config, score=score, stage="LIGHT_SCORE")
        scheduled = ports.scheduler.update(score, now)
        evaluation.scheduled = scheduled
        priority = required(score.priority, "score.priority")
        ports.score_journal.append(
            SCANNER_SCORE,
            **ports.trace_payload(),
            symbol=symbol_config.symbol,
            strategy=strategy.global_strategy_name,
            score=score.score,
            priority=priority.value,
            action=score.action,
            reason=score.reason,
            execution_mode=ports.execution_mode,
            environment=ports.environment,
            **evaluation.ai_audit,
        )

        evaluation.learning_advisory = ports.record_learning_advisory(
            symbol_config=symbol_config,
            action=score.action,
        )

        status = (
            SymbolRuntimeStatus.WARMUP
            if evaluation.warmup
            else _status_from_score(priority, position.is_open)
        )
        evaluation.operational_state = ""
        evaluation.protection_warning = ""
        runtime_state = evaluation.runtime_state
        if runtime_state is not None:
            evaluation.operational_state = runtime_state.operational_state.value
            evaluation.protection_warning = runtime_state.protection_warning
            if position.is_open and evaluation.operational_state in SymbolRuntimeStatus.__members__:
                status = SymbolRuntimeStatus[evaluation.operational_state]
        if manual_reconcile_required(symbol_config.runtime_dir, symbol_config.symbol):
            status = SymbolRuntimeStatus.MANUAL_RECONCILE_REQUIRED
            evaluation.operational_state = SymbolRuntimeStatus.MANUAL_RECONCILE_REQUIRED.value
        evaluation.status = status
        ports.board.publish(
            SymbolRuntimeSnapshot(
                symbol=symbol_config.symbol,
                enabled=True,
                status=status,
                score=int(score.score),
                priority=priority,
                last_price=str(light.market.price),
                last_checked_at=scheduled.last_checked_at,
                next_check_at=scheduled.next_check_at,
                data_age_seconds=light.data_age_seconds,
                stale=light.stale,
                position_direction=position.direction.value,
                position_quantity=str(position.quantity),
                entry_price=str(position.entry_price),
                unrealized_pnl=str(position.unrealized_pnl),
                operational_state=evaluation.operational_state,
                protection_warning=evaluation.protection_warning,
                strategy=strategy.global_strategy_name,
                reason=evaluation.protection_warning or score.reason,
                error="",
            )
        )
        evaluation.light_evaluation = LightEvaluation(
            evaluation_input=evaluation.evaluation_input,
            light=light,
            position=position,
            runtime_state=runtime_state,
            score=score,
            scheduled=scheduled,
            status=status,
            warmup=evaluation.warmup,
            operational_state=evaluation.operational_state,
            protection_warning=evaluation.protection_warning,
        )
        return None

    def _score_or_skip(
        self,
        evaluation: SymbolEvaluationContext,
        light: LightMarketSnapshot,
        position: FuturesPosition,
    ) -> SignalScore:
        """Score the symbol, or record why the strategy skipped it this cycle."""
        ports = self.ports
        strategy = ports.strategy
        symbol_config = evaluation.evaluation_input.symbol_config
        should_evaluate = getattr(strategy, "should_evaluate", None)
        strategy_should_run = True
        strategy_skip_reason = ""
        if callable(should_evaluate):
            strategy_should_run, strategy_skip_reason = should_evaluate(
                symbol_config.symbol,
                light.market,
                symbol_config=symbol_config,
            )

        if strategy_should_run:
            score: SignalScore = strategy.score(symbol_config, light.market, position)
            evaluation.score = score
            mark_evaluated = getattr(strategy, "mark_evaluated", None)
            if callable(mark_evaluated):
                mark_evaluated(symbol_config.symbol, light.market)
            return score
        score = ports.skip_score(
            symbol_config=symbol_config,
            market=light.market,
            reason=strategy_skip_reason,
            now=evaluation.evaluation_input.now,
        )
        evaluation.score = score
        observe_event(
            "strategy_evaluation_skipped",
            component=__name__,
            category="strategy",
            channel=EventChannel.AUDIT,
            symbol=symbol_config.symbol,
            strategy=strategy.global_strategy_name,
            reason=strategy_skip_reason,
            primary_timeframe=getattr(light.market, "primary_timeframe", None),
        )
        return score

    def _light_snapshot(
        self, symbol: str, *, now: float, context: CycleContext | None
    ) -> LightMarketSnapshot:
        router = self.ports.router
        try:
            snapshot: LightMarketSnapshot = router.get_light_snapshot(
                symbol,
                now=now,
                operational_snapshot=(
                    context.operational_snapshot if context is not None else None
                ),
            )
        except TypeError as error:
            if "operational_snapshot" not in str(error):
                raise
            snapshot = router.get_light_snapshot(symbol, now=now)
        return snapshot


@dataclass(frozen=True)
class UserDataCachePorts:
    """User-data-stream cache hooks around the heavy snapshot (shadow and assisted)."""

    record_stale_guard: Callable[..., Any]
    record_usage_shadow: Callable[..., Any]
    record_assisted_plan_shadow: Callable[..., Any]
    cached_position: Callable[..., Any]
    cached_account_info: Callable[..., Any]
    cached_open_orders: Callable[..., Any]
    record_runtime_metrics: Callable[..., Any]
    record_shadow_audit: Callable[..., Any]
    record_reconciliation: Callable[..., Any]


@dataclass(frozen=True)
class HeavyContextPorts:
    """Everything stage 9 reads or notifies while it resolves the heavy snapshot."""

    router: Any
    prefetched_snapshot_max_age_seconds: float
    prepare_symbol_snapshot: Callable[[CycleContext | None, str], Any] | None
    user_data: UserDataCachePorts
    journal: EventJournal
    trace_payload: TracePayload
    strategy_name: Callable[[], str]
    remember_operational_snapshot: Callable[[CycleContext | None], None]
    sync_position: Callable[[SymbolTradingConfig, FuturesPosition], FuturesPosition] | None
    attach_trend: AttachTrend
    record_adaptive_management: Callable[..., Any]
    record_transition: StateTransitionRecorder


@dataclass(frozen=True)
class _CachedReads:
    """What the private-stream cache served for one heavy snapshot (``None`` means REST)."""

    assisted_plan: Any
    position: Any
    account_info: Any
    open_orders: Any

    def sources(self, prefetch_source: str | None) -> tuple[str, str, str]:
        """Source label of the position, account and open orders, in that order."""

        def source(cached: object) -> str:
            if prefetch_source is not None:
                return prefetch_source
            return "USER_DATA_CACHE" if cached is not None else "REST"

        return source(self.position), source(self.account_info), source(self.open_orders)


class HeavyContextStage:
    """Stage 9: resolve the heavy snapshot (prefetched or just in time)."""

    name = "heavy_snapshot"

    def __init__(self, ports: HeavyContextPorts) -> None:
        self.ports = ports

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        ports = self.ports
        symbol_config = evaluation.evaluation_input.symbol_config
        now = evaluation.evaluation_input.now
        light_position = required(evaluation.position, "position")

        prefetched_snapshot = self._take_prefetched(evaluation)
        if prefetched_snapshot is None and ports.prepare_symbol_snapshot is not None:
            ports.prepare_symbol_snapshot(evaluation.cycle_context, symbol_config.symbol)
        cached = self._read_user_data_cache(evaluation)
        self._record_heavy_check_started(evaluation, light_position)

        heavy_snapshot_started_at = monotonic_now()
        router_heavy = self._resolve_router_heavy(evaluation, prefetched_snapshot, cached)
        evaluation.router_heavy = router_heavy
        heavy_snapshot_duration_ms = elapsed_ms(heavy_snapshot_started_at)
        evaluation.heavy = router_heavy

        position_source, account_source, open_orders_source = cached.sources(
            "PARALLEL_PREFETCH" if prefetched_snapshot is not None else None
        )
        ports.user_data.record_runtime_metrics(
            symbol_config=symbol_config,
            assisted_plan=cached.assisted_plan,
            position_source=position_source,
            account_source=account_source,
            open_orders_source=open_orders_source,
            heavy_snapshot_duration_ms=heavy_snapshot_duration_ms,
        )

        heavy, final_position_source = self._with_local_position(
            evaluation, router_heavy, light_position
        )

        ports.user_data.record_shadow_audit(
            symbol_config=symbol_config, heavy=router_heavy, now=now
        )
        ports.user_data.record_reconciliation(
            symbol_config=symbol_config,
            heavy=router_heavy,
            now=now,
            rest_backed_position=cached.position is None,
            rest_backed_account=cached.account_info is None,
            rest_backed_open_orders=cached.open_orders is None,
            position_source=position_source,
            account_source=account_source,
            open_orders_source=open_orders_source,
            final_position_source=final_position_source,
        )

        ports.record_adaptive_management(
            symbol_config=symbol_config,
            heavy=heavy,
            runtime_state=evaluation.runtime_state,
        )
        ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.HEAVY_EVALUATED,
            reason="HEAVY_SNAPSHOT_EVALUATED",
            stage="HEAVY_SNAPSHOT",
        )
        if isinstance(evaluation.light_evaluation, LightEvaluation):
            evaluation.heavy_evaluation = HeavyEvaluation(
                light_evaluation=evaluation.light_evaluation,
                heavy=heavy,
            )
        return None

    def _take_prefetched(self, evaluation: SymbolEvaluationContext) -> HeavyMarketSnapshot | None:
        context = evaluation.cycle_context
        if context is None:
            return None
        return context.take_prefetched_heavy_snapshot(
            evaluation.evaluation_input.symbol_config.symbol,
            now=time.time(),
            max_age_seconds=self.ports.prefetched_snapshot_max_age_seconds,
        )

    def _read_user_data_cache(self, evaluation: SymbolEvaluationContext) -> _CachedReads:
        """Run the private-stream cache guards and take what the cache may serve."""

        user_data = self.ports.user_data
        symbol_config = evaluation.evaluation_input.symbol_config
        now = evaluation.evaluation_input.now
        user_data.record_stale_guard(symbol_config=symbol_config, now=now)
        usage_evaluation = user_data.record_usage_shadow(symbol_config=symbol_config, now=now)
        assisted_plan = user_data.record_assisted_plan_shadow(
            symbol_config=symbol_config,
            usage_evaluation=usage_evaluation,
        )
        return _CachedReads(
            assisted_plan=assisted_plan,
            position=user_data.cached_position(
                symbol_config=symbol_config,
                assisted_plan=assisted_plan,
            ),
            account_info=user_data.cached_account_info(
                symbol_config=symbol_config,
                assisted_plan=assisted_plan,
            ),
            open_orders=user_data.cached_open_orders(
                symbol_config=symbol_config,
                assisted_plan=assisted_plan,
            ),
        )

    def _record_heavy_check_started(
        self,
        evaluation: SymbolEvaluationContext,
        light_position: FuturesPosition,
    ) -> None:
        ports = self.ports
        score = required(evaluation.score, "score")
        ports.journal.append(
            HEAVY_CHECK_STARTED,
            **ports.trace_payload(),
            symbol=evaluation.evaluation_input.symbol_config.symbol,
            strategy=ports.strategy_name(),
            score=score.score,
            priority=required(score.priority, "score.priority").value,
            action=score.action,
            position_open=light_position.is_open,
            request_budget=(
                evaluation.budget_decision.to_dict()
                if evaluation.budget_decision is not None
                else {"enabled": False}
            ),
            **evaluation.ai_audit,
        )

    def _resolve_router_heavy(
        self,
        evaluation: SymbolEvaluationContext,
        prefetched_snapshot: HeavyMarketSnapshot | None,
        cached: _CachedReads,
    ) -> HeavyMarketSnapshot:
        """The prefetched snapshot when it is still valid, otherwise a just-in-time read."""

        ports = self.ports
        context = evaluation.cycle_context
        symbol = evaluation.evaluation_input.symbol_config.symbol
        if prefetched_snapshot is not None:
            ports.journal.append(
                "heavy_snapshot_prefetch_used",
                **ports.trace_payload(),
                symbol=symbol,
                private_state_generation=(
                    context.private_state_generation if context is not None else 0
                ),
                data_age_seconds=prefetched_snapshot.data_age_seconds,
            )
            return prefetched_snapshot

        heavy_kwargs: dict[str, Any] = {
            "now": evaluation.evaluation_input.now,
            "cached_position": cached.position,
            "operational_snapshot": (context.operational_snapshot if context is not None else None),
        }
        if cached.account_info is not None:
            heavy_kwargs["cached_account_info"] = cached.account_info
        if cached.open_orders is not None:
            heavy_kwargs["cached_open_orders"] = cached.open_orders
        router_heavy = self._heavy_snapshot(symbol, heavy_kwargs)
        ports.remember_operational_snapshot(context)
        return router_heavy

    def _with_local_position(
        self,
        evaluation: SymbolEvaluationContext,
        router_heavy: HeavyMarketSnapshot,
        light_position: FuturesPosition,
    ) -> tuple[HeavyMarketSnapshot, str]:
        """Apply the engine's synced position (or the light one) and the trend context."""

        ports = self.ports
        symbol_config = evaluation.evaluation_input.symbol_config
        context = evaluation.cycle_context
        heavy = router_heavy
        final_position_source = "LOCAL_STATE"
        if ports.sync_position is not None:
            heavy = replace(heavy, position=ports.sync_position(symbol_config, heavy.position))
            evaluation.heavy = heavy
            final_position_source = "SYNC_POSITION"
            if context is not None:
                context.positions[symbol_config.symbol] = heavy.position
        else:
            heavy = replace(heavy, position=light_position)

        if evaluation.trend_context is not None:
            heavy = replace(
                heavy,
                market=ports.attach_trend(heavy.market, evaluation.trend_context),
            )
        evaluation.heavy = heavy
        return heavy, final_position_source

    def _heavy_snapshot(self, symbol: str, heavy_kwargs: dict[str, Any]) -> HeavyMarketSnapshot:
        router = self.ports.router
        try:
            snapshot: HeavyMarketSnapshot = router.get_heavy_snapshot(symbol, **heavy_kwargs)
        except TypeError as error:
            if "operational_snapshot" not in str(error):
                raise
            heavy_kwargs.pop("operational_snapshot", None)
            snapshot = router.get_heavy_snapshot(symbol, **heavy_kwargs)
        return snapshot
