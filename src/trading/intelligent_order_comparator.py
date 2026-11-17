from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.intelligent_order_builder import IntelligentOrderPlan
from trading.plan import TradePlan

INTELLIGENT_ORDER_COMPARISON_VERSION = "0.1.0"
INTELLIGENT_ORDER_COMPARISON_STATUS_COMPARED = "COMPARED"
INTELLIGENT_ORDER_COMPARISON_STATUS_NO_INTELLIGENT_PLAN = "NO_INTELLIGENT_PLAN"
INTELLIGENT_ORDER_COMPARISON_STATUS_NO_TRADE_PLAN = "NO_TRADE_PLAN"


def _new_comparison_id() -> str:
    return f"ioc_{uuid4().hex}"


@dataclass(frozen=True)
class IntelligentOrderComparison:
    symbol: str
    action: str
    status: str
    reason: str
    trade_plan_id: str | None = None
    intelligent_order_plan_id: str | None = None
    quantity_changed: bool = False
    stop_changed: bool = False
    take_profit_changed: bool = False
    execution_unchanged: bool = True
    can_use_for_execution: bool = False
    shadow_only: bool = True
    differences: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    comparison_id: str = field(default_factory=_new_comparison_id)
    version: str = INTELLIGENT_ORDER_COMPARISON_VERSION


class IntelligentOrderComparator:
    """Compara o plano inteligente com o plano operacional atual.

    Nesta fase, a comparação é apenas observacional. Ela ajuda a entender
    se o plano inteligente teria sugerido quantidade, stop ou take diferentes.
    """

    def compare(
        self,
        *,
        trade_plan: TradePlan | None,
        intelligent_order_plan: IntelligentOrderPlan | None,
        action: str,
        planned_quantity: Decimal | None,
        planned_exit: dict[str, Decimal] | None,
    ) -> IntelligentOrderComparison:
        if intelligent_order_plan is None:
            return IntelligentOrderComparison(
                symbol=trade_plan.symbol if trade_plan is not None else "",
                action=action,
                status=INTELLIGENT_ORDER_COMPARISON_STATUS_NO_INTELLIGENT_PLAN,
                reason="INTELLIGENT_ORDER_PLAN_NOT_AVAILABLE",
                trade_plan_id=trade_plan.plan_id if trade_plan is not None else None,
                diagnostics={
                    "shadow_only": True,
                    "can_use_for_execution": False,
                    "execution_unchanged": True,
                },
            )

        if trade_plan is None:
            return IntelligentOrderComparison(
                symbol=intelligent_order_plan.symbol,
                action=action,
                status=INTELLIGENT_ORDER_COMPARISON_STATUS_NO_TRADE_PLAN,
                reason="TRADE_PLAN_NOT_AVAILABLE",
                intelligent_order_plan_id=intelligent_order_plan.plan_id,
                shadow_only=intelligent_order_plan.shadow_only,
                can_use_for_execution=intelligent_order_plan.can_use_for_execution,
                diagnostics={
                    "shadow_only": intelligent_order_plan.shadow_only,
                    "can_use_for_execution": intelligent_order_plan.can_use_for_execution,
                    "execution_unchanged": True,
                },
            )

        planned_stop = None
        planned_take = None
        if planned_exit is not None:
            planned_stop = planned_exit.get("stop_price")
            planned_take = planned_exit.get("take_profit_price")

        quantity_changed = _changed(
            intelligent_order_plan.recommended_quantity,
            planned_quantity,
        )
        stop_changed = _changed(
            intelligent_order_plan.recommended_stop_price,
            planned_stop,
        )
        take_profit_changed = _changed(
            intelligent_order_plan.recommended_take_profit_price,
            planned_take,
        )

        differences = {
            "quantity": {
                "recommended": _decimal_or_none(intelligent_order_plan.recommended_quantity),
                "execution_candidate": _decimal_or_none(planned_quantity),
                "changed": quantity_changed,
            },
            "stop": {
                "recommended": _decimal_or_none(intelligent_order_plan.recommended_stop_price),
                "execution_candidate": _decimal_or_none(planned_stop),
                "changed": stop_changed,
            },
            "take_profit": {
                "recommended": _decimal_or_none(
                    intelligent_order_plan.recommended_take_profit_price
                ),
                "execution_candidate": _decimal_or_none(planned_take),
                "changed": take_profit_changed,
            },
        }

        return IntelligentOrderComparison(
            symbol=trade_plan.symbol,
            action=action,
            status=INTELLIGENT_ORDER_COMPARISON_STATUS_COMPARED,
            reason="INTELLIGENT_ORDER_SHADOW_COMPARED",
            trade_plan_id=trade_plan.plan_id,
            intelligent_order_plan_id=intelligent_order_plan.plan_id,
            quantity_changed=quantity_changed,
            stop_changed=stop_changed,
            take_profit_changed=take_profit_changed,
            execution_unchanged=not intelligent_order_plan.can_use_for_execution,
            can_use_for_execution=intelligent_order_plan.can_use_for_execution,
            shadow_only=intelligent_order_plan.shadow_only,
            differences=differences,
            diagnostics={
                "shadow_only": intelligent_order_plan.shadow_only,
                "can_use_for_execution": intelligent_order_plan.can_use_for_execution,
                "execution_unchanged": not intelligent_order_plan.can_use_for_execution,
                "comparison_scope": "quantity_stop_take_profit",
                "safety_note": (
                    "Fase 2C: comparacao observacional. "
                    "Nao altera quantidade, stop, take profit ou execucao."
                ),
            },
        )


def _changed(recommended: Decimal | None, execution_candidate: Decimal | None) -> bool:
    if recommended is None and execution_candidate is None:
        return False
    if recommended is None or execution_candidate is None:
        return True
    return recommended != execution_candidate


def _decimal_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)
