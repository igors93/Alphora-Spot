from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.intelligent_order_builder import (
    INTELLIGENT_ORDER_STATUS_SHADOW_READY,
    IntelligentOrderPlan,
)

INTELLIGENT_ORDER_ACTIVATION_VERSION = "0.1.0"

INTELLIGENT_ORDER_ACTIVATION_STATUS_DISABLED = "DISABLED"
INTELLIGENT_ORDER_ACTIVATION_STATUS_NO_INTELLIGENT_PLAN = "NO_INTELLIGENT_PLAN"
INTELLIGENT_ORDER_ACTIVATION_STATUS_NOT_PAPER_ACTIVE = "NOT_PAPER_ACTIVE"
INTELLIGENT_ORDER_ACTIVATION_STATUS_NOT_ENTRY_ACTION = "NOT_ENTRY_ACTION"
INTELLIGENT_ORDER_ACTIVATION_STATUS_PLAN_NOT_READY = "PLAN_NOT_READY"
INTELLIGENT_ORDER_ACTIVATION_STATUS_MODULES_DISABLED = "MODULES_DISABLED"
INTELLIGENT_ORDER_ACTIVATION_STATUS_APPLIED = "APPLIED"

INTELLIGENT_ORDER_ACTIVATION_ACTION_KEEP_SHADOW = "KEEP_SHADOW"
INTELLIGENT_ORDER_ACTIVATION_ACTION_APPLY_PAPER = "APPLY_PAPER_INTELLIGENT_ORDER"

PAPER_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}


def _new_activation_id() -> str:
    return f"ioact_{uuid4().hex}"


