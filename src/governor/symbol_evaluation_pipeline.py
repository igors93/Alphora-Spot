"""Typed state shared by the symbol evaluation stages.

``SymbolEvaluationContext`` is filled stage by stage. Each field documents the
stage that writes it (numbers follow the pipeline order in the migration plan);
every other stage only reads it. Stage 22 revalidates before dispatch and may
refresh light/heavy data or replan (``reset_plan_for_replan`` then stages 15–21
again); those refreshes are listed next to the field. The frozen pass-through types at the bottom
(``LightEvaluation`` … ``ExecutableTrade``) are the hand-off between phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from domain.models import FuturesPosition, TradeDecision
from domain.opportunity import TradeOpportunity
from governor.market_data_router import HeavyMarketSnapshot, LightMarketSnapshot
from governor.models import SymbolRuntimeStatus
from scanner.priority_scheduler import ScheduledSymbol
from scanner.signal_score import SignalScore
from trading.intent import TradeIntent
from trading.plan import TradePlan

if TYPE_CHECKING:
    from config.symbol_context import SymbolTradingConfig
    from execution.state_reconciliation import ReconciliationDecision
    from futures.execution.order_policy import OrderExecutionPlan
    from futures.planning.economic_gate import EconomicGateDecision
    from futures.planning.trade_plan_execution_validator import TradePlanExecutionValidation
    from futures.protection import ExpectedProtection
    from futures.state import RuntimeState
    from futures.trend import TrendContext
    from governor.cycle_context import CycleContext
    from governor.execution_engine import ExecutionCommand
    from governor.hot_symbol_worker import GuardDecision
    from governor.models import GovernorDecision
    from governor.pre_execution_guard import PreExecutionGuardDecision
    from governor.request_budget import RequestBudgetDecision
    from governor.smart_execution import SmartExecutionDecision
    from governor.smart_sizing import SmartSizingDecision
    from governor.trade_pipeline import TradePipelineResult
    from trading.intelligent_execution_planner import IntelligentExecutionPlan
    from trading.intelligent_exit_activation import IntelligentExitActivationDecision
    from trading.intelligent_exit_builder import IntelligentExitPlan
    from trading.intelligent_order_activation import IntelligentOrderActivationDecision
    from trading.intelligent_order_builder import IntelligentOrderPlan
    from trading.learning_advisory import LearningAdvisoryEvaluation
    from trading.learning_execution_shadow import LearningExecutionShadowDecision
    from trading.learning_paper_execution import LearningPaperExecutionDecision
    from trading.learning_testnet_execution import LearningTestnetExecutionDecision

PlannedExit = dict[str, Any]
"""Stop/take payload chosen for execution (strategy metadata, economic or dynamic)."""


@dataclass
class SymbolEvaluationInput:
    symbol_config: SymbolTradingConfig
    now: float
    cycle_id: str
    symbol_eval_id: str


@dataclass
class SymbolEvaluationStageResult:
    stage: str
    classification: str
    reason: str
    terminal: bool
    execution_will_run: bool = False
    action: str | None = None
    status: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class SymbolEvaluationContext:
    evaluation_input: SymbolEvaluationInput
    # Set when the evaluation is created, from the cycle that owns it.
    cycle_context: CycleContext | None = None

    # --- Light phase (stages 1–8) -------------------------------------------------
    # 1 light context
    light: LightMarketSnapshot | None = None
    # 1 light context
    position: FuturesPosition | None = None
    # 1 light context
    runtime_state: RuntimeState | None = None
    # 1 light context
    score: SignalScore | None = None
    # 1 light context
    trend_context: TrendContext | None = None
    # 1 light context
    scheduled: ScheduledSymbol | None = None
    # 1 light context
    learning_advisory: LearningAdvisoryEvaluation | None = None
    # 1 light context
    status: SymbolRuntimeStatus | None = None
    # 1 light context
    warmup: bool = False
    # 1 light context
    operational_state: str = ""
    # 1 light context
    protection_warning: str = ""
    # 1 light context
    ai_audit: dict[str, Any] = field(default_factory=dict)
    # 8 request budget
    budget_decision: RequestBudgetDecision | None = None

    # --- Heavy phase (stages 9–14) ------------------------------------------------
    # 9 heavy context; refreshed by 22 when execution revalidates the snapshot
    router_heavy: HeavyMarketSnapshot | None = None
    # 9 heavy context (router snapshot + synced position); refreshed by 22
    heavy: HeavyMarketSnapshot | None = None
    # 10 hot symbol decision
    trade_decision: TradeDecision | None = None
    # 10 hot symbol decision
    hot_result: GuardDecision | None = None
    # 10 hot symbol decision; price refreshed by 22 (pre-dispatch check)
    intent: TradeIntent | None = None
    # 11 state reconciliation
    state_reconciliation_decision: ReconciliationDecision | None = None

    # --- Plan phase (stages 15–17) ------------------------------------------------
    # 15 trade plan
    trade_pipeline_result: TradePipelineResult | None = None
    # 15 trade plan (operable opportunity produced by TradingPolicy)
    opportunity: TradeOpportunity | None = None
    # 15 trade plan
    economic_gate_decision: EconomicGateDecision | None = None
    # 15 trade plan; 21 execution planner
    trade_plan_id: str | None = None
    # 15 trade plan
    learning_execution_decision: LearningExecutionShadowDecision | None = None
    # 15 trade plan, 16 intelligent layers
    planned_exit: PlannedExit | None = None
    # 15 trade plan, 16 intelligent layers
    planned_exit_reason: str = ""
    # 16 intelligent layers, 17 smart sizing, 19 learning execution
    planned_quantity: Decimal | None = None
    # 15 trade plan, 16 intelligent layers, 17 smart sizing, 19 learning execution
    planned_quantity_reason: str = ""
    # 16 intelligent layers
    intelligent_order_plan: IntelligentOrderPlan | None = None
    # 16 intelligent layers
    intelligent_exit_plan: IntelligentExitPlan | None = None
    # 16 intelligent layers
    intelligent_order_activation: IntelligentOrderActivationDecision | None = None
    # 16 intelligent layers
    intelligent_exit_activation: IntelligentExitActivationDecision | None = None
    # 17 smart sizing
    smart_sizing_decision: SmartSizingDecision | None = None

    # --- Risk and execution phase (stages 18–22) ----------------------------------
    # 18 final guards; 19 when learning changes the payload
    final_economic_validation: TradePlanExecutionValidation | None = None
    # 18 final guards
    order_execution_plan: OrderExecutionPlan | None = None
    # 18 final guards; 19 when learning changes the payload; 22 revalidation
    portfolio_decision: GovernorDecision | None = None
    # 15 trade plan; refreshed by 22 revalidation
    account_snapshot: dict[str, Any] | None = None
    # 19 learning execution
    learning_paper_execution: LearningPaperExecutionDecision | None = None
    # 19 learning execution
    learning_testnet_execution: LearningTestnetExecutionDecision | None = None
    # 19 learning execution
    learning_changed_execution_payload: bool = False
    # 19 learning execution
    learning_payload_revalidated: bool = False
    # 20 smart execution
    smart_execution_decision: SmartExecutionDecision | None = None
    # 21 execution planner
    intelligent_execution_plan: IntelligentExecutionPlan | None = None
    # 22 execution (revalidation)
    pre_execution_guard_decision: PreExecutionGuardDecision | None = None
    # 22 execution (revalidation)
    effective_protection_plan: ExpectedProtection | None = None
    # 22 execution (pre-dispatch price check)
    pre_dispatch_price_decision: dict[str, Any] = field(default_factory=dict)
    # 22 execution (dispatch)
    execution_command: ExecutionCommand | None = None

    # --- Frozen hand-off between phases -------------------------------------------
    # 1 light context
    light_evaluation: LightEvaluation | None = None
    # 9 heavy context; refreshed by 22 revalidation
    heavy_evaluation: HeavyEvaluation | None = None
    # 10 hot symbol decision; refreshed by 22
    intent_candidate: IntentCandidate | None = None
    # 15 trade plan; refreshed by 22 revalidation
    planned_trade: PlannedTrade | None = None
    # 22 execution
    validated_trade: ValidatedTrade | None = None
    # 22 execution
    executable_trade: ExecutableTrade | None = None

    # --- Runner bookkeeping -------------------------------------------------------
    diagnostics: dict[str, Any] = field(default_factory=dict)
    terminal_result: SymbolEvaluationResult | None = None
    lifecycle_started_at: float = 0.0
    lifecycle_finished: bool = False

    def reset_plan_for_replan(self) -> None:
        """Clear everything stages 15–22 produced, before the pre-dispatch replan."""

        self.trade_pipeline_result = None
        self.opportunity = None
        self.economic_gate_decision = None
        self.planned_quantity = None
        self.planned_quantity_reason = ""
        self.planned_exit = None
        self.planned_exit_reason = ""
        self.final_economic_validation = None
        self.order_execution_plan = None
        self.smart_sizing_decision = None
        self.smart_execution_decision = None
        self.portfolio_decision = None
        self.intelligent_order_plan = None
        self.intelligent_exit_plan = None
        self.intelligent_order_activation = None
        self.intelligent_exit_activation = None
        self.intelligent_execution_plan = None
        self.learning_execution_decision = None
        self.learning_paper_execution = None
        self.learning_testnet_execution = None
        self.learning_changed_execution_payload = False
        self.learning_payload_revalidated = False
        self.pre_execution_guard_decision = None
        self.effective_protection_plan = None
        self.planned_trade = None
        self.validated_trade = None
        self.executable_trade = None


@dataclass(frozen=True)
class LightEvaluation:
    evaluation_input: SymbolEvaluationInput
    light: LightMarketSnapshot
    position: FuturesPosition
    runtime_state: RuntimeState | None
    score: SignalScore
    scheduled: ScheduledSymbol
    status: SymbolRuntimeStatus
    warmup: bool
    operational_state: str = ""
    protection_warning: str = ""


@dataclass(frozen=True)
class HeavyEvaluation:
    light_evaluation: LightEvaluation
    heavy: HeavyMarketSnapshot


@dataclass(frozen=True)
class IntentCandidate:
    heavy_evaluation: HeavyEvaluation
    decision: TradeDecision | None
    intent: TradeIntent


@dataclass(frozen=True)
class PlannedTrade:
    candidate: IntentCandidate
    plan: TradePlan
    trade_plan_id: str
    opportunity: TradeOpportunity | None = None


@dataclass(frozen=True)
class ValidatedTrade:
    planned_trade: PlannedTrade
    portfolio_decision: GovernorDecision | None
    final_economic_validation: TradePlanExecutionValidation | None


@dataclass(frozen=True)
class ExecutableTrade:
    validated_trade: ValidatedTrade
    execution_command: ExecutionCommand


@dataclass
class SymbolEvaluationResult:
    symbol: str
    action: str
    status: str
    terminal: bool
    execution_will_run: bool
    reason: str
    stage: str
    classification: str
    planned_quantity: Decimal | None = None
    planned_exit: PlannedExit | None = None
    trade_plan_id: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
