"""Trade planning: the shadow trade plan and what of it reaches the execution payload."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from config.symbol_context import SymbolTradingConfig
from futures.execution.order_policy import OrderExecutionPolicy
from futures.planned_exit import (
    GLOBAL_FALLBACK_EXIT_SOURCE,
    STRATEGY_METADATA_EXIT_SOURCE,
    extract_strategy_planned_exit,
    has_strategy_planned_exit_metadata,
    resolve_strategy_planned_exit,
)
from futures.planning.economic_gate import EconomicGateDecision
from governor.evaluation.outcomes import EventJournal
from governor.event_sink import EventSink
from governor.events import TRADE_PLAN_SHADOW_CREATED
from governor.governor_trade_helpers import (
    EXECUTION_OPEN_ACTIONS,
    EXECUTION_PLAN_ALLOWED_STATUSES,
    _economic_adjusted_exit_for_execution,
)
from governor.services.learning import LearningService
from trading.learning_advisory import LearningAdvisoryEvaluation
from utils.events import EventChannel, observe_event

TracePayload = Callable[[], Mapping[str, str]]
PlannedExit = dict[str, Any] | None


def spread_bps_for_order_policy(trade_pipeline_result: Any) -> Decimal | None:
    """The spread of the plan context, or the spread the cost model assumed."""

    if trade_pipeline_result is None:
        return None

    context = trade_pipeline_result.context
    if context.spread_pct is not None:
        spread: Decimal = abs(context.spread_pct) * Decimal("10000")
        return spread

    plan = trade_pipeline_result.plan
    diagnostics = plan.diagnostics if isinstance(plan.diagnostics, dict) else {}
    cost = diagnostics.get("cost", {})
    if isinstance(cost, dict):
        rates = cost.get("rates", {})
        if isinstance(rates, dict) and rates.get("spread_bps") is not None:
            try:
                return Decimal(str(rates["spread_bps"]))
            except Exception:
                return None
    return None


class TradePlanningService:
    """Facade of stages 15–17: build the shadow plan and derive the execution inputs.

    Planned quantity and exit only reach the order when dynamic sizing or dynamic
    exits are enabled and the plan is executable; otherwise the reason is returned.
    """

    def __init__(
        self,
        *,
        config: Any,
        journal: EventJournal,
        pipeline_events: EventSink,
        trade_pipeline: Any,
        order_execution_policy: OrderExecutionPolicy,
        learning: LearningService,
        strategy_name: Callable[[], str],
        trace_payload: TracePayload,
    ) -> None:
        self.config = config
        self.journal = journal
        self.pipeline_events = pipeline_events
        self.trade_pipeline = trade_pipeline
        self.order_execution_policy = order_execution_policy
        self.learning = learning
        self.strategy_name = strategy_name
        self.trace_payload = trace_payload

    # --- shadow plan (stage 15) -----------------------------------------------------------

    def record_shadow_trade_plan(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        score: Any,
        intent: Any,
        heavy: Any,
        account_snapshot: dict[str, Any],
        learning_advisory: LearningAdvisoryEvaluation | None = None,
    ) -> Any:
        if self.trade_pipeline is None:
            return None
        try:
            return self._build_shadow_plan(
                symbol_config=symbol_config,
                score=score,
                intent=intent,
                heavy=heavy,
                account_snapshot=account_snapshot,
                learning_advisory=learning_advisory,
            )
        except Exception as error:
            self.journal.append(
                "trade_plan_shadow_failed",
                symbol=symbol_config.symbol,
                strategy=self.strategy_name(),
                action=getattr(intent, "action", ""),
                status="SHADOW_FAILED",
                diagnostics={"error": str(error)},
            )
            return None

    def _build_shadow_plan(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        score: Any,
        intent: Any,
        heavy: Any,
        account_snapshot: dict[str, Any],
        learning_advisory: LearningAdvisoryEvaluation | None,
    ) -> Any:
        emit = self.pipeline_events.emit
        trace: dict[str, Any] = dict(self.trace_payload())
        emit(
            "trade_intent_created",
            **trace,
            symbol=symbol_config.symbol,
            strategy=intent.strategy_name,
            action=intent.action,
            intent_id=intent.intent_id,
            status="SHADOW",
            diagnostics={"confidence": intent.confidence, "reason": intent.reason},
        )
        try:
            result = self.trade_pipeline.build_shadow_plan(
                intent=intent,
                symbol_config=symbol_config,
                heavy_snapshot=heavy,
                portfolio_snapshot=account_snapshot,
                score=score,
            )
        except TypeError:
            result = self.trade_pipeline.build_shadow_plan(
                intent=intent,
                symbol_config=symbol_config,
                heavy_snapshot=heavy,
                portfolio_snapshot=account_snapshot,
            )
        result = self.learning.attach_advisory_to_trade_plan(
            result=result,
            learning_advisory=learning_advisory,
        )
        plan = result.plan
        plan_fields = {
            "symbol": symbol_config.symbol,
            "strategy": plan.strategy_name,
            "action": plan.action,
            "intent_id": intent.intent_id,
            "status": plan.status,
            "plan_id": plan.plan_id,
        }
        emit(
            TRADE_PLAN_SHADOW_CREATED,
            **trace,
            **plan_fields,
            plan_version=plan.plan_version,
            source=plan.source,
            created_at=plan.created_at,
            trade_plan=plan,
            diagnostics=plan.diagnostics,
        )
        observe_event(
            "trade_plan_created",
            component=__name__,
            category="execution",
            channel=EventChannel.AUDIT,
            **trace,
            **plan_fields,
            trade_plan_id=plan.plan_id,
        )
        emit(
            "trade_plan_cost_diagnostics",
            **plan_fields,
            plan_version=plan.plan_version,
            source=plan.source,
            cost_summary=plan.diagnostics.get("cost_summary", {}),
            diagnostics=plan.diagnostics.get("cost", {}),
        )
        comparison = result.comparison
        emit(
            "trade_plan_shadow_compared",
            **plan_fields,
            plan_version=plan.plan_version,
            source=plan.source,
            reference_flow=comparison.reference_flow,
            quantity_changed=comparison.quantity_changed,
            notional_changed=comparison.notional_changed,
            exit_changed=comparison.exit_changed,
            comparison=comparison,
            diagnostics=comparison.diagnostics,
        )
        return result

    # --- execution inputs -----------------------------------------------------------------

    def _executable_plan(self, trade_pipeline_result: Any, *, action: str) -> tuple[Any, str]:
        """The plan when it can feed the order, otherwise ``(None, reason)``."""

        if trade_pipeline_result is None:
            return None, "NO_TRADE_PLAN"

        plan = trade_pipeline_result.plan

        if action not in EXECUTION_OPEN_ACTIONS:
            return None, "ACTION_NOT_OPEN"

        if plan.action != action:
            return None, "PLAN_ACTION_MISMATCH"

        if plan.status not in EXECUTION_PLAN_ALLOWED_STATUSES:
            return None, f"PLAN_STATUS_{plan.status}"

        return plan, ""

    def planned_quantity_for_execution(
        self,
        trade_pipeline_result: Any,
        *,
        action: str,
    ) -> tuple[Any, str]:
        if not self.config.dynamic_sizing_enabled:
            return None, "DYNAMIC_SIZING_DISABLED"

        plan, reason = self._executable_plan(trade_pipeline_result, action=action)
        if plan is None:
            return None, reason

        if plan.quantity <= 0:
            return None, "PLAN_QUANTITY_NOT_POSITIVE"

        return plan.quantity, "TRADE_PLAN_QUANTITY_READY"

    def planned_exit_for_execution(
        self,
        trade_pipeline_result: Any,
        *,
        action: str,
    ) -> tuple[PlannedExit, str]:
        if not self.config.dynamic_exit_enabled:
            economic_exit = _economic_adjusted_exit_for_execution(
                trade_pipeline_result,
                action=action,
            )
            if economic_exit is not None:
                return economic_exit, "ECONOMIC_EXIT_ADJUSTMENT_REQUIRED"
            return None, "DYNAMIC_EXIT_DISABLED"

        plan, reason = self._executable_plan(trade_pipeline_result, action=action)
        if plan is None:
            return None, reason

        if plan.stop_price is None or plan.take_profit_price is None:
            return None, "PLAN_EXIT_NOT_AVAILABLE"

        return {
            "stop_price": plan.stop_price,
            "take_profit_price": plan.take_profit_price,
            "source": GLOBAL_FALLBACK_EXIT_SOURCE,
        }, "TRADE_PLAN_EXIT_READY"

    def strategy_planned_exit_for_execution(
        self,
        *,
        score: Any,
        heavy_snapshot: Any,
        action: str,
    ) -> tuple[PlannedExit, str]:
        """Use the stop and take the strategy proposed, when they pass validation."""

        metadata = getattr(score, "metadata", None)
        if not has_strategy_planned_exit_metadata(metadata):
            return None, "NO_STRATEGY_PLANNED_EXIT_METADATA"

        symbol = getattr(score, "symbol", None)
        market_price = getattr(getattr(heavy_snapshot, "market", None), "price", None)
        exchange_rules = getattr(heavy_snapshot, "exchange_rules", None)
        if market_price is None or exchange_rules is None:
            reason = "STRATEGY_PLANNED_EXIT_CONTEXT_UNAVAILABLE"
            self.journal.append(
                "strategy_planned_exit_rejected",
                **self.trace_payload(),
                symbol=symbol,
                action=action,
                reason=reason,
                diagnostics=dict(extract_strategy_planned_exit(metadata).diagnostics),
            )
            return None, reason

        _, candidate, validated = resolve_strategy_planned_exit(
            metadata,
            action=action,
            entry_price=Decimal(str(market_price)),
            price_tick=exchange_rules.price_tick,
        )
        self.journal.append(
            "strategy_planned_exit_detected",
            **self.trace_payload(),
            symbol=symbol,
            action=action,
            stop_price=candidate.stop_price,
            take_profit_price=candidate.take_profit_price,
            valid=validated.valid,
            rejection_reason=validated.rejection_reason,
            diagnostics=dict(validated.diagnostics),
        )
        if not validated.valid:
            self.journal.append(
                "strategy_planned_exit_rejected",
                **self.trace_payload(),
                symbol=symbol,
                action=action,
                reason=validated.rejection_reason,
                fallback_source=GLOBAL_FALLBACK_EXIT_SOURCE,
                diagnostics=dict(validated.diagnostics),
            )
            self.journal.append(
                "dynamic_exit_fallback_used",
                **self.trace_payload(),
                symbol=symbol,
                action=action,
                reason=validated.rejection_reason,
            )
            return None, validated.rejection_reason or "STRATEGY_PLANNED_EXIT_REJECTED"

        payload: dict[str, Any] = validated.as_payload()
        self.journal.append(
            "strategy_planned_exit_accepted",
            **self.trace_payload(),
            symbol=symbol,
            action=action,
            stop_price=validated.stop_price,
            take_profit_price=validated.take_profit_price,
            reward_risk_liquido=validated.reward_risk_liquido,
            cost_to_reward_ratio=validated.cost_to_reward_ratio,
        )
        self.journal.append(
            "dynamic_exit_used",
            **self.trace_payload(),
            symbol=symbol,
            action=action,
            exit_source=STRATEGY_METADATA_EXIT_SOURCE,
            stop_price=validated.stop_price,
            take_profit_price=validated.take_profit_price,
        )
        return payload, "STRATEGY_METADATA_EXIT_READY"

    def select_order_execution_policy(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        score: Any,
        trade_pipeline_result: Any,
        economic_gate_decision: EconomicGateDecision,
    ) -> Any:
        if action not in EXECUTION_OPEN_ACTIONS:
            return None

        context = trade_pipeline_result.context if trade_pipeline_result is not None else None
        plan = trade_pipeline_result.plan if trade_pipeline_result is not None else None
        market_price = context.market_price if context is not None else None
        reference_price = plan.entry_reference_price if plan is not None else market_price
        execution_plan = self.order_execution_policy.build_entry_plan(
            symbol=symbol_config.symbol,
            action=action,
            market_price=market_price,
            reference_price=reference_price,
            economic_decision=economic_gate_decision,
            spread_bps=spread_bps_for_order_policy(trade_pipeline_result),
            signal_confidence=getattr(score, "confidence", None),
        )
        event_type = "order_execution_policy_selected"
        if execution_plan.fallback_to_market:
            event_type = "order_execution_policy_fallback"
        if not execution_plan.allowed:
            event_type = "order_execution_policy_blocked"

        self.journal.append(
            event_type,
            symbol=symbol_config.symbol,
            action=action,
            allowed=execution_plan.allowed,
            reason=execution_plan.reason,
            execution_style=execution_plan.execution_style,
            entry_order_type=execution_plan.entry_order_type,
            assumes_entry_liquidity=execution_plan.assumes_entry_liquidity,
            assumes_exit_liquidity=execution_plan.assumes_exit_liquidity,
            market_price=execution_plan.market_price,
            reference_price=execution_plan.reference_price,
            limit_price=execution_plan.limit_price,
            post_only=execution_plan.post_only,
            timeout_seconds=execution_plan.timeout_seconds,
            fallback_to_market=execution_plan.fallback_to_market,
            operational_entry_order_type=execution_plan.operational_entry_order_type,
            executor_behavior_unchanged=execution_plan.executor_behavior_unchanged,
            note=execution_plan.note,
            diagnostics=execution_plan.diagnostics,
            order_execution_plan=execution_plan,
        )
        return execution_plan
