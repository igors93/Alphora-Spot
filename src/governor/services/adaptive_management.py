"""Adaptive management of an open position: shadow evaluation, adjustment plan and its activation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from config.symbol_context import SymbolTradingConfig
from governor.evaluation.outcomes import EventJournal
from governor.governor_helpers import _enum_value


class AdaptiveManagementService:
    """Evaluate an open position and hand the adjustment plan to the execution engine.

    The plan is built with execution disabled; only an activation decision that
    enables execution lets the engine apply it. Every failure is journaled and
    never interrupts the evaluation of the symbol.
    """

    def __init__(
        self,
        *,
        config: Any,
        journal: EventJournal,
        manager: Any,
        planner: Any,
        activation_policy: Any,
        execution_engine: Any,
    ) -> None:
        self.config = config
        self.journal = journal
        self.manager = manager
        self.planner = planner
        self.activation_policy = activation_policy
        self.execution_engine = execution_engine

    def record_shadow(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy: Any,
        runtime_state: Any,
    ) -> None:
        if self.manager is None:
            return

        if not heavy.position.is_open:
            return

        try:
            evaluation = self.manager.evaluate(
                position=heavy.position,
                current_price=heavy.mark_price,
                runtime_state=runtime_state,
            )
            self._record_evaluation(symbol_config, evaluation)

            if self.planner is None:
                return

            adjustment_plan = self.planner.build_shadow_plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                execution_enabled=False,
            )
            activation_decision = self.evaluate_activation(
                symbol_config=symbol_config,
                adjustment_plan=adjustment_plan,
            )
            if activation_decision is not None and activation_decision.execution_enabled:
                adjustment_plan = replace(
                    adjustment_plan,
                    execution_enabled=True,
                    diagnostics={
                        **adjustment_plan.diagnostics,
                        "activation": activation_decision.diagnostics,
                        "activation_decision_id": activation_decision.decision_id,
                        "activation_status": activation_decision.status,
                    },
                )

            self._record_plan(symbol_config, adjustment_plan)
            self.record_execution_result(
                symbol_config=symbol_config,
                adjustment_plan=adjustment_plan,
            )
        except Exception as error:
            self.journal.append(
                "adaptive_management_shadow_failed",
                symbol=symbol_config.symbol,
                reason=str(error),
            )

    def _record_evaluation(self, symbol_config: SymbolTradingConfig, evaluation: Any) -> None:
        self.journal.append(
            "adaptive_management_shadow_evaluated",
            symbol=symbol_config.symbol,
            action=evaluation.action,
            reason=evaluation.reason,
            confidence=evaluation.confidence,
            trade_plan_id=evaluation.trade_plan_id,
            suggested_stop_price=evaluation.suggested_stop_price,
            suggested_take_profit_price=evaluation.suggested_take_profit_price,
            trailing_stop_price=evaluation.trailing_stop_price,
            suggested_reduction_fraction=evaluation.suggested_reduction_fraction,
            suggested_exit_fraction=evaluation.suggested_exit_fraction,
            should_reduce_position=evaluation.should_reduce_position,
            should_exit_position=evaluation.should_exit_position,
            deterioration_score=evaluation.deterioration_score,
            management_mode=evaluation.management_mode,
            evaluation=evaluation,
            diagnostics=evaluation.diagnostics,
        )

    def _record_plan(self, symbol_config: SymbolTradingConfig, plan: Any) -> None:
        self.journal.append(
            "adaptive_adjustment_plan_shadow_created",
            symbol=symbol_config.symbol,
            action=plan.action,
            status=plan.status,
            reason=plan.reason,
            plan_id=plan.plan_id,
            plan_version=plan.plan_version,
            source=plan.source,
            trade_plan_id=plan.trade_plan_id,
            current_stop_price=plan.current_stop_price,
            current_take_profit_price=plan.current_take_profit_price,
            target_stop_price=plan.target_stop_price,
            target_take_profit_price=plan.target_take_profit_price,
            trailing_stop_price=plan.trailing_stop_price,
            target_reduction_fraction=plan.target_reduction_fraction,
            target_exit_fraction=plan.target_exit_fraction,
            should_reduce_position=plan.should_reduce_position,
            should_exit_position=plan.should_exit_position,
            deterioration_score=plan.deterioration_score,
            management_mode=plan.management_mode,
            requires_order_change=plan.requires_order_change,
            requires_manual_review=plan.requires_manual_review,
            execution_enabled=plan.execution_enabled,
            shadow_only=plan.shadow_only,
            adjustment_plan=plan,
            diagnostics=plan.diagnostics,
        )

    def evaluate_activation(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        adjustment_plan: Any,
    ) -> Any:
        policy = self.activation_policy
        if policy is None:
            return None

        config = self.config
        try:
            decision = policy.evaluate(
                adjustment_plan=adjustment_plan,
                architecture_mode=_enum_value(config.architecture_mode),
                intelligent_activation_level=_enum_value(config.intelligent_activation_level),
                execution_mode=_enum_value(config.execution_mode),
                adaptive_execution_enabled=config.adaptive_execution_enabled,
                dynamic_exit_enabled=config.dynamic_exit_enabled,
            )
            self.journal.append(
                "adaptive_adjustment_activation_evaluated",
                symbol=symbol_config.symbol,
                status=decision.status,
                activation_action=decision.activation_action,
                reason=decision.reason,
                decision_id=decision.decision_id,
                plan_id=decision.plan_id,
                plan_action=decision.plan_action,
                trade_plan_id=decision.trade_plan_id,
                execution_enabled=decision.execution_enabled,
                applied=decision.applied,
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
            self.journal.append(
                "adaptive_adjustment_activation_failed",
                symbol=symbol_config.symbol,
                plan_id=getattr(adjustment_plan, "plan_id", None),
                reason=str(error),
            )
            return None

    def record_execution_result(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        adjustment_plan: Any,
    ) -> None:
        apply_adjustment = getattr(self.execution_engine, "apply_adaptive_adjustment_plan", None)
        if not callable(apply_adjustment):
            self.journal.append(
                "adaptive_adjustment_execution_result",
                symbol=symbol_config.symbol,
                plan_id=adjustment_plan.plan_id,
                status="SKIPPED",
                reason="EXECUTION_ENGINE_NOT_SUPPORTED",
                execution_enabled=adjustment_plan.execution_enabled,
            )
            return

        try:
            result = apply_adjustment(
                symbol_config=symbol_config,
                adjustment_plan=adjustment_plan,
            )
            self.journal.append(
                "adaptive_adjustment_execution_result",
                symbol=symbol_config.symbol,
                plan_id=adjustment_plan.plan_id,
                status=result.get("status"),
                reason=result.get("reason"),
                execution_enabled=adjustment_plan.execution_enabled,
                result=result,
            )
        except Exception as error:
            self.journal.append(
                "adaptive_adjustment_execution_failed",
                symbol=symbol_config.symbol,
                plan_id=adjustment_plan.plan_id,
                reason=str(error),
            )
