from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from domain.models import PositionDirection
from futures.state import RuntimeState
from governor.adaptive_manager import (
    ACTION_EXIT_POSITION_SHADOW,
    ACTION_HOLD,
    ACTION_MOVE_STOP_TO_BREAKEVEN_SHADOW,
    ACTION_NO_POSITION,
    ACTION_PARTIAL_REDUCTION_SHADOW,
    ACTION_PROTECT_PROFIT_SHADOW,
    ACTION_REDUCE_RISK_SHADOW,
    ACTION_TRAILING_STOP_SHADOW,
    AdaptiveManagementEvaluation,
)

ADAPTIVE_ADJUSTMENT_PLAN_VERSION = "1.0.0"
ADAPTIVE_ADJUSTMENT_SOURCE_SHADOW = "SHADOW"

PLAN_ACTION_NONE = "NO_ADJUSTMENT"
PLAN_ACTION_MOVE_STOP_TO_BREAKEVEN = "MOVE_STOP_TO_BREAKEVEN"
PLAN_ACTION_PROTECT_PROFIT = "PROTECT_PROFIT"
PLAN_ACTION_TRAILING_STOP = "TRAILING_STOP"
PLAN_ACTION_REDUCE_RISK_REVIEW = "REDUCE_RISK_REVIEW"
PLAN_ACTION_PARTIAL_REDUCTION_REVIEW = "PARTIAL_REDUCTION_REVIEW"
PLAN_ACTION_EXIT_POSITION_REVIEW = "EXIT_POSITION_REVIEW"

PLAN_STATUS_HOLD = "HOLD"
PLAN_STATUS_NO_POSITION = "NO_POSITION"
PLAN_STATUS_SHADOW_READY = "SHADOW_READY"
PLAN_STATUS_SHADOW_REVIEW_REQUIRED = "SHADOW_REVIEW_REQUIRED"
PLAN_STATUS_BLOCKED_MISSING_RUNTIME_STATE = "BLOCKED_MISSING_RUNTIME_STATE"
PLAN_STATUS_BLOCKED_MISSING_TARGET = "BLOCKED_MISSING_TARGET"
PLAN_STATUS_BLOCKED_TARGET_NOT_IMPROVING = "BLOCKED_TARGET_NOT_IMPROVING"


def _new_adjustment_plan_id() -> str:
    return f"adj_{uuid4().hex}"


@dataclass(frozen=True)
class AdaptiveAdjustmentPlan:
    symbol: str
    action: str
    status: str
    reason: str
    confidence: int
    current_price: Decimal
    entry_price: Decimal
    pnl_pct: Decimal
    current_stop_price: Decimal | None = None
    current_take_profit_price: Decimal | None = None
    target_stop_price: Decimal | None = None
    target_take_profit_price: Decimal | None = None
    trailing_stop_price: Decimal | None = None
    target_reduction_fraction: Decimal | None = None
    target_exit_fraction: Decimal | None = None
    should_reduce_position: bool = False
    should_exit_position: bool = False
    deterioration_score: Decimal | None = None
    management_mode: str = ""
    trade_plan_id: str | None = None
    source_evaluation_action: str = ""
    requires_order_change: bool = False
    requires_manual_review: bool = False
    execution_enabled: bool = False
    shadow_only: bool = True
    plan_id: str = field(default_factory=_new_adjustment_plan_id)
    plan_version: str = ADAPTIVE_ADJUSTMENT_PLAN_VERSION
    source: str = ADAPTIVE_ADJUSTMENT_SOURCE_SHADOW
    diagnostics: dict[str, Any] = field(default_factory=dict)


