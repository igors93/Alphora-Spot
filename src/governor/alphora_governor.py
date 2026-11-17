import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from binance.futures_client import BinanceFuturesClient
from config import AppConfig
from config.symbol_context import (
    PortfolioRiskSettings,
    SymbolTradingConfig,
    load_portfolio_risk_settings,
    resolve_governor_symbol_configs,
)
from domain.models import FuturesPosition
from execution.state_reconciliation import StateReconciliationEngine
from execution.trade_audit import TradeAuditRecorder
from futures.execution.order_policy import OrderExecutionPolicy
from futures.planning.economic_gate import EconomicTradeGate
from futures.planning.trade_plan_execution_validator import TradePlanExecutionValidator
from futures.trend import TrendContextEngine
from governor.adaptive_adjustment_activation import AdaptiveAdjustmentActivationPolicy
from governor.adaptive_adjustment_plan import AdaptiveAdjustmentPlanner
from governor.adaptive_manager import AdaptivePositionManager
from governor.composition import build_governor_components, trend_timeframe
from governor.control_requests import GovernorControlRequests
from governor.cycle import CycleCoordinator, CyclePorts
from governor.cycle.prefetch import HeavyPrefetcher
from governor.cycle.snapshots import CycleSnapshots
from governor.cycle.telemetry import CycleMetricsState, CycleTelemetry
from governor.cycle.wakeup import MarketWakeup
from governor.cycle_context import CycleContext
from governor.daily_portfolio_metrics import DailyPortfolioMetricsReader
from governor.evaluation import StageCallable, StageOutcomes, StageRunner, SymbolRuntimeBoard
from governor.evaluation.evaluator import SymbolEvaluator
from governor.evaluation.stage_telemetry import StageTelemetry
from governor.evaluation.transitions import SymbolTransitionRecorder
from governor.event_payloads import runtime_effective_config_payload
from governor.event_sink import EventSink
from governor.events import (
    GOVERNOR_STARTED,
    RUNTIME_EFFECTIVE_CONFIG_SNAPSHOT,
)
from governor.execution_engine import ExecutionEngine
from governor.hot_symbol_worker import HotSymbolWorker
from governor.market_data_hub import MarketDataHub
from governor.market_data_router import MarketDataRouter
from governor.models import SymbolRuntimeSnapshot, SymbolRuntimeStatus
from governor.opportunity_ranker import OpportunityRanker
from governor.portfolio_risk_engine import (
    BALANCE_UNAVAILABLE_FIX_HINT,
    BALANCE_UNAVAILABLE_MESSAGE,
    BALANCE_UNAVAILABLE_SAFETY_ACTION,
    PortfolioRiskEngine,
)
from governor.portfolio_snapshot_provider import PortfolioSnapshotProvider
from governor.pre_execution_guard import PreExecutionGuard
from governor.safe_journal import safe_append
from governor.services import (
    AIGateService,
    DefensiveModeService,
    FastLaneService,
    LearningPolicies,
    LearningReportState,
    LearningService,
    OpportunityService,
    RequestBudgetService,
    TradeEventRecorder,
    UserDataCacheService,
)
from governor.services.adaptive_management import AdaptiveManagementService
from governor.services.economic_gate import EconomicGateService, economic_gate_event_payload
from governor.services.intelligent_flow import IntelligentFlowComponents, IntelligentFlowService
from governor.services.symbol_health import SymbolHealthService
from governor.services.trade_planning import TradePlanningService
from governor.services.trend_context import TrendContextService, attach_trend_context
from governor.smart_execution import SmartExecutionEngine
from governor.smart_sizing import SmartSizingEngine
from governor.stages.context import (
    HeavyContextPorts,
    HeavyContextStage,
    LightContextPorts,
    LightContextStage,
    UserDataCachePorts,
)
from governor.stages.decision import HotSymbolDecisionStage, StateReconciliationStage
from governor.stages.decision_kernel import build_decision_kernel_stages
from governor.stages.execution import ExecutionPorts, ExecutionStage
from governor.stages.execution_planning import ExecutionPlannerStage
from governor.stages.guards import DefensiveAndAIGuardStage
from governor.stages.light import (
    LightOnlyStage,
    ManualControlStage,
    RequestBudgetStage,
    WarmupStage,
)
from governor.stages.planning import (
    IntelligentLayersPorts,
    IntelligentLayersStage,
    TradePlanPorts,
    TradePlanStage,
)
from governor.stages.risk import FinalRiskGuardsStage, LearningExecutionStage, RiskPorts
from governor.stages.symbol_health import Phase, QuarantineStage, SymbolWarmupStage
from governor.state_writer import GovernorStateWriter
from governor.strategy_coordinator import StrategyCoordinator
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext
from governor.symbol_quarantine import SymbolQuarantine
from governor.symbol_state_machine import SymbolStateTracker
from governor.trade_pipeline import TradePipeline
from governor.user_data_cache import UserDataShadowCache
from governor.user_data_stream import UserDataStreamHub
from market_data.candle_hub import CandleDataHub
from market_data.snapshot_provider import (
    SnapshotProvider,
    snapshot_error_diagnostics_payload,
    wire_names,
)
from operational_module_modes import ModuleMode
from scanner.priority_scheduler import PriorityScheduler
from trading.intelligent_execution_planner import IntelligentExecutionPlanner
from trading.intelligent_exit_activation import IntelligentExitActivationPolicy
from trading.intelligent_exit_builder import IntelligentExitBuilder
from trading.intelligent_exit_comparator import IntelligentExitComparator
from trading.intelligent_order_activation import IntelligentOrderActivationPolicy
from trading.intelligent_order_builder import IntelligentOrderBuilder
from trading.intelligent_order_comparator import IntelligentOrderComparator
from trading.learning_advisory import LearningAdvisoryEngine
from trading.learning_execution_shadow import LearningExecutionShadowPolicy
from trading.learning_paper_execution import LearningPaperExecutionPolicy
from trading.learning_plan_guard import LearningPlanGuard
from trading.learning_report import TradePlanLearningReporter
from trading.learning_shadow_impact_report import LearningShadowImpactReporter
from trading.learning_testnet_execution import LearningTestnetExecutionPolicy
from trading.learning_testnet_observation import LearningTestnetObservationPolicy
from utils.events import (
    EventChannel,
    observe_error,
    observe_event,
    register_event_sink,
    unregister_event_sink,
)
from utils.journals import JournalRegistry
from utils.process_metrics import process_memory_snapshot
from utils.trade_journal import TradeJournal