@dataclass(frozen=True)
class IntelligentOrderActivationDecision:
    status: str
    action: str
    reason: str
    symbol: str = ""
    trade_action: str = ""
    plan_id: str | None = None
    trade_plan_id: str | None = None
    final_quantity: Decimal | None = None
    final_stop_price: Decimal | None = None
    final_take_profit_price: Decimal | None = None
    applied: bool = False
    quantity_applied: bool = False
    exit_applied: bool = False
    can_use_for_execution: bool = False
    paper_only: bool = True
    testnet_only: bool = False
    can_activate_testnet: bool = False
    can_activate_real: bool = False
    decision_id: str = field(default_factory=_new_activation_id)
    version: str = INTELLIGENT_ORDER_ACTIVATION_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class IntelligentOrderActivationPolicy:
    """Decide quando o plano inteligente pode sair do shadow.

    Fase 3A:
    - Apenas PAPER_ACTIVE pode aplicar.
    - Testnet e real continuam protegidos.
    - Nao inventa nova logica de stop/take; usa o IntelligentOrderPlan atual.
    """

    def evaluate(
        self,
        *,
        intelligent_order_plan: IntelligentOrderPlan | None,
        action: str,
        architecture_mode: str,
        intelligent_activation_level: str,
        execution_mode: str,
        dynamic_sizing_enabled: bool,
        dynamic_exit_enabled: bool,
    ) -> IntelligentOrderActivationDecision:
        if intelligent_order_plan is None:
            return self._build(
                status=INTELLIGENT_ORDER_ACTIVATION_STATUS_NO_INTELLIGENT_PLAN,
                action=INTELLIGENT_ORDER_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Plano inteligente nao esta disponivel",
                intelligent_order_plan=None,
                trade_action=action,
                diagnostics={
                    "architecture_mode": architecture_mode,
                    "intelligent_activation_level": intelligent_activation_level,
                    "execution_mode": execution_mode,
                },
            )

        base_diagnostics = {
            "architecture_mode": architecture_mode,
            "intelligent_activation_level": intelligent_activation_level,
            "execution_mode": execution_mode,
            "dynamic_sizing_enabled": dynamic_sizing_enabled,
            "dynamic_exit_enabled": dynamic_exit_enabled,
            "original_shadow_only": intelligent_order_plan.shadow_only,
            "original_can_use_for_execution": (intelligent_order_plan.can_use_for_execution),
        }

        if architecture_mode != "INTELLIGENT_FLOW":
            return self._build(
                status=INTELLIGENT_ORDER_ACTIVATION_STATUS_DISABLED,
                action=INTELLIGENT_ORDER_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Arquitetura inteligente esta desligada",
                intelligent_order_plan=intelligent_order_plan,
                trade_action=action,
                diagnostics=base_diagnostics,
            )

        paper_active = (
            execution_mode == "PAPER_LIVE" and intelligent_activation_level == "PAPER_ACTIVE"
        )
        testnet_reduce = (
            execution_mode == "TESTNET_LIVE" and intelligent_activation_level == "TESTNET_REDUCE"
        )
        testnet_block = (
            execution_mode == "TESTNET_LIVE" and intelligent_activation_level == "TESTNET_BLOCK"
        )

        if not paper_active and not testnet_reduce and not testnet_block:
            return self._build(
                status=INTELLIGENT_ORDER_ACTIVATION_STATUS_NOT_PAPER_ACTIVE,
                action=INTELLIGENT_ORDER_ACTIVATION_ACTION_KEEP_SHADOW,
                reason=(
                    "Plano inteligente ativo nesta fase somente em "
                    "PAPER_ACTIVE, TESTNET_REDUCE ou TESTNET_BLOCK"
                ),
                intelligent_order_plan=intelligent_order_plan,
                trade_action=action,
                diagnostics=base_diagnostics,
            )

        if action not in PAPER_ENTRY_ACTIONS:
            return self._build(
                status=INTELLIGENT_ORDER_ACTIVATION_STATUS_NOT_ENTRY_ACTION,
                action=INTELLIGENT_ORDER_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Plano inteligente em paper nao altera saidas nesta fase",
                intelligent_order_plan=intelligent_order_plan,
                trade_action=action,
                diagnostics=base_diagnostics,
            )

        if intelligent_order_plan.status != INTELLIGENT_ORDER_STATUS_SHADOW_READY:
            return self._build(
                status=INTELLIGENT_ORDER_ACTIVATION_STATUS_PLAN_NOT_READY,
                action=INTELLIGENT_ORDER_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Plano inteligente ainda nao esta pronto para ativacao",
                intelligent_order_plan=intelligent_order_plan,
                trade_action=action,
                diagnostics=base_diagnostics,
            )

        quantity_applied = (
            dynamic_sizing_enabled and intelligent_order_plan.recommended_quantity is not None
        )
        exit_allowed = paper_active or testnet_block
        exit_applied = (
            exit_allowed
            and dynamic_exit_enabled
            and (
                intelligent_order_plan.recommended_stop_price is not None
                or intelligent_order_plan.recommended_take_profit_price is not None
            )
        )

        if not quantity_applied and not exit_applied:
            return self._build(
                status=INTELLIGENT_ORDER_ACTIVATION_STATUS_MODULES_DISABLED,
                action=INTELLIGENT_ORDER_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Modulos de sizing e exit estao desligados",
                intelligent_order_plan=intelligent_order_plan,
                trade_action=action,
                diagnostics=base_diagnostics,
            )

        applied_reason = "Plano inteligente aplicado em PAPER_ACTIVE"
        if testnet_reduce:
            applied_reason = "Plano inteligente aplicou quantidade em TESTNET_REDUCE"
        if testnet_block:
            applied_reason = "Plano inteligente aplicado em TESTNET_BLOCK"

        return self._build(
            status=INTELLIGENT_ORDER_ACTIVATION_STATUS_APPLIED,
            action=INTELLIGENT_ORDER_ACTIVATION_ACTION_APPLY_PAPER,
            reason=applied_reason,
            intelligent_order_plan=intelligent_order_plan,
            trade_action=action,
            final_quantity=(
                intelligent_order_plan.recommended_quantity if quantity_applied else None
            ),
            final_stop_price=(
                intelligent_order_plan.recommended_stop_price if exit_applied else None
            ),
            final_take_profit_price=(
                intelligent_order_plan.recommended_take_profit_price if exit_applied else None
            ),
            applied=True,
            quantity_applied=quantity_applied,
            exit_applied=exit_applied,
            can_use_for_execution=True,
            paper_only=paper_active,
            testnet_only=testnet_reduce or testnet_block,
            can_activate_testnet=testnet_reduce or testnet_block,
            diagnostics={
                **base_diagnostics,
                "paper_active": paper_active,
                "testnet_reduce": testnet_reduce,
                "testnet_block": testnet_block,
                "exit_allowed": exit_allowed,
                "safety_note": (
                    "Fase 4A Mega: plano inteligente pode aplicar em paper "
                    "e em testnet controlado. Real continua protegido."
                ),
            },
        )

    def _build(
        self,
        *,
        status: str,
        action: str,
        reason: str,
        intelligent_order_plan: IntelligentOrderPlan | None,
        trade_action: str,
        final_quantity: Decimal | None = None,
        final_stop_price: Decimal | None = None,
        final_take_profit_price: Decimal | None = None,
        applied: bool = False,
        quantity_applied: bool = False,
        exit_applied: bool = False,
        can_use_for_execution: bool = False,
        paper_only: bool = True,
        testnet_only: bool = False,
        can_activate_testnet: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> IntelligentOrderActivationDecision:
        return IntelligentOrderActivationDecision(
            status=status,
            action=action,
            reason=reason,
            symbol=(intelligent_order_plan.symbol if intelligent_order_plan is not None else ""),
            trade_action=trade_action,
            plan_id=(
                intelligent_order_plan.plan_id if intelligent_order_plan is not None else None
            ),
            trade_plan_id=(
                intelligent_order_plan.trade_plan_id if intelligent_order_plan is not None else None
            ),
            final_quantity=final_quantity,
            final_stop_price=final_stop_price,
            final_take_profit_price=final_take_profit_price,
            applied=applied,
            quantity_applied=quantity_applied,
            exit_applied=exit_applied,
            can_use_for_execution=can_use_for_execution,
            paper_only=paper_only,
            testnet_only=testnet_only,
            can_activate_testnet=can_activate_testnet,
            can_activate_real=False,
            diagnostics=diagnostics or {},
        )
