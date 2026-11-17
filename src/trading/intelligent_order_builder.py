from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.plan import TradePlan

INTELLIGENT_ORDER_PLAN_VERSION = "0.1.0"
INTELLIGENT_ORDER_PLAN_SOURCE_SHADOW = "SHADOW"

INTELLIGENT_ORDER_STATUS_NO_TRADE_PLAN = "NO_TRADE_PLAN"
INTELLIGENT_ORDER_STATUS_SHADOW_READY = "SHADOW_READY"


def _new_intelligent_order_plan_id() -> str:
    return f"iop_{uuid4().hex}"


@dataclass(frozen=True)
class IntelligentOrderPlan:
    symbol: str
    action: str
    status: str
    reason: str
    trade_plan_id: str | None = None
    recommended_quantity: Decimal | None = None
    recommended_stop_price: Decimal | None = None
    recommended_take_profit_price: Decimal | None = None
    execution_quantity_candidate: Decimal | None = None
    execution_stop_price_candidate: Decimal | None = None
    execution_take_profit_price_candidate: Decimal | None = None
    entry_reference_price: Decimal | None = None
    execution_style: str = "MARKET"
    management_style: str = "SHADOW_ONLY"
    shadow_only: bool = True
    can_use_for_execution: bool = False
    plan_id: str = field(default_factory=_new_intelligent_order_plan_id)
    version: str = INTELLIGENT_ORDER_PLAN_VERSION
    source: str = INTELLIGENT_ORDER_PLAN_SOURCE_SHADOW
    diagnostics: dict[str, Any] = field(default_factory=dict)


class IntelligentOrderBuilder:
    """Monta o primeiro plano inteligente de ordem em modo shadow.

    Nesta fase, este builder apenas observa e registra o plano que a
    arquitetura inteligente montaria. Ele nao altera execucao.
    """

    def build_shadow_plan(
        self,
        *,
        trade_plan: TradePlan | None,
        action: str,
        planned_quantity: Decimal | None,
        planned_exit: dict[str, Decimal] | None,
        dynamic_sizing_enabled: bool,
        dynamic_exit_enabled: bool,
        architecture_mode: str,
        intelligent_activation_level: str,
    ) -> IntelligentOrderPlan:
        if trade_plan is None:
            return IntelligentOrderPlan(
                symbol="",
                action=action,
                status=INTELLIGENT_ORDER_STATUS_NO_TRADE_PLAN,
                reason="TRADE_PLAN_NOT_AVAILABLE",
                diagnostics={
                    "shadow_only": True,
                    "can_use_for_execution": False,
                    "architecture_mode": architecture_mode,
                    "intelligent_activation_level": intelligent_activation_level,
                },
            )

        exit_stop = None
        exit_take = None
        if planned_exit is not None:
            exit_stop = planned_exit.get("stop_price")
            exit_take = planned_exit.get("take_profit_price")

        return IntelligentOrderPlan(
            symbol=trade_plan.symbol,
            action=action,
            status=INTELLIGENT_ORDER_STATUS_SHADOW_READY,
            reason="INTELLIGENT_ORDER_SHADOW_ONLY",
            trade_plan_id=trade_plan.plan_id,
            recommended_quantity=trade_plan.quantity,
            recommended_stop_price=trade_plan.stop_price,
            recommended_take_profit_price=trade_plan.take_profit_price,
            execution_quantity_candidate=planned_quantity,
            execution_stop_price_candidate=exit_stop,
            execution_take_profit_price_candidate=exit_take,
            entry_reference_price=trade_plan.entry_reference_price,
            execution_style=trade_plan.execution_style,
            management_style="SHADOW_ONLY",
            shadow_only=True,
            can_use_for_execution=False,
            diagnostics={
                "shadow_only": True,
                "can_use_for_execution": False,
                "architecture_mode": architecture_mode,
                "intelligent_activation_level": intelligent_activation_level,
                "dynamic_sizing_enabled": dynamic_sizing_enabled,
                "dynamic_exit_enabled": dynamic_exit_enabled,
                "trade_plan_status": trade_plan.status,
                "trade_plan_source": trade_plan.source,
                "trade_plan_version": trade_plan.plan_version,
                "recommended_quantity": str(trade_plan.quantity),
                "recommended_stop_price": _decimal_or_none(trade_plan.stop_price),
                "recommended_take_profit_price": _decimal_or_none(trade_plan.take_profit_price),
                "execution_quantity_candidate": _decimal_or_none(planned_quantity),
                "execution_stop_price_candidate": _decimal_or_none(exit_stop),
                "execution_take_profit_price_candidate": _decimal_or_none(exit_take),
                "safety_note": (
                    "Fase 2A: plano inteligente registrado em shadow. "
                    "Nao altera quantidade, stop, take profit ou execucao."
                ),
            },
        )


def _decimal_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)