class AlphoraGovernor:
    """Facade of the Governor runtime: start, run_cycle and the state it writes.

    The composition root (``governor/composition.py``) builds every component and
    installs it here; the attributes below are that contract. The Governor builds
    its services and stage tuples on demand from these attributes, so a test or the
    golden harness that replaces a collaborator after construction is honored.
    """

    # Installed by build_governor_components (see GovernorComponents).
    # data
    logger: logging.Logger
    scheduler: PriorityScheduler
    market_wakeup: MarketWakeup
    market_data_hub: MarketDataHub | None
    user_data_stream_hub: UserDataStreamHub | None
    user_data_shadow_cache: UserDataShadowCache
    _last_user_data_cache_audits: dict[str, dict[str, Any]]
    candle_data_hub: CandleDataHub | None
    market_data_router: MarketDataRouter
    snapshot_provider: SnapshotProvider | None
    trend_context_engine: TrendContextEngine
    # decision
    strategy_coordinator: StrategyCoordinator
    hot_symbol_worker: HotSymbolWorker
    trade_pipeline: TradePipeline | None
    intelligent_order_builder: IntelligentOrderBuilder | None
    intelligent_order_comparator: IntelligentOrderComparator | None
    intelligent_exit_builder: IntelligentExitBuilder | None
    intelligent_exit_comparator: IntelligentExitComparator | None
    intelligent_exit_activation_policy: IntelligentExitActivationPolicy | None
    intelligent_execution_planner: IntelligentExecutionPlanner | None
    intelligent_order_activation_policy: IntelligentOrderActivationPolicy | None
    adaptive_manager: AdaptivePositionManager | None
    adaptive_adjustment_planner: AdaptiveAdjustmentPlanner | None
    adaptive_adjustment_activation_policy: AdaptiveAdjustmentActivationPolicy | None
    learning_advisory: LearningAdvisoryEngine | None
    learning_plan_guard: LearningPlanGuard | None
    learning_execution_shadow_policy: LearningExecutionShadowPolicy | None
    learning_paper_execution_policy: LearningPaperExecutionPolicy | None
    learning_testnet_observation_policy: LearningTestnetObservationPolicy | None
    learning_testnet_execution_policy: LearningTestnetExecutionPolicy | None
    opportunity_ranker: OpportunityRanker
    # risk
    economic_trade_gate: EconomicTradeGate
    trade_plan_execution_validator: TradePlanExecutionValidator
    order_execution_policy: OrderExecutionPolicy
    portfolio_risk_engine: PortfolioRiskEngine
    pre_execution_guard: PreExecutionGuard
    operational_module_modes: dict[str, ModuleMode]
    smart_sizing_engine: SmartSizingEngine
    smart_execution_engine: SmartExecutionEngine
    state_reconciliation_engine: StateReconciliationEngine
    daily_metrics_reader: DailyPortfolioMetricsReader
    portfolio_snapshot_provider: PortfolioSnapshotProvider
    symbol_quarantine: SymbolQuarantine
    # execution
    execution_engine: ExecutionEngine
    trade_audit_recorder: TradeAuditRecorder
    _risk_guard_for_symbol: Callable[[SymbolTradingConfig], Any]
    # state
    max_symbols_per_cycle: int
    _cycle_sequence: int
    _symbol_eval_sequence: int
    _current_cycle_id: str
    _current_symbol_eval_id: str
    governor_events_path: Path
    journals: JournalRegistry
    events_journal: TradeJournal
    symbol_state_tracker: SymbolStateTracker
    score_journal: TradeJournal
    control_requests: GovernorControlRequests
    learning_reporter: TradePlanLearningReporter | None
    learning_shadow_impact_reporter: LearningShadowImpactReporter | None
    learning_state: LearningReportState
    state_path: Path
    state_writer: GovernorStateWriter
    paused_symbols: set[str]
    stopped_symbols: set[str]
    snapshots: dict[str, SymbolRuntimeSnapshot]
    _running: bool
    _new_positions_this_cycle: int
    _current_cycle_context: CycleContext | None
    _last_portfolio_state_payload: dict[str, Any] | None
    _last_portfolio_state_captured_at: float | None
    cycle_metrics_state: CycleMetricsState
    stage_telemetry: StageTelemetry
    _last_defensive_mode_active: bool
    _last_fast_lane_symbol_at: dict[str, float]

    def __init__(
        self,
        *,
        config: AppConfig,
        client: BinanceFuturesClient,
        symbol_configs: list[SymbolTradingConfig],
        portfolio_settings: PortfolioRiskSettings | None = None,
        max_symbols_per_cycle: int = 10,
    ) -> None:
        self.config = config
        self.client = client
        self.symbol_configs = [item for item in symbol_configs if item.enabled]
        if not self.symbol_configs:
            raise ValueError("Governador exige pelo menos um perfil habilitado")

        self.portfolio_settings = portfolio_settings or PortfolioRiskSettings()
        components = build_governor_components(
            config,
            client,
            governor=self,
            symbol_configs=self.symbol_configs,
            portfolio_settings=self.portfolio_settings,
            max_symbols_per_cycle=max_symbols_per_cycle,
        )
        components.install_on(self)
        self.components = components

    @classmethod
    def from_config(cls, config: AppConfig, client: BinanceFuturesClient) -> "AlphoraGovernor":
        symbol_configs = resolve_governor_symbol_configs(config)
        portfolio_settings = load_portfolio_risk_settings(config.portfolio_risk_path)
        return cls(
            config=config,
            client=client,
            symbol_configs=symbol_configs,
            portfolio_settings=portfolio_settings,
            max_symbols_per_cycle=(
                portfolio_settings.governor_max_symbols_per_cycle
                if portfolio_settings.governor_max_symbols_per_cycle > 0
                else len(symbol_configs)
            ),
        )

    def _refresh_candle_data_hub(self, *, now: float) -> None:
        hub = self.candle_data_hub
        if hub is None:
            return
        try:
            hub.refresh(now_ms=int(float(now) * 1000))
        except Exception as error:
            observe_event(
                "candle_hub_refresh_failed",
                component=__name__,
                category="candle_hub",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                reason=str(error),
                error_type=type(error).__name__,
            )

    def _next_cycle_id(self) -> str:
        sequence = self._cycle_sequence + 1
        self._cycle_sequence = sequence
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        timestamp = timestamp.replace("+00:00", "Z")
        return f"{timestamp}-{sequence:06d}"

    def _next_symbol_eval_id(self, *, cycle_id: str, symbol: str) -> str:
        sequence = self._symbol_eval_sequence + 1
        self._symbol_eval_sequence = sequence
        normalized_symbol = str(symbol or "UNKNOWN").upper().strip() or "UNKNOWN"
        return f"{cycle_id}:{normalized_symbol}:{sequence:06d}"

    def _cycle_id_for_symbol_eval(self, context: CycleContext | None) -> str:
        if context is not None and context.cycle_id:
            return context.cycle_id
        if self._current_cycle_id:
            return self._current_cycle_id
        return self._next_cycle_id()

    def _trace_payload(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self._current_cycle_id:
            payload["cycle_id"] = self._current_cycle_id
        if self._current_symbol_eval_id:
            payload["symbol_eval_id"] = self._current_symbol_eval_id
        return payload

    @contextmanager
    def _evaluation_trace_scope(self, evaluation: SymbolEvaluationContext) -> Iterator[None]:
        """Expose the evaluation identifiers to ``_trace_payload`` while stages run."""

        previous_cycle_id = self._current_cycle_id
        previous_symbol_eval_id = self._current_symbol_eval_id
        self._current_cycle_id = evaluation.evaluation_input.cycle_id
        self._current_symbol_eval_id = evaluation.evaluation_input.symbol_eval_id
        try:
            yield
        finally:
            self._current_cycle_id = previous_cycle_id
            self._current_symbol_eval_id = previous_symbol_eval_id

    def _stage_runner(self) -> StageRunner:
        return StageRunner(trace_scope=self._evaluation_trace_scope)

    def _runtime_board(self) -> SymbolRuntimeBoard:
        return SymbolRuntimeBoard(self.snapshots, strategy=self.strategy_coordinator)

    def _stage_outcomes(self) -> StageOutcomes:
        """Outcomes bound to the collaborators the Governor holds right now.

        Built on demand because tests and the golden harness replace the journal
        and the strategy coordinator after construction.
        """

        return StageOutcomes(
            journal=self.events_journal,
            trace_payload=self._trace_payload,
            board=self._runtime_board(),
            logger=self.logger,
        )

    def _ai_gate(self) -> AIGateService:
        return AIGateService(
            config=self.config,
            journal=self.events_journal,
            strategy=self.strategy_coordinator,
        )

    def _request_budget_service(self) -> RequestBudgetService:
        return RequestBudgetService(
            config=self.config,
            journal=self.events_journal,
            strategy=self.strategy_coordinator,
            trace_payload=self._trace_payload,
        )

    def _opportunity_service(self) -> OpportunityService:
        return OpportunityService(
            config=self.config,
            scheduler=self.scheduler,
            ranker=self.opportunity_ranker,
            quarantine=lambda: self.symbol_quarantine,
            portfolio_settings=self.portfolio_settings,
            journal=self.events_journal,
            trace_payload=self._trace_payload,
            logger=self.logger,
        )

    def _user_data_cache(self) -> UserDataCacheService:
        return UserDataCacheService(
            config=self.config,
            journal=self.events_journal,
            logger=self.logger,
            hub=self.user_data_stream_hub,
            cache=self.user_data_shadow_cache,
            audits=self._last_user_data_cache_audits,
            provider=self.snapshot_provider,
            on_cache_created=self._adopt_user_data_cache,
        )

    def _adopt_user_data_cache(self, cache: UserDataShadowCache) -> None:
        self.user_data_shadow_cache = cache

    def _on_user_data_stream_event(self, event: dict[str, Any]) -> None:
        """User Data Stream callback (runs on the stream thread)."""

        self._user_data_cache().record_stream_event(event)

    def _defensive_mode_service(self) -> DefensiveModeService:
        return DefensiveModeService(
            config=self.config,
            journal=self.events_journal,
            strategy=self.strategy_coordinator,
        )

    def _cycle_snapshots(self) -> CycleSnapshots:
        return CycleSnapshots(
            provider=self.snapshot_provider,
            execution_engine=self.execution_engine,
            router=self.market_data_router,
            symbol_configs=self.symbol_configs,
            journal=self.events_journal,
            logger=self.logger,
        )

    def _heavy_prefetcher(self) -> HeavyPrefetcher:
        return HeavyPrefetcher(
            config=self.config,
            provider=self.snapshot_provider,
            router=self.market_data_router,
            journal=self.events_journal,
        )

    def _cycle_telemetry(self) -> CycleTelemetry:
        return CycleTelemetry(
            config=self.config,
            journal=self.events_journal,
            logger=self.logger,
            market_data_hub=self.market_data_hub,
            state=self.cycle_metrics_state,
            current_cycle_id=lambda: self._current_cycle_id,
            stage_telemetry=self.stage_telemetry,
        )

    def _fast_lane(self) -> FastLaneService:
        return FastLaneService(
            config=self.config,
            journal=self.events_journal,
            symbols=[item.symbol for item in self.symbol_configs],
            stopped_symbols=self.stopped_symbols,
            paused_symbols=self.paused_symbols,
            last_selected_at=self._last_fast_lane_symbol_at,
        )

    def _learning(self) -> LearningService:
        return LearningService(
            journal=self.events_journal,
            policies=LearningPolicies(
                reporter=self.learning_reporter,
                shadow_impact_reporter=self.learning_shadow_impact_reporter,
                advisory=self.learning_advisory,
                plan_guard=self.learning_plan_guard,
                execution_shadow_policy=self.learning_execution_shadow_policy,
                paper_execution_policy=self.learning_paper_execution_policy,
                testnet_observation_policy=self.learning_testnet_observation_policy,
                testnet_execution_policy=self.learning_testnet_execution_policy,
            ),
            state=self.learning_state,
        )

    def _trade_events(self) -> TradeEventRecorder:
        return TradeEventRecorder(
            journal=self.events_journal,
            logger=self.logger,
            trace_payload=self._trace_payload,
            config=self.config,
            pipeline_events=self._pipeline_events(),
            trade_audit_recorder=self.trade_audit_recorder,
        )

    def _pipeline_events(self) -> EventSink:
        return EventSink(destinations=(self.events_journal,), logger=self.logger)

    def _economic_gate(self) -> EconomicGateService:
        return EconomicGateService(
            config=self.config,
            gate=self.economic_trade_gate,
            validator=self.trade_plan_execution_validator,
            journal=self.events_journal,
            outcomes=self._stage_outcomes(),
            logger=self.logger,
            trace_payload=self._trace_payload,
        )

    def _trade_planning(self) -> TradePlanningService:
        return TradePlanningService(
            config=self.config,
            journal=self.events_journal,
            pipeline_events=self._pipeline_events(),
            trade_pipeline=self.trade_pipeline,
            order_execution_policy=self.order_execution_policy,
            learning=self._learning(),
            strategy_name=lambda: self.strategy_coordinator.global_strategy_name,
            trace_payload=self._trace_payload,
        )

    def _intelligent_flow(self) -> IntelligentFlowService:
        return IntelligentFlowService(
            config=self.config,
            journal=self.events_journal,
            components=IntelligentFlowComponents(
                order_builder=self.intelligent_order_builder,
                order_comparator=self.intelligent_order_comparator,
                exit_builder=self.intelligent_exit_builder,
                exit_comparator=self.intelligent_exit_comparator,
                order_activation_policy=self.intelligent_order_activation_policy,
                exit_activation_policy=self.intelligent_exit_activation_policy,
                execution_planner=self.intelligent_execution_planner,
            ),
        )

    def _adaptive_management(self) -> AdaptiveManagementService:
        return AdaptiveManagementService(
            config=self.config,
            journal=self.events_journal,
            manager=self.adaptive_manager,
            planner=self.adaptive_adjustment_planner,
            activation_policy=self.adaptive_adjustment_activation_policy,
            execution_engine=self.execution_engine,
        )

    # --- symbol evaluation ------------------------------------------------------------------

    def _transitions(self) -> SymbolTransitionRecorder:
        return SymbolTransitionRecorder(
            tracker=self.symbol_state_tracker,
            journal=self.events_journal,
            logger=self.logger,
            trace_payload=self._trace_payload,
        )

    def _trend_context(self) -> TrendContextService:
        return TrendContextService(
            config=self.config,
            engine=self.trend_context_engine,
            journal=self.events_journal,
            timeframe_for=self._trend_timeframe_for,
            logger=self.logger,
            trace_payload=self._trace_payload,
        )

    def _trend_timeframe_for(self, symbol: str) -> str:
        primary = self.strategy_coordinator.requirements_for_symbol(symbol).primary_timeframe
        return trend_timeframe(self.config, primary)

    def _symbol_health(self) -> SymbolHealthService:
        return SymbolHealthService(
            config=self.config,
            strategy=self.strategy_coordinator,
            quarantine=self.symbol_quarantine,
            board=self._runtime_board(),
            journal=self.events_journal,
            logger=self.logger,
            trace_payload=self._trace_payload,
        )

    def _symbol_evaluator(self) -> SymbolEvaluator:
        return SymbolEvaluator(
            runner=self._stage_runner(),
            light_stages=self._light_evaluation_stages,
            heavy_stages=self._heavy_evaluation_stages,
            journal=self.events_journal,
            trace_payload=self._trace_payload,
            strategy_name=lambda: self.strategy_coordinator.global_strategy_name,
            cycle_id_for=self._cycle_id_for_symbol_eval,
            next_symbol_eval_id=self._next_symbol_eval_id,
        )

    def _record_symbol_error(self, symbol_config: SymbolTradingConfig, error: Exception) -> None:
        self._symbol_health().record_symbol_error(symbol_config, error)

    def _light_evaluation_stages(self) -> tuple[StageCallable, ...]:
        """Stages 1–8: everything that runs before a private (heavy) read."""

        outcomes = self._stage_outcomes()
        transitions = self._transitions()
        health = self._symbol_health()
        return (
            self._light_context_stage(transitions, health),
            ManualControlStage(outcomes=outcomes, record_transition=transitions),
            WarmupStage(outcomes=outcomes),
            self._symbol_warmup_stage("PRE_HEAVY", outcomes, health),
            self._symbol_quarantine_stage("PRE_HEAVY", outcomes, health),
            self._guard_stage("PRE_HEAVY", outcomes),
            LightOnlyStage(
                outcomes=outcomes,
                needs_heavy_check=RequestBudgetService.needs_heavy_check,
                journal=self.events_journal,
                trace_payload=self._trace_payload,
                strategy=self.strategy_coordinator,
                record_transition=transitions,
            ),
            RequestBudgetStage(
                outcomes=outcomes,
                consume_budget=self._request_budget_service().consume_heavy_check,
            ),
        )

    def _heavy_evaluation_stages(self) -> tuple[StageCallable, ...]:
        """Stages 9–22: the heavy snapshot, the decision, the plan and the dispatch."""

        outcomes = self._stage_outcomes()
        transitions = self._transitions()
        health = self._symbol_health()
        return (
            self._heavy_context_stage(transitions),
            HotSymbolDecisionStage(
                outcomes=outcomes,
                strategy=self.strategy_coordinator,
                hot_worker=self.hot_symbol_worker,
                risk_guard_for_symbol=self._risk_guard_factory(),
                paused_symbols=self.paused_symbols,
                journal=self.events_journal,
                trace_payload=self._trace_payload,
                record_transition=transitions,
                record_quarantine_event=health.record_quarantine_event,
            ),
            self._state_reconciliation_stage(outcomes),
            self._symbol_warmup_stage("POST_HEAVY", outcomes, health),
            self._symbol_quarantine_stage("POST_HEAVY", outcomes, health),
            self._guard_stage("POST_HEAVY", outcomes),
            *self._plan_stages(outcomes),
            self._execution_stage(outcomes),
        )

    def _plan_stages(self, outcomes: StageOutcomes) -> tuple[StageCallable, ...]:
        """Stages 15–21, shared by the heavy pipeline and the pre-dispatch replan."""

        transitions = self._transitions()
        learning = self._learning()
        planning = self._trade_planning()
        economic = self._economic_gate()
        intelligent = self._intelligent_flow()
        trade_events = self._trade_events()
        risk_ports = RiskPorts(
            evaluate_final_economic_gate=economic.evaluate_final,
            record_final_economic_gate_block=economic.record_final_block,
            portfolio_risk_engine=self.portfolio_risk_engine,
            symbol_configs=self.symbol_configs,
            record_finalized=intelligent.record_order_execution_finalized,
            journal=self.events_journal,
            log_warning=self.logger.warning,
            record_transition=transitions,
        )
        kernel_sizing, kernel_execution = build_decision_kernel_stages(
            outcomes=outcomes,
            module_modes=self.operational_module_modes,
            sizing_engine=lambda: self.smart_sizing_engine,
            execution_engine=lambda: self.smart_execution_engine,
            config=self.config,
            journal=self.events_journal,
            trace_payload=self._trace_payload,
            record_transition=transitions,
        )
        return (
            TradePlanStage(
                outcomes=outcomes,
                ports=TradePlanPorts(
                    portfolio_account_snapshot=self._portfolio_account_snapshot,
                    record_shadow_trade_plan=planning.record_shadow_trade_plan,
                    evaluate_economic_gate=economic.evaluate,
                    economic_gate_event_payload=economic_gate_event_payload,
                    planned_quantity_for_execution=planning.planned_quantity_for_execution,
                    record_learning_execution_shadow=learning.record_execution_shadow,
                    planned_exit_for_execution=planning.planned_exit_for_execution,
                    strategy_planned_exit_for_execution=planning.strategy_planned_exit_for_execution,
                    record_planned_exit_checkpoint=trade_events.record_planned_exit_checkpoint,
                    dynamic_exit_enabled=lambda: bool(self.config.dynamic_exit_enabled),
                    journal=self.events_journal,
                    trace_payload=self._trace_payload,
                    log_warning=self.logger.warning,
                    record_transition=transitions,
                ),
            ),
            IntelligentLayersStage(
                IntelligentLayersPorts(
                    record_order_shadow=intelligent.record_order_shadow,
                    record_order_comparison=intelligent.record_order_comparison,
                    record_exit_shadow=intelligent.record_exit_shadow,
                    record_exit_comparison=intelligent.record_exit_comparison,
                    evaluate_order_activation=intelligent.evaluate_order_activation,
                    record_order_activation_result=intelligent.record_order_activation_result,
                    evaluate_exit_activation=intelligent.evaluate_exit_activation,
                    record_exit_activation_result=intelligent.record_exit_activation_result,
                    apply_exit_candidate_if_economic=(
                        economic.apply_intelligent_exit_candidate_if_economic
                    ),
                    record_planned_exit_checkpoint=trade_events.record_planned_exit_checkpoint,
                    record_transition=transitions,
                )
            ),
            kernel_sizing,
            FinalRiskGuardsStage(
                outcomes=outcomes,
                ports=risk_ports,
                select_order_execution_policy=planning.select_order_execution_policy,
                strategy_name=lambda: self.strategy_coordinator.global_strategy_name,
                execution_mode=lambda: self.config.execution_mode.value,
                environment=lambda: self.config.environment.value,
            ),
            LearningExecutionStage(
                outcomes=outcomes,
                ports=risk_ports,
                evaluate_paper=learning.evaluate_paper_execution,
                record_testnet_observation=learning.record_testnet_observation,
                evaluate_testnet=learning.evaluate_testnet_execution,
                dynamic_sizing_enabled=lambda: bool(self.config.dynamic_sizing_enabled),
            ),
            kernel_execution,
            ExecutionPlannerStage(
                outcomes=outcomes,
                evaluate_plan=intelligent.evaluate_execution_plan,
                record_finalized=intelligent.record_order_execution_finalized,
                journal=self.events_journal,
                record_transition=transitions,
            ),
        )

    def _execution_stage(self, outcomes: StageOutcomes) -> ExecutionStage:
        """Stage 22, the only stage that sends orders."""

        return ExecutionStage(
            outcomes=outcomes,
            ports=ExecutionPorts(
                config=self.config,
                journal=self.events_journal,
                trace_payload=self._trace_payload,
                logger=self.logger,
                record_transition=self._transitions(),
                portfolio_account_snapshot=self._portfolio_account_snapshot,
                portfolio_risk_engine=self.portfolio_risk_engine,
                symbol_configs=self.symbol_configs,
                state_reconciliation=self._state_reconciliation_stage(outcomes),
                plan_stages=lambda: self._plan_stages(self._stage_outcomes()),
                trade_events=self._trade_events(),
                record_finalized=self._intelligent_flow().record_order_execution_finalized,
                pre_execution_guard=self.pre_execution_guard,
                execution_engine=self.execution_engine,
                execution_coordinator=getattr(self.execution_engine, "coordinator", None)
                or self.execution_engine,
                invalidate_private_state=self._cycle_snapshots().invalidate_private_state,
                count_new_position=self._count_new_position,
            ),
        )

    def _count_new_position(self) -> None:
        self._new_positions_this_cycle += 1

    def _light_context_stage(
        self,
        transitions: SymbolTransitionRecorder,
        health: SymbolHealthService,
    ) -> LightContextStage:
        get_runtime_state = getattr(self.execution_engine, "get_runtime_state", None)
        trend = self._trend_context()
        return LightContextStage(
            LightContextPorts(
                router=self.market_data_router,
                remember_operational_snapshot=self._remember_operational_snapshot,
                trend_for_market=trend.for_market,
                attach_trend=attach_trend_context,
                record_trend=trend.record_evaluated,
                position_for_symbol=self._position_for_symbol,
                runtime_state_for=get_runtime_state if callable(get_runtime_state) else None,
                strategy=self.strategy_coordinator,
                skip_score=health.strategy_skip_score,
                warmup_interval_seconds=self._warmup_interval_seconds,
                ai_gate=self._ai_gate(),
                scheduler=self.scheduler,
                score_journal=self.score_journal,
                trace_payload=self._trace_payload,
                execution_mode=self.config.execution_mode.value,
                environment=self.config.environment.value,
                record_learning_advisory=self._learning().record_advisory_shadow,
                board=self._runtime_board(),
                record_transition=transitions,
            )
        )

    def _heavy_context_stage(self, transitions: SymbolTransitionRecorder) -> HeavyContextStage:
        sync_position = getattr(self.execution_engine, "sync_position_state", None)
        user_data = self._user_data_cache()
        return HeavyContextStage(
            HeavyContextPorts(
                router=self.market_data_router,
                prefetched_snapshot_max_age_seconds=float(
                    getattr(self.config, "governor_prefetched_snapshot_max_age_seconds", 3.0)
                ),
                prepare_symbol_snapshot=self._cycle_snapshots().prepare_symbol_evaluation,
                user_data=UserDataCachePorts(
                    record_stale_guard=user_data.record_stale_guard,
                    record_usage_shadow=user_data.record_usage_shadow,
                    record_assisted_plan_shadow=user_data.record_assisted_plan_shadow,
                    cached_position=user_data.cached_position,
                    cached_account_info=user_data.cached_account_info,
                    cached_open_orders=user_data.cached_open_orders,
                    record_runtime_metrics=user_data.record_runtime_metrics,
                    record_shadow_audit=user_data.record_shadow_audit,
                    record_reconciliation=user_data.record_reconciliation,
                ),
                journal=self.events_journal,
                trace_payload=self._trace_payload,
                strategy_name=lambda: self.strategy_coordinator.global_strategy_name,
                remember_operational_snapshot=self._remember_operational_snapshot,
                sync_position=sync_position if callable(sync_position) else None,
                attach_trend=attach_trend_context,
                record_adaptive_management=self._adaptive_management().record_shadow,
                record_transition=transitions,
            )
        )

    def _state_reconciliation_stage(self, outcomes: StageOutcomes) -> StateReconciliationStage:
        return StateReconciliationStage(
            outcomes=outcomes,
            engine=self.state_reconciliation_engine,
            module_modes=self.operational_module_modes,
            journal=self.events_journal,
            trace_payload=self._trace_payload,
            record_transition=self._transitions(),
        )

    def _risk_guard_factory(self) -> Callable[[SymbolTradingConfig], Any]:
        """The execution engine's risk guard per symbol (also for test engines)."""

        risk_guard_factory: Callable[[SymbolTradingConfig], Any] | None = getattr(
            self.execution_engine, "risk_guard_for_symbol", None
        )
        if callable(risk_guard_factory):
            return risk_guard_factory
        return self._risk_guard_for_symbol

    def _symbol_warmup_stage(
        self,
        phase: Phase,
        outcomes: StageOutcomes,
        health: SymbolHealthService,
    ) -> SymbolWarmupStage:
        return SymbolWarmupStage(
            phase=phase,
            outcomes=outcomes,
            journal=self.events_journal,
            trace_payload=self._trace_payload,
            evaluator=health.warmup_evaluator,
            min_observed_prices=health.warmup_min_observed_prices,
        )

    def _symbol_quarantine_stage(
        self,
        phase: Phase,
        outcomes: StageOutcomes,
        health: SymbolHealthService,
    ) -> QuarantineStage:
        return QuarantineStage(
            phase=phase,
            outcomes=outcomes,
            journal=self.events_journal,
            trace_payload=self._trace_payload,
            quarantine=health.quarantine_manager,
        )

    def _guard_stage(self, phase: Phase, outcomes: StageOutcomes) -> DefensiveAndAIGuardStage:
        return DefensiveAndAIGuardStage(
            phase=phase,
            outcomes=outcomes,
            ai_gate=self._ai_gate(),
            defensive_mode=self._defensive_mode_service(),
            regime_policy_mode=self._trend_context().mode,
        )

    # --- cycle ----------------------------------------------------------------------------

    def run_cycle(self, *, now: float | None = None) -> None:
        CycleCoordinator(self._cycle_ports(), clock=time.time).run(now=now)

    def _cycle_ports(self) -> CyclePorts:
        snapshots = self._cycle_snapshots()
        telemetry = self._cycle_telemetry()
        fast_lane = self._fast_lane()
        learning = self._learning()
        evaluator = self._symbol_evaluator()
        return CyclePorts(
            next_cycle_id=self._next_cycle_id,
            cycle_scope=self._cycle_scope,
            start_cycle_context=self._start_cycle_context,
            build_request_budget=self._request_budget_service().build_budget,
            request_budget_summary=RequestBudgetService.summary,
            symbol_configs=self.symbol_configs,
            collect_session_health=snapshots.collect_health,
            close_session=snapshots.close,
            consume_symbol_control_requests=self._consume_symbol_control_requests,
            prepare_session_baseline=snapshots.prepare_baseline,
            refresh_candle_data_hub=self._refresh_candle_data_hub,
            audit_open_positions=self._audit_open_positions,
            drain_dirty_symbols=telemetry.drain_dirty_symbols,
            drain_shock_events=telemetry.drain_shock_events,
            record_shock_events=telemetry.record_shock_events,
            fast_lane_symbols_from_shocks=fast_lane.symbols_from_shocks,
            market_health_snapshot=telemetry.market_health,
            defensive_mode=self._defensive_mode_service(),
            defensive_mode_active=lambda: self._last_defensive_mode_active,
            set_defensive_mode_active=self._set_defensive_mode_active,
            max_symbols_per_cycle=self.max_symbols_per_cycle,
            rank_due_symbols=self._opportunity_service().rank_due_symbols,
            record_fast_lane_prioritization=fast_lane.record_prioritization,
            prepare_selected_snapshot=snapshots.prepare_selected,
            is_stopped=lambda symbol: symbol in self.stopped_symbols,
            is_paused=lambda symbol: symbol in self.paused_symbols,
            set_disabled_snapshot=self._set_disabled_snapshot,
            prepare_symbol_evaluation=evaluator.prepare,
            record_symbol_error=self._record_symbol_error,
            prefetch_heavy_snapshots=self._heavy_prefetcher().prefetch,
            resume_symbol_evaluation=evaluator.resume,
            write_state=self._write_state,
            record_learning_recommendations=learning.record_recommendations,
            record_learning_shadow_impact_report=learning.record_shadow_impact_report,
            record_cycle_metrics=telemetry.record_cycle_metrics,
            slow_cycle_threshold_ms=telemetry.slow_cycle_threshold_ms,
        )

    @contextmanager
    def _cycle_scope(self, cycle_id: str) -> Iterator[None]:
        """Publish the cycle id to the trace payload; restore the previous ids after."""

        previous_cycle_id = self._current_cycle_id
        previous_symbol_eval_id = self._current_symbol_eval_id
        self._current_cycle_id = cycle_id
        self._current_symbol_eval_id = ""
        try:
            yield
        finally:
            self._current_cycle_context = None
            self._current_cycle_id = previous_cycle_id
            self._current_symbol_eval_id = previous_symbol_eval_id

    def _start_cycle_context(self, context: CycleContext) -> None:
        self._current_cycle_context = context
        self._new_positions_this_cycle = 0
        begin_cycle = getattr(self.strategy_coordinator, "begin_cycle", None)
        if callable(begin_cycle):
            begin_cycle()

    def _set_defensive_mode_active(self, active: bool) -> None:
        self._last_defensive_mode_active = active

    def _set_disabled_snapshot(
        self,
        symbol_config: SymbolTradingConfig,
        status: SymbolRuntimeStatus,
    ) -> None:
        self._runtime_board().set_disabled(symbol_config.symbol, status)

    def _wait_until_next_cycle(self) -> None:
        self.market_wakeup.wait_for_next_cycle(
            journal=self.events_journal,
            heartbeat=lambda: self._cycle_snapshots().collect_health("heartbeat"),
        )

    def _consume_control_requests(self) -> bool:
        if self.control_requests.consume_governor_stop():
            self._running = False
            return True

        self._consume_symbol_control_requests()
        return False

    def _consume_symbol_control_requests(self) -> None:
        self.control_requests.consume_symbol_requests(
            symbol_configs=self.symbol_configs,
            paused_symbols=self.paused_symbols,
            stopped_symbols=self.stopped_symbols,
        )

    def _record_runtime_effective_config_snapshot(self) -> None:
        safe_append(
            self.events_journal,
            RUNTIME_EFFECTIVE_CONFIG_SNAPSHOT,
            logger=self.logger,
            **runtime_effective_config_payload(
                self.config,
                operational_mode=self.operational_mode,
                symbols=[item.symbol for item in self.symbol_configs],
                strategy=self.strategy_coordinator,
                governor_events_path=self.governor_events_path,
            ),
        )

    def start(self) -> None:
        self._running = True
        observe_event(
            "governor_starting",
            component="governor.alphora_governor",
            category="governor",
            execution_mode=self.config.execution_mode.value,
            environment=self.config.environment.value,
            symbols=[item.symbol for item in self.symbol_configs],
            strategy=self.strategy_coordinator.global_strategy_name,
        )
        observe_event(
            "run_started",
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            execution_mode=self.config.execution_mode.value,
            environment=self.config.environment.value,
            symbols=[item.symbol for item in self.symbol_configs],
            strategy=self.strategy_coordinator.global_strategy_name,
        )
        self.events_journal.append(
            GOVERNOR_STARTED,
            execution_mode=self.config.execution_mode.value,
            environment=self.config.environment.value,
            operational_mode=self.operational_mode,
            symbols=[item.symbol for item in self.symbol_configs],
            global_strategy=self.strategy_coordinator.global_strategy_name,
            strategy_runtime_mode=self.strategy_coordinator.runtime_mode,
            strategy_state_mode=self.strategy_coordinator.state_mode,
        )
        self._record_runtime_effective_config_snapshot()
        self.logger.info(
            "Alphora Governor iniciado | modo=%s | estrategia_global=%s | "
            "strategy_runtime_mode=%s | state=%s | simbolos=%s",
            self.operational_mode,
            self.strategy_coordinator.global_strategy_name,
            self.strategy_coordinator.runtime_mode,
            self.strategy_coordinator.state_mode,
            ", ".join(item.symbol for item in self.symbol_configs),
        )
        self.execution_engine.acquire_symbol_locks(self.symbol_configs)
        register_event_sink(self.stage_telemetry)
        try:
            self._start_market_data_hub()
            self._user_data_cache().start_stream()
            self._bootstrap_exchange_info()
            self._warmup_market_data_hub()
            observe_event(
                "governor_started",
                component="governor.alphora_governor",
                category="governor",
                operational_mode=self.operational_mode,
                symbols=[item.symbol for item in self.symbol_configs],
            )
            for symbol_config in self.symbol_configs:
                if self.config.recover_on_boot:
                    self._safe_recover(symbol_config)
                    if self._symbol_has_error(symbol_config.symbol):
                        continue
                if self.config.mutations_enabled:
                    self._safe_configure_symbol_exchange(symbol_config)

            while self._running:
                if self._consume_control_requests():
                    break

                self.market_wakeup.clear()
                self.run_cycle()

                if self._consume_control_requests():
                    break

                self._wait_until_next_cycle()
        finally:
            self._running = False
            unregister_event_sink(self.stage_telemetry)
            observe_event(
                "governor_shutdown_started",
                component="governor.alphora_governor",
                category="governor",
            )
            try:
                self._write_final_state_safely()
                self._user_data_cache().stop_stream()
                self._stop_market_data_hub()
                self._stop_snapshot_provider()
                self.execution_engine.release_symbol_locks()
                self.events_journal.append("governor_stopped")
                observe_event(
                    "run_finished",
                    component=__name__,
                    category="governor",
                    channel=EventChannel.AUDIT,
                )
            except Exception as error:
                observe_error(
                    "governor_shutdown_failed",
                    error=error,
                    component="governor.alphora_governor",
                    category="governor",
                )
                raise
            else:
                observe_event(
                    "governor_shutdown_completed",
                    component="governor.alphora_governor",
                    category="governor",
                )

    def _bootstrap_exchange_info(self) -> None:
        provider = self.snapshot_provider

        if provider is None:
            return

        try:
            result = provider.bootstrap_exchange_info()
        except Exception as error:
            diagnostics = snapshot_error_diagnostics_payload(error)

            self.logger.warning(
                "Falha ao carregar exchangeInfo no bootstrap; "
                "o fallback operacional permanecera ativo: %s",
                error,
            )

            self.events_journal.append(
                wire_names.EXCHANGE_INFO_BOOTSTRAP_FAILED,
                status="DEGRADED",
                reason=str(error),
                error_type=type(error).__name__,
                diagnostics=diagnostics,
            )

            observe_event(
                wire_names.EXCHANGE_INFO_BOOTSTRAP_FAILED,
                component=__name__,
                category="governor",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                reason=str(error),
                error_type=type(error).__name__,
                diagnostics=diagnostics,
            )
            observe_error(
                "snapshot_build_failed",
                error=error,
                component="market_data.snapshot_provider",
                category="market_data",
                resource="exchange:info",
                source=wire_names.EXCHANGE_INFO_BOOTSTRAP_ERROR_SOURCE,
                error_type=type(error).__name__,
                message=str(error),
                attempts=getattr(
                    getattr(provider, "settings", None),
                    "exchange_info_bootstrap_attempts",
                    None,
                ),
                partial_snapshot_available=bool(getattr(error, "snapshot", None)),
                diagnostics=diagnostics,
            )
            return

        status = "STALE" if result.stale else "READY"
        severity = "WARNING" if result.stale else "INFO"

        event_payload = {
            "status": status,
            "attempts": result.attempts,
            "duration_ms": result.duration_ms,
            "symbol_count": result.symbol_count,
            "from_cache": result.from_cache,
            "stale": result.stale,
        }

        self.events_journal.append(
            wire_names.EXCHANGE_INFO_BOOTSTRAPPED,
            **event_payload,
        )

        observe_event(
            wire_names.EXCHANGE_INFO_BOOTSTRAPPED,
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            severity=severity,
            **event_payload,
        )

    def _start_market_data_hub(self) -> None:
        if self.market_data_hub is None:
            return
        self.market_data_hub.start()

    def _warmup_market_data_hub(self) -> None:
        if self.market_data_hub is None:
            return
        warmup_seconds = float(self.config.market_ws_warmup_seconds)
        if warmup_seconds <= 0:
            return

        deadline = time.time() + warmup_seconds
        symbols = [item.symbol for item in self.symbol_configs]
        while time.time() < deadline:
            now = time.time()
            if all(self.market_data_hub.has_recent_price(symbol, now=now) for symbol in symbols):
                return
            time.sleep(min(0.05, max(0.0, deadline - now)))

    def _stop_market_data_hub(self) -> None:
        if self.market_data_hub is None:
            return
        try:
            self.market_data_hub.stop()
        except Exception as error:
            self.logger.warning("Falha ao parar WebSocket de mercado: %s", error)

    def _write_final_state_safely(self) -> None:
        try:
            self._write_state(cached_portfolio_only=True)
        except Exception as error:
            self.logger.warning(
                "Failed to write final Governor state during shutdown: %s",
                error,
            )
            try:
                observe_event(
                    "final_state_write_failed",
                    component=__name__,
                    category="governor",
                    channel=EventChannel.AUDIT,
                    severity="WARNING",
                    reason=str(error),
                    error_type=type(error).__name__,
                )
            except Exception as audit_error:
                self.logger.warning(
                    "Failed to record final state write failure: %s",
                    audit_error,
                )

    def _stop_snapshot_provider(self) -> None:
        provider = self.snapshot_provider
        if provider is None:
            return
        try:
            provider.close()
        except Exception as error:
            self.logger.warning("Falha ao parar o provider de snapshots: %s", error)

    def _position_for_symbol(
        self,
        symbol_config: SymbolTradingConfig,
        *,
        context: CycleContext | None = None,
    ) -> FuturesPosition:
        context = context or self._current_cycle_context
        if context is not None and symbol_config.symbol in context.positions:
            return context.positions[symbol_config.symbol]

        position = self.execution_engine.get_position(symbol_config)
        if context is not None:
            context.positions[symbol_config.symbol] = position
        return position

    def _portfolio_account_snapshot(
        self,
        *,
        context: CycleContext | None = None,
    ) -> dict[str, Any]:
        return self.portfolio_snapshot_provider.build(
            context=context,
            new_positions_this_cycle=self._new_positions_this_cycle,
        )

    def _audit_open_positions(self, *, context: CycleContext | None = None) -> set[str]:
        open_symbols: set[str] = set()
        for symbol_config in self.symbol_configs:
            if symbol_config.symbol in self.paused_symbols:
                continue
            try:
                position = self._position_for_symbol(symbol_config, context=context)
                still_open = self.execution_engine.audit_symbol_protections(
                    symbol_config,
                    position=position,
                )
                if still_open:
                    open_symbols.add(symbol_config.symbol)
                elif position.is_open and context is not None:
                    # Closed during the audit (e.g. simulated paper stop/take):
                    # drop the stale cached view for the rest of the cycle.
                    context.positions.pop(symbol_config.symbol, None)
            except Exception as error:
                self._record_symbol_error(symbol_config, error)
        return open_symbols

    def _safe_recover(self, symbol_config: SymbolTradingConfig) -> None:
        try:
            self.execution_engine.recover_symbol(symbol_config)
        except Exception as error:
            self._record_symbol_error(symbol_config, error)

    def _safe_configure_symbol_exchange(self, symbol_config: SymbolTradingConfig) -> None:
        try:
            self.execution_engine.configure_symbol_exchange_safely(symbol_config)
        except Exception as error:
            self._record_symbol_error(symbol_config, error)

    def _symbol_has_error(self, symbol: str) -> bool:
        snapshot = self.snapshots.get(symbol)
        return snapshot is not None and snapshot.status is SymbolRuntimeStatus.ERROR

    def _safe_account_info(
        self,
        *,
        context: CycleContext | None = None,
    ) -> dict[str, Any] | None:
        context = context or self._current_cycle_context
        if context is not None and context.account_info_loaded:
            return context.account_info

        now = time.time()
        try:
            get_account_info = getattr(self.market_data_router, "get_account_info", None)
            account_info = (
                get_account_info(now=now)
                if callable(get_account_info)
                else self.client.account_info()
            )
        except Exception as error:
            self.logger.error(
                "%s Erro original: %s | Como corrigir: %s",
                BALANCE_UNAVAILABLE_MESSAGE,
                error,
                BALANCE_UNAVAILABLE_FIX_HINT,
            )
            self.events_journal.append(
                "account_info_failed",
                reason=str(error),
                motivo_pt=BALANCE_UNAVAILABLE_MESSAGE,
                como_corrigir=BALANCE_UNAVAILABLE_FIX_HINT,
                acao_de_seguranca=BALANCE_UNAVAILABLE_SAFETY_ACTION,
                bloqueia_novas_entradas=True,
                bloqueia_fechamentos=False,
                execution_mode=self.config.execution_mode.value,
                environment=self.config.environment.value,
            )
            if context is not None:
                context.account_info = None
                context.account_info_loaded = True
            return None

        self._remember_operational_snapshot(context)
        if context is not None:
            context.account_info = account_info
            context.account_info_loaded = True
        return account_info

    def _remember_operational_snapshot(self, context: CycleContext | None = None) -> None:
        if context is None:
            context = self._current_cycle_context
        if context is None:
            return
        snapshot = getattr(self.market_data_router, "last_operational_snapshot", None)
        if snapshot is not None:
            context.operational_snapshot = snapshot

    def _warmup_interval_seconds(self) -> float:
        if self.config.environment.value == "REAL":
            return 12.0
        return 5.0

    def _daily_portfolio_metrics(
        self,
        *,
        context: CycleContext | None = None,
    ) -> dict[str, Any]:
        return self.daily_metrics_reader.read(context=context)

    def _portfolio_state_for_write(
        self,
        *,
        context: CycleContext | None = None,
        cached_only: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = time.time()

        if not cached_only:
            portfolio_snapshot = self.portfolio_risk_engine.snapshot(
                self._portfolio_account_snapshot(context=context)
            )
            portfolio_payload = asdict(portfolio_snapshot)
            captured_at = float(portfolio_snapshot.timestamp)

            self._last_portfolio_state_payload = deepcopy(portfolio_payload)
            self._last_portfolio_state_captured_at = captured_at

            return portfolio_payload, {
                "available": True,
                "source": "CURRENT_SNAPSHOT",
                "captured_at": captured_at,
                "age_seconds": max(0.0, now - captured_at),
            }

        if self._last_portfolio_state_payload is not None:
            last_captured_at = self._last_portfolio_state_captured_at
            age_seconds = None if last_captured_at is None else max(0.0, now - last_captured_at)

            return deepcopy(self._last_portfolio_state_payload), {
                "available": True,
                "source": "LAST_CONFIRMED_SNAPSHOT",
                "captured_at": last_captured_at,
                "age_seconds": age_seconds,
            }

        unavailable_snapshot = self.portfolio_risk_engine.snapshot(
            {
                "open_positions": [],
                "account_info_failed": True,
                "blocked_symbols": [
                    "FINAL_STATE_PORTFOLIO_UNAVAILABLE",
                ],
            }
        )
        unavailable_payload = asdict(unavailable_snapshot)

        # Zero means that no live portfolio snapshot was captured in this run.
        unavailable_payload["timestamp"] = 0.0

        return unavailable_payload, {
            "available": False,
            "source": "UNAVAILABLE",
            "captured_at": None,
            "age_seconds": None,
        }

    def _write_state(
        self,
        *,
        context: CycleContext | None = None,
        cached_portfolio_only: bool = False,
    ) -> None:
        portfolio_payload, portfolio_metadata = self._portfolio_state_for_write(
            context=context,
            cached_only=cached_portfolio_only,
        )

        payload = {
            "updated_at": time.time(),
            "process_memory": process_memory_snapshot(),
            "execution_mode": self.config.execution_mode.value,
            "environment": self.config.environment.value,
            "operational_mode": self.operational_mode,
            "strategy_runtime_mode": self.strategy_coordinator.runtime_mode,
            "global_strategy": self.strategy_coordinator.global_strategy_name,
            "strategy_state_mode": self.strategy_coordinator.state_mode,
            "status_summary": self._status_summary(),
            "symbols": {
                symbol: asdict(snapshot) for symbol, snapshot in sorted(self.snapshots.items())
            },
            "portfolio": portfolio_payload,
            "portfolio_metadata": portfolio_metadata,
        }

        self.state_writer.write(payload)

    @property
    def operational_mode(self) -> str:
        if len(self.symbol_configs) == 1:
            return "GOVERNOR_SINGLE_SYMBOL"
        return "GOVERNOR_MULTI_SYMBOL"

    def _status_summary(self) -> str:
        if not self.snapshots:
            return f"{self.operational_mode}: aguardando primeira avaliacao"
        counts: dict[str, int] = {}
        for snapshot in self.snapshots.values():
            counts[snapshot.status.value] = counts.get(snapshot.status.value, 0) + 1
        summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        return f"{self.operational_mode}: {summary}"