class AdaptiveAdjustmentPlanner:
    def build_shadow_plan(
        self,
        *,
        evaluation: AdaptiveManagementEvaluation,
        runtime_state: RuntimeState | None = None,
        execution_enabled: bool = False,
    ) -> AdaptiveAdjustmentPlan:
        current_stop = runtime_state.planned_stop_price if runtime_state is not None else None
        current_take = (
            runtime_state.planned_take_profit_price if runtime_state is not None else None
        )

        diagnostics = {
            "shadow_only": True,
            "execution_enabled": bool(execution_enabled),
            "evaluation_action": evaluation.action,
            "evaluation_reason": evaluation.reason,
            "evaluation_confidence": evaluation.confidence,
            "management_mode": getattr(evaluation, "management_mode", ""),
            "should_reduce_position": getattr(evaluation, "should_reduce_position", False),
            "should_exit_position": getattr(evaluation, "should_exit_position", False),
            "suggested_reduction_fraction": (
                str(evaluation.suggested_reduction_fraction)
                if getattr(evaluation, "suggested_reduction_fraction", None) is not None
                else None
            ),
            "suggested_exit_fraction": (
                str(evaluation.suggested_exit_fraction)
                if getattr(evaluation, "suggested_exit_fraction", None) is not None
                else None
            ),
            "trailing_stop_price": (
                str(evaluation.trailing_stop_price)
                if getattr(evaluation, "trailing_stop_price", None) is not None
                else None
            ),
            "deterioration_score": (
                str(evaluation.deterioration_score)
                if getattr(evaluation, "deterioration_score", None) is not None
                else None
            ),
            "runtime_state_available": runtime_state is not None,
            "current_stop_price": str(current_stop) if current_stop is not None else None,
            "current_take_profit_price": (str(current_take) if current_take is not None else None),
            "target_stop_price": (
                str(evaluation.suggested_stop_price)
                if evaluation.suggested_stop_price is not None
                else None
            ),
            "target_take_profit_price": (
                str(evaluation.suggested_take_profit_price)
                if evaluation.suggested_take_profit_price is not None
                else None
            ),
        }

        if evaluation.action == ACTION_NO_POSITION:
            return self._plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_NONE,
                status=PLAN_STATUS_NO_POSITION,
                reason="POSITION_NOT_OPEN",
                diagnostics=diagnostics,
            )

        if evaluation.action == ACTION_HOLD:
            status = (
                PLAN_STATUS_BLOCKED_MISSING_RUNTIME_STATE
                if runtime_state is None
                else PLAN_STATUS_HOLD
            )
            return self._plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_NONE,
                status=status,
                reason=evaluation.reason,
                diagnostics=diagnostics,
            )

        if runtime_state is None:
            return self._plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_NONE,
                status=PLAN_STATUS_BLOCKED_MISSING_RUNTIME_STATE,
                reason="RUNTIME_STATE_NOT_AVAILABLE",
                diagnostics=diagnostics,
            )

        if evaluation.action == ACTION_MOVE_STOP_TO_BREAKEVEN_SHADOW:
            return self._plan_from_target(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_MOVE_STOP_TO_BREAKEVEN,
                reason=evaluation.reason,
                diagnostics=diagnostics,
                execution_enabled=execution_enabled,
            )

        if evaluation.action == ACTION_PROTECT_PROFIT_SHADOW:
            return self._plan_from_target(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_PROTECT_PROFIT,
                reason=evaluation.reason,
                diagnostics=diagnostics,
                execution_enabled=execution_enabled,
            )

        if evaluation.action == ACTION_TRAILING_STOP_SHADOW:
            return self._plan_from_target(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_TRAILING_STOP,
                reason=evaluation.reason,
                diagnostics=diagnostics,
                execution_enabled=execution_enabled,
            )

        if evaluation.action == ACTION_PARTIAL_REDUCTION_SHADOW:
            return self._plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_PARTIAL_REDUCTION_REVIEW,
                status=PLAN_STATUS_SHADOW_REVIEW_REQUIRED,
                reason=evaluation.reason,
                target_stop_price=evaluation.suggested_stop_price,
                target_take_profit_price=evaluation.suggested_take_profit_price,
                requires_order_change=False,
                requires_manual_review=True,
                diagnostics=diagnostics,
            )

        if evaluation.action == ACTION_EXIT_POSITION_SHADOW:
            return self._plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_EXIT_POSITION_REVIEW,
                status=PLAN_STATUS_SHADOW_REVIEW_REQUIRED,
                reason=evaluation.reason,
                target_stop_price=evaluation.suggested_stop_price,
                target_take_profit_price=evaluation.suggested_take_profit_price,
                requires_order_change=False,
                requires_manual_review=True,
                diagnostics=diagnostics,
            )

        if evaluation.action == ACTION_REDUCE_RISK_SHADOW:
            return self._plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_REDUCE_RISK_REVIEW,
                status=PLAN_STATUS_SHADOW_REVIEW_REQUIRED,
                reason=evaluation.reason,
                target_stop_price=evaluation.suggested_stop_price,
                target_take_profit_price=evaluation.suggested_take_profit_price,
                requires_order_change=False,
                requires_manual_review=True,
                diagnostics=diagnostics,
            )

        return self._plan(
            evaluation=evaluation,
            runtime_state=runtime_state,
            action=PLAN_ACTION_NONE,
            status=PLAN_STATUS_HOLD,
            reason="UNKNOWN_EVALUATION_ACTION",
            diagnostics=diagnostics,
        )

    def _plan_from_target(
        self,
        *,
        evaluation: AdaptiveManagementEvaluation,
        runtime_state: RuntimeState,
        action: str,
        reason: str,
        diagnostics: dict[str, Any],
        execution_enabled: bool = False,
    ) -> AdaptiveAdjustmentPlan:
        if evaluation.suggested_stop_price is None:
            return self._plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_NONE,
                status=PLAN_STATUS_BLOCKED_MISSING_TARGET,
                reason="SUGGESTED_STOP_NOT_AVAILABLE",
                diagnostics=diagnostics,
            )

        if runtime_state.planned_stop_price is None:
            return self._plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_NONE,
                status=PLAN_STATUS_BLOCKED_MISSING_TARGET,
                reason="CURRENT_STOP_NOT_AVAILABLE",
                diagnostics=diagnostics,
            )

        if not _target_stop_improves_protection(
            direction=runtime_state.direction,
            current_price=evaluation.current_price,
            current_stop_price=runtime_state.planned_stop_price,
            target_stop_price=evaluation.suggested_stop_price,
        ):
            return self._plan(
                evaluation=evaluation,
                runtime_state=runtime_state,
                action=PLAN_ACTION_NONE,
                status=PLAN_STATUS_BLOCKED_TARGET_NOT_IMPROVING,
                reason="TARGET_STOP_DOES_NOT_IMPROVE_PROTECTION",
                target_stop_price=evaluation.suggested_stop_price,
                target_take_profit_price=evaluation.suggested_take_profit_price,
                diagnostics=diagnostics,
            )

        return self._plan(
            evaluation=evaluation,
            runtime_state=runtime_state,
            action=action,
            status=PLAN_STATUS_SHADOW_READY,
            reason=reason,
            target_stop_price=evaluation.suggested_stop_price,
            target_take_profit_price=evaluation.suggested_take_profit_price,
            requires_order_change=True,
            execution_enabled=execution_enabled,
            diagnostics=diagnostics,
        )

    def _plan(
        self,
        *,
        evaluation: AdaptiveManagementEvaluation,
        runtime_state: RuntimeState | None,
        action: str,
        status: str,
        reason: str,
        target_stop_price: Decimal | None = None,
        target_take_profit_price: Decimal | None = None,
        requires_order_change: bool = False,
        requires_manual_review: bool = False,
        execution_enabled: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> AdaptiveAdjustmentPlan:
        return AdaptiveAdjustmentPlan(
            symbol=evaluation.symbol,
            action=action,
            status=status,
            reason=reason,
            confidence=evaluation.confidence,
            current_price=evaluation.current_price,
            entry_price=evaluation.entry_price,
            pnl_pct=evaluation.pnl_pct,
            current_stop_price=(
                runtime_state.planned_stop_price if runtime_state is not None else None
            ),
            current_take_profit_price=(
                runtime_state.planned_take_profit_price if runtime_state is not None else None
            ),
            target_stop_price=target_stop_price,
            target_take_profit_price=target_take_profit_price,
            trailing_stop_price=getattr(evaluation, "trailing_stop_price", None),
            target_reduction_fraction=getattr(
                evaluation,
                "suggested_reduction_fraction",
                None,
            ),
            target_exit_fraction=getattr(
                evaluation,
                "suggested_exit_fraction",
                None,
            ),
            should_reduce_position=getattr(
                evaluation,
                "should_reduce_position",
                False,
            ),
            should_exit_position=getattr(
                evaluation,
                "should_exit_position",
                False,
            ),
            deterioration_score=getattr(evaluation, "deterioration_score", None),
            management_mode=getattr(evaluation, "management_mode", ""),
            trade_plan_id=(
                runtime_state.trade_plan_id
                if runtime_state is not None
                else evaluation.trade_plan_id
            ),
            source_evaluation_action=evaluation.action,
            requires_order_change=requires_order_change,
            requires_manual_review=requires_manual_review,
            execution_enabled=bool(execution_enabled and requires_order_change),
            shadow_only=True,
            diagnostics=diagnostics or {},
        )


def _target_stop_improves_protection(
    *,
    direction: PositionDirection,
    current_price: Decimal,
    current_stop_price: Decimal,
    target_stop_price: Decimal,
) -> bool:
    if current_price <= 0:
        return False

    if direction == PositionDirection.LONG:
        if target_stop_price <= current_stop_price:
            return False
        if target_stop_price >= current_price:
            return False
        return True

    if direction == PositionDirection.SHORT:
        if target_stop_price >= current_stop_price:
            return False
        if target_stop_price <= current_price:
            return False
        return True

    return False
