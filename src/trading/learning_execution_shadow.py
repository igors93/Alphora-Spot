from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.learning_plan_guard import (
    LEARNING_GUARD_STATUS_WOULD_BLOCK,
    LEARNING_GUARD_STATUS_WOULD_REDUCE_SIZE,
)
from trading.plan import TradePlan

LEARNING_EXECUTION_SHADOW_VERSION = "1.0.0"

LEARNING_EXECUTION_STATUS_NO_PLAN = "NO_PLAN"
LEARNING_EXECUTION_STATUS_NO_GUARD = "NO_GUARD"
LEARNING_EXECUTION_STATUS_ALLOW = "ALLOW"
LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE = "WOULD_REDUCE_SIZE"
LEARNING_EXECUTION_STATUS_WOULD_BLOCK = "WOULD_BLOCK"

LEARNING_EXECUTION_ACTION_ALLOW = "ALLOW_EXECUTION_SHADOW"
LEARNING_EXECUTION_ACTION_REDUCE_SIZE = "REDUCE_SIZE_SHADOW"
LEARNING_EXECUTION_ACTION_BLOCK = "BLOCK_EXECUTION_SHADOW"


def _new_execution_shadow_id() -> str:
    return f"lex_{uuid4().hex}"


@dataclass(frozen=True)
class LearningExecutionShadowDecision:
    symbol: str
    action: str
    status: str
    suggested_action: str
    reason: str
    plan_id: str | None = None
    guard_id: str | None = None
    guard_status: str = ""
    original_quantity: Decimal | None = None
    shadow_quantity: Decimal | None = None
    suggested_size_multiplier: Decimal = Decimal("1")
    would_block_trade: bool = False
    would_reduce_size: bool = False
    can_use_for_execution: bool = False
    shadow_only: bool = True
    decision_id: str = field(default_factory=_new_execution_shadow_id)
    version: str = LEARNING_EXECUTION_SHADOW_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningExecutionShadowPolicy:
    def evaluate(
        self,
        *,
        plan: TradePlan | None,
        action: str,
        planned_quantity: Decimal | None,
    ) -> LearningExecutionShadowDecision:
        if plan is None:
            return LearningExecutionShadowDecision(
                symbol="",
                action=action,
                status=LEARNING_EXECUTION_STATUS_NO_PLAN,
                suggested_action=LEARNING_EXECUTION_ACTION_ALLOW,
                reason="TRADE_PLAN_NOT_AVAILABLE",
                original_quantity=planned_quantity,
                shadow_quantity=planned_quantity,
                diagnostics={
                    "shadow_only": True,
                    "can_use_for_execution": False,
                },
            )

        guard = _guard_from_plan(plan)
        if guard is None:
            return LearningExecutionShadowDecision(
                symbol=plan.symbol,
                action=action,
                plan_id=plan.plan_id,
                status=LEARNING_EXECUTION_STATUS_NO_GUARD,
                suggested_action=LEARNING_EXECUTION_ACTION_ALLOW,
                reason="LEARNING_GUARD_NOT_AVAILABLE",
                original_quantity=planned_quantity,
                shadow_quantity=planned_quantity,
                diagnostics={
                    "shadow_only": True,
                    "can_use_for_execution": False,
                },
            )

        guard_status = str(guard.get("status", ""))
        guard_id = guard.get("guard_id")
        multiplier = _decimal_or_default(
            guard.get("suggested_size_multiplier"),
            Decimal("1"),
        )

        if guard_status == LEARNING_GUARD_STATUS_WOULD_BLOCK:
            return LearningExecutionShadowDecision(
                symbol=plan.symbol,
                action=action,
                plan_id=plan.plan_id,
                guard_id=str(guard_id) if guard_id is not None else None,
                guard_status=guard_status,
                status=LEARNING_EXECUTION_STATUS_WOULD_BLOCK,
                suggested_action=LEARNING_EXECUTION_ACTION_BLOCK,
                reason="LEARNING_GUARD_WOULD_BLOCK_EXECUTION",
                original_quantity=planned_quantity,
                shadow_quantity=Decimal("0"),
                suggested_size_multiplier=Decimal("0"),
                would_block_trade=True,
                diagnostics=_diagnostics_from_guard(guard),
            )

        if guard_status == LEARNING_GUARD_STATUS_WOULD_REDUCE_SIZE:
            shadow_quantity = _multiply_quantity(planned_quantity, multiplier)
            return LearningExecutionShadowDecision(
                symbol=plan.symbol,
                action=action,
                plan_id=plan.plan_id,
                guard_id=str(guard_id) if guard_id is not None else None,
                guard_status=guard_status,
                status=LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE,
                suggested_action=LEARNING_EXECUTION_ACTION_REDUCE_SIZE,
                reason="LEARNING_GUARD_WOULD_REDUCE_EXECUTION_SIZE",
                original_quantity=planned_quantity,
                shadow_quantity=shadow_quantity,
                suggested_size_multiplier=multiplier,
                would_reduce_size=True,
                diagnostics=_diagnostics_from_guard(guard),
            )

        return LearningExecutionShadowDecision(
            symbol=plan.symbol,
            action=action,
            plan_id=plan.plan_id,
            guard_id=str(guard_id) if guard_id is not None else None,
            guard_status=guard_status,
            status=LEARNING_EXECUTION_STATUS_ALLOW,
            suggested_action=LEARNING_EXECUTION_ACTION_ALLOW,
            reason="LEARNING_GUARD_DOES_NOT_CHANGE_EXECUTION",
            original_quantity=planned_quantity,
            shadow_quantity=planned_quantity,
            suggested_size_multiplier=Decimal("1"),
            diagnostics=_diagnostics_from_guard(guard),
        )


def _guard_from_plan(plan: TradePlan) -> dict[str, Any] | None:
    diagnostics = plan.diagnostics or {}
    guard = diagnostics.get("learning_guard")
    return guard if isinstance(guard, dict) else None


def _multiply_quantity(
    quantity: Decimal | None,
    multiplier: Decimal,
) -> Decimal | None:
    if quantity is None:
        return None

    return quantity * multiplier


def _decimal_or_default(value, default: Decimal) -> Decimal:
    if value is None:
        return default

    try:
        return Decimal(str(value))
    except Exception:
        return default


def _diagnostics_from_guard(guard: dict[str, Any]) -> dict[str, Any]:
    return {
        "shadow_only": True,
        "can_use_for_execution": False,
        "guard_status": guard.get("status"),
        "guard_suggested_action": guard.get("suggested_action"),
        "guard_reason": guard.get("reason"),
        "guard_would_block_trade": guard.get("would_block_trade"),
        "guard_would_reduce_size": guard.get("would_reduce_size"),
        "guard_suggested_size_multiplier": str(guard.get("suggested_size_multiplier")),
    }
