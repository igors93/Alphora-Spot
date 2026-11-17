"""INTELLIGENT_FLOW layers: shadow order and exit plans, their activation and the planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.symbol_context import SymbolTradingConfig
from governor.evaluation.outcomes import EventJournal
from governor.governor_helpers import _enum_value

PlannedExit = dict[str, Any] | None


@dataclass(frozen=True)
class IntelligentFlowComponents:
    """Builders, comparators and policies of the flow; ``None`` turns a layer off."""

    order_builder: Any = None
    order_comparator: Any = None
    exit_builder: Any = None
    exit_comparator: Any = None
    order_activation_policy: Any = None
    exit_activation_policy: Any = None
    execution_planner: Any = None


def _plan_of(trade_pipeline_result: Any) -> Any:
    return trade_pipeline_result.plan if trade_pipeline_result is not None else None


def _passed(value: Any) -> str | None:
    return str(value) if value is not None else None


def _passed_exit(planned_exit: PlannedExit, key: str) -> str | None:
    return str(planned_exit[key]) if planned_exit is not None else None


class IntelligentFlowService:
    """Evaluate each intelligent layer of a trade and journal what it decided.

    Every layer is fail-safe: an error is journaled and the trade goes on without
    that layer. Only an activation decision that is ``applied`` changes the order.
    """

    def __init__(
        self,
        *,
        config: Any,
        journal: EventJournal,
        components: IntelligentFlowComponents,
    ) -> None:
        self.config = config
        self.journal = journal
        self.components = components

    def _modes(self) -> dict[str, str]:
        return {
            "architecture_mode": _enum_value(self.config.architecture_mode),
            "intelligent_activation_level": _enum_value(self.config.intelligent_activation_level),
        }

    def _failed(
        self, event_type: str, symbol_config: SymbolTradingConfig, action: str, error: Exception
    ) -> None:
        self.journal.append(
            event_type,
            symbol=symbol_config.symbol,
            action=action,
            reason=str(error),
        )

    # --- shadow plans and comparisons ---------------------------------------------------

    def record_order_shadow(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        trade_pipeline_result: Any,
        planned_quantity: Any,
        planned_exit: PlannedExit,
    ) -> Any:
        builder = self.components.order_builder
        if builder is None:
            return None

        try:
            plan = builder.build_shadow_plan(
                trade_plan=_plan_of(trade_pipeline_result),
                action=action,
                planned_quantity=planned_quantity,
                planned_exit=planned_exit,
                dynamic_sizing_enabled=self.config.dynamic_sizing_enabled,
                dynamic_exit_enabled=self.config.dynamic_exit_enabled,
                **self._modes(),
            )
            self.journal.append(
                "intelligent_order_plan_shadow_created",
                symbol=symbol_config.symbol,
                action=action,
                status=plan.status,
                reason=plan.reason,
                plan_id=plan.plan_id,
                trade_plan_id=plan.trade_plan_id,
                recommended_quantity=plan.recommended_quantity,
                recommended_stop_price=plan.recommended_stop_price,
                recommended_take_profit_price=plan.recommended_take_profit_price,
                execution_quantity_candidate=plan.execution_quantity_candidate,
                execution_stop_price_candidate=plan.execution_stop_price_candidate,
                execution_take_profit_price_candidate=plan.execution_take_profit_price_candidate,
                can_use_for_execution=plan.can_use_for_execution,
                shadow_only=plan.shadow_only,
                intelligent_order_plan=plan,
                diagnostics=plan.diagnostics,
            )
            return plan
        except Exception as error:
            self._failed("intelligent_order_plan_shadow_failed", symbol_config, action, error)
            return None

    def record_exit_shadow(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        trade_pipeline_result: Any,
        planned_exit: PlannedExit,
    ) -> Any:
        builder = self.components.exit_builder
        if builder is None:
            return None

        try:
            exit_plan = builder.build_shadow_plan(
                trade_plan=_plan_of(trade_pipeline_result),
                action=action,
                planned_exit=planned_exit,
                dynamic_exit_enabled=self.config.dynamic_exit_enabled,
                **self._modes(),
            )
            self.journal.append(
                "intelligent_exit_plan_shadow_created",
                symbol=symbol_config.symbol,
                action=action,
                status=exit_plan.status,
                reason=exit_plan.reason,
                plan_id=exit_plan.plan_id,
                trade_plan_id=exit_plan.trade_plan_id,
                entry_reference_price=exit_plan.entry_reference_price,
                recommended_stop_price=exit_plan.recommended_stop_price,
                recommended_take_profit_price=exit_plan.recommended_take_profit_price,
                current_stop_price=exit_plan.current_stop_price,
                current_take_profit_price=exit_plan.current_take_profit_price,
                stop_distance_pct=exit_plan.stop_distance_pct,
                take_profit_distance_pct=exit_plan.take_profit_distance_pct,
                risk_reward=exit_plan.risk_reward,
                adaptive_stop_price=exit_plan.adaptive_stop_price,
                adaptive_take_profit_price=exit_plan.adaptive_take_profit_price,
                adaptive_stop_distance_pct=exit_plan.adaptive_stop_distance_pct,
                adaptive_take_profit_distance_pct=exit_plan.adaptive_take_profit_distance_pct,
                adaptive_risk_reward=exit_plan.adaptive_risk_reward,
                minimum_reward_risk=exit_plan.minimum_reward_risk,
                minimum_stop_distance_pct=exit_plan.minimum_stop_distance_pct,
                recommendation_mode=exit_plan.recommendation_mode,
                protection_quality=exit_plan.protection_quality,
                adaptive_candidate_available=exit_plan.adaptive_candidate_available,
                adaptive_changed=exit_plan.adaptive_changed,
                stop_valid=exit_plan.stop_valid,
                take_profit_valid=exit_plan.take_profit_valid,
                protection_complete=exit_plan.protection_complete,
                shadow_only=exit_plan.shadow_only,
                can_use_for_execution=exit_plan.can_use_for_execution,
                intelligent_exit_plan=exit_plan,
                diagnostics=exit_plan.diagnostics,
            )
            return exit_plan
        except Exception as error:
            self._failed("intelligent_exit_plan_shadow_failed", symbol_config, action, error)
            return None

    def record_exit_comparison(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        trade_pipeline_result: Any,
        intelligent_exit_plan: Any,
        planned_exit: PlannedExit,
    ) -> Any:
        comparator = self.components.exit_comparator
        if comparator is None:
            return None

        try:
            comparison = comparator.compare(
                trade_plan=_plan_of(trade_pipeline_result),
                intelligent_exit_plan=intelligent_exit_plan,
                action=action,
                planned_exit=planned_exit,
            )
            self.journal.append(
                "intelligent_exit_plan_compared",
                symbol=symbol_config.symbol,
                action=action,
                status=comparison.status,
                reason=comparison.reason,
                comparison_id=comparison.comparison_id,
                trade_plan_id=comparison.trade_plan_id,
                intelligent_exit_plan_id=comparison.intelligent_exit_plan_id,
                stop_changed=comparison.stop_changed,
                take_profit_changed=comparison.take_profit_changed,
                risk_reward_changed=comparison.risk_reward_changed,
                protection_complete=comparison.protection_complete,
                execution_unchanged=comparison.execution_unchanged,
                can_use_for_execution=comparison.can_use_for_execution,
                shadow_only=comparison.shadow_only,
                differences=comparison.differences,
                diagnostics=comparison.diagnostics,
                comparison=comparison,
            )
            return comparison
        except Exception as error:
            self._failed("intelligent_exit_plan_comparison_failed", symbol_config, action, error)
            return None

    def record_order_comparison(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        trade_pipeline_result: Any,
        intelligent_order_plan: Any,
        planned_quantity: Any,
        planned_exit: PlannedExit,
    ) -> Any:
        comparator = self.components.order_comparator
        if comparator is None:
            return None

        try:
            comparison = comparator.compare(
                trade_plan=_plan_of(trade_pipeline_result),
                intelligent_order_plan=intelligent_order_plan,
                action=action,
                planned_quantity=planned_quantity,
                planned_exit=planned_exit,
            )
            self.journal.append(
                "intelligent_order_plan_compared",
                symbol=symbol_config.symbol,
                action=action,
                status=comparison.status,
                reason=comparison.reason,
                comparison_id=comparison.comparison_id,
                trade_plan_id=comparison.trade_plan_id,
                intelligent_order_plan_id=comparison.intelligent_order_plan_id,
                quantity_changed=comparison.quantity_changed,
                stop_changed=comparison.stop_changed,
                take_profit_changed=comparison.take_profit_changed,
                execution_unchanged=comparison.execution_unchanged,
                can_use_for_execution=comparison.can_use_for_execution,
                shadow_only=comparison.shadow_only,
                differences=comparison.differences,
                diagnostics=comparison.diagnostics,
                comparison=comparison,
            )
            return comparison
        except Exception as error:
            self._failed("intelligent_order_plan_comparison_failed", symbol_config, action, error)
            return None

    # --- activation ---------------------------------------------------------------------

    def evaluate_order_activation(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        intelligent_order_plan: Any,
    ) -> Any:
        policy = self.components.order_activation_policy
        if policy is None:
            return None

        try:
            decision = policy.evaluate(
                intelligent_order_plan=intelligent_order_plan,
                action=action,
                **self._modes(),
                execution_mode=_enum_value(self.config.execution_mode),
                dynamic_sizing_enabled=self.config.dynamic_sizing_enabled,
                dynamic_exit_enabled=self.config.dynamic_exit_enabled,
            )
            self.journal.append(
                "intelligent_order_activation_evaluated",
                symbol=symbol_config.symbol,
                action=action,
                status=decision.status,
                activation_action=decision.action,
                reason=decision.reason,
                decision_id=decision.decision_id,
                plan_id=decision.plan_id,
                trade_plan_id=decision.trade_plan_id,
                final_quantity=decision.final_quantity,
                final_stop_price=decision.final_stop_price,
                final_take_profit_price=decision.final_take_profit_price,
                applied=decision.applied,
                quantity_applied=decision.quantity_applied,
                exit_applied=decision.exit_applied,
                can_use_for_execution=decision.can_use_for_execution,
                paper_only=decision.paper_only,
                testnet_only=decision.testnet_only,
                can_activate_testnet=decision.can_activate_testnet,
                can_activate_real=decision.can_activate_real,
                diagnostics=decision.diagnostics,
                activation=decision,
            )
            return decision
        except Exception as error:
            self._failed("intelligent_order_activation_failed", symbol_config, action, error)
            return None

    def evaluate_exit_activation(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        intelligent_exit_plan: Any,
    ) -> Any:
        policy = self.components.exit_activation_policy
        if policy is None:
            return None

        try:
            decision = policy.evaluate(
                intelligent_exit_plan=intelligent_exit_plan,
                action=action,
                **self._modes(),
                execution_mode=_enum_value(self.config.execution_mode),
                dynamic_exit_enabled=self.config.dynamic_exit_enabled,
            )
            self.journal.append(
                "intelligent_exit_activation_evaluated",
                symbol=symbol_config.symbol,
                action=action,
                status=decision.status,
                activation_action=decision.action,
                reason=decision.reason,
                decision_id=decision.decision_id,
                plan_id=decision.plan_id,
                trade_plan_id=decision.trade_plan_id,
                final_stop_price=decision.final_stop_price,
                final_take_profit_price=decision.final_take_profit_price,
                exit_source=decision.exit_source,
                used_adaptive_exit=decision.used_adaptive_exit,
                adaptive_exit_applied=decision.adaptive_exit_applied,
                recommendation_mode=decision.recommendation_mode,
                protection_quality=decision.protection_quality,
                applied=decision.applied,
                exit_applied=decision.exit_applied,
                can_use_for_execution=decision.can_use_for_execution,
                paper_only=decision.paper_only,
                testnet_only=decision.testnet_only,
                can_activate_testnet=decision.can_activate_testnet,
                can_activate_real=decision.can_activate_real,
                diagnostics=decision.diagnostics,
                activation=decision,
            )
            return decision
        except Exception as error:
            self._failed("intelligent_exit_activation_failed", symbol_config, action, error)
            return None

    def record_exit_activation_result(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        activation_decision: Any,
        planned_exit: PlannedExit,
        planned_exit_reason: str,
    ) -> None:
        if activation_decision is None:
            return

        decision = activation_decision
        self.journal.append(
            "intelligent_exit_activation_result",
            symbol=symbol_config.symbol,
            action=action,
            status=decision.status,
            activation_action=decision.action,
            reason=decision.reason,
            decision_id=decision.decision_id,
            plan_id=decision.plan_id,
            trade_plan_id=decision.trade_plan_id,
            applied=decision.applied,
            exit_applied=decision.exit_applied,
            can_use_for_execution=decision.can_use_for_execution,
            paper_only=decision.paper_only,
            testnet_only=decision.testnet_only,
            can_activate_testnet=decision.can_activate_testnet,
            can_activate_real=decision.can_activate_real,
            final_stop_price=decision.final_stop_price,
            final_take_profit_price=decision.final_take_profit_price,
            exit_source=decision.exit_source,
            used_adaptive_exit=decision.used_adaptive_exit,
            adaptive_exit_applied=decision.adaptive_exit_applied,
            recommendation_mode=decision.recommendation_mode,
            protection_quality=decision.protection_quality,
            planned_stop_price_passed=_passed_exit(planned_exit, "stop_price"),
            planned_take_profit_price_passed=_passed_exit(planned_exit, "take_profit_price"),
            planned_exit_reason=planned_exit_reason,
            execution_unchanged=not decision.applied,
            diagnostics=decision.diagnostics,
        )

    def record_order_activation_result(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        activation_decision: Any,
        planned_quantity: Any,
        planned_exit: PlannedExit,
        planned_quantity_reason: str,
        planned_exit_reason: str,
    ) -> None:
        if activation_decision is None:
            return

        decision = activation_decision
        self.journal.append(
            "intelligent_order_activation_result",
            symbol=symbol_config.symbol,
            action=action,
            status=decision.status,
            activation_action=decision.action,
            reason=decision.reason,
            decision_id=decision.decision_id,
            plan_id=decision.plan_id,
            trade_plan_id=decision.trade_plan_id,
            applied=decision.applied,
            quantity_applied=decision.quantity_applied,
            exit_applied=decision.exit_applied,
            can_use_for_execution=decision.can_use_for_execution,
            paper_only=decision.paper_only,
            testnet_only=decision.testnet_only,
            can_activate_testnet=decision.can_activate_testnet,
            can_activate_real=decision.can_activate_real,
            final_quantity=decision.final_quantity,
            final_stop_price=decision.final_stop_price,
            final_take_profit_price=decision.final_take_profit_price,
            planned_quantity_passed=_passed(planned_quantity),
            planned_stop_price_passed=_passed_exit(planned_exit, "stop_price"),
            planned_take_profit_price_passed=_passed_exit(planned_exit, "take_profit_price"),
            planned_quantity_reason=planned_quantity_reason,
            planned_exit_reason=planned_exit_reason,
            execution_unchanged=not decision.applied,
            diagnostics=decision.diagnostics,
        )

    # --- execution planner and final audit ----------------------------------------------

    def evaluate_execution_plan(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        intent: Any,
        heavy_snapshot: Any,
        planned_quantity: Any,
        planned_exit: PlannedExit,
        trade_plan_id: str | None,
    ) -> Any:
        planner = self.components.execution_planner
        if planner is None:
            return None

        try:
            plan = planner.build_plan(
                symbol=symbol_config.symbol,
                action=action,
                **self._modes(),
                execution_mode=_enum_value(self.config.execution_mode),
                execution_planner_enabled=self.config.execution_planner_enabled,
                intent_market_price=intent.market_price,
                mark_price=getattr(heavy_snapshot, "mark_price", None),
                market_data_age_seconds=getattr(heavy_snapshot, "data_age_seconds", None),
                market_data_stale=getattr(heavy_snapshot, "stale", False),
                planned_quantity=planned_quantity,
                planned_exit=planned_exit,
                trade_plan_id=trade_plan_id,
            )
            self.journal.append(
                "intelligent_execution_plan_evaluated",
                symbol=symbol_config.symbol,
                action=action,
                status=plan.status,
                planner_action=plan.planner_action,
                reason=plan.reason,
                plan_id=plan.plan_id,
                trade_plan_id=plan.trade_plan_id,
                execution_style=plan.execution_style,
                should_execute=plan.should_execute,
                execution_will_run=plan.execution_will_run,
                planned_quantity_present=plan.planned_quantity_present,
                planned_exit_present=plan.planned_exit_present,
                market_price=plan.market_price,
                mark_price=plan.mark_price,
                price_drift_bps=plan.price_drift_bps,
                max_price_drift_bps=plan.max_price_drift_bps,
                market_data_age_seconds=plan.market_data_age_seconds,
                max_market_data_age_seconds=plan.max_market_data_age_seconds,
                market_data_stale=plan.market_data_stale,
                applied=plan.applied,
                can_use_for_execution=plan.can_use_for_execution,
                paper_only=plan.paper_only,
                testnet_only=plan.testnet_only,
                can_activate_testnet=plan.can_activate_testnet,
                can_activate_real=plan.can_activate_real,
                diagnostics=plan.diagnostics,
                execution_plan=plan,
            )
            return plan
        except Exception as error:
            self._failed("intelligent_execution_plan_failed", symbol_config, action, error)
            return None

    def record_order_execution_finalized(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        activation_decision: Any,
        governor_decision: Any,
        final_status: str,
        final_reason: str,
        planned_quantity: Any,
        planned_exit: PlannedExit,
        planned_quantity_reason: str,
        planned_exit_reason: str,
        execution_will_run: bool,
    ) -> None:
        if activation_decision is None:
            return

        decision = activation_decision
        risk_allowed = governor_decision.allowed if governor_decision is not None else None
        self.journal.append(
            "intelligent_order_execution_finalized",
            symbol=symbol_config.symbol,
            action=action,
            status=final_status,
            reason=final_reason,
            execution_will_run=execution_will_run,
            risk_allowed=risk_allowed,
            governor_decision_reason=(
                governor_decision.reason if governor_decision is not None else None
            ),
            activation_status=decision.status,
            activation_action=decision.action,
            activation_applied=decision.applied,
            quantity_applied=decision.quantity_applied,
            exit_applied=decision.exit_applied,
            can_use_for_execution=decision.can_use_for_execution,
            paper_only=decision.paper_only,
            testnet_only=decision.testnet_only,
            can_activate_testnet=decision.can_activate_testnet,
            can_activate_real=decision.can_activate_real,
            decision_id=decision.decision_id,
            plan_id=decision.plan_id,
            trade_plan_id=decision.trade_plan_id,
            final_quantity=decision.final_quantity,
            final_stop_price=decision.final_stop_price,
            final_take_profit_price=decision.final_take_profit_price,
            planned_quantity_passed=_passed(planned_quantity),
            planned_stop_price_passed=_passed_exit(planned_exit, "stop_price"),
            planned_take_profit_price_passed=_passed_exit(planned_exit, "take_profit_price"),
            planned_quantity_reason=planned_quantity_reason,
            planned_exit_reason=planned_exit_reason,
            diagnostics={
                **decision.diagnostics,
                "final_status": final_status,
                "execution_will_run": execution_will_run,
                "risk_allowed": risk_allowed,
                "safety_note": (
                    "Fase 4B: auditoria final da ordem inteligente depois "
                    "do risco e das decisões de learning. REAL continua protegido."
                ),
            },
        )
