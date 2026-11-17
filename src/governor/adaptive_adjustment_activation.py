from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from governor.adaptive_adjustment_plan import (
    PLAN_ACTION_MOVE_STOP_TO_BREAKEVEN,
    PLAN_ACTION_PROTECT_PROFIT,
    PLAN_ACTION_TRAILING_STOP,
    PLAN_STATUS_SHADOW_READY,
    AdaptiveAdjustmentPlan,
)

ADAPTIVE_ADJUSTMENT_ACTIVATION_VERSION = "0.1.0"

ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_NO_PLAN = "NO_ADJUSTMENT_PLAN"
ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_DISABLED = "DISABLED"
ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_NOT_INTELLIGENT_FLOW = "NOT_INTELLIGENT_FLOW"
ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_NOT_PAPER_ACTIVE = "NOT_PAPER_ACTIVE"
ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_DYNAMIC_EXIT_DISABLED = "DYNAMIC_EXIT_DISABLED"
ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_PLAN_NOT_READY = "PLAN_NOT_READY"
ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_ACTION_NOT_SUPPORTED = "ACTION_NOT_SUPPORTED"
ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_APPLIED = "APPLIED"

ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_KEEP_SHADOW = "KEEP_SHADOW"
ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_APPLY_PAPER_ADJUSTMENT = "APPLY_PAPER_ADAPTIVE_ADJUSTMENT"

ADAPTIVE_ADJUSTMENT_EXECUTABLE_ACTIONS = {
    PLAN_ACTION_MOVE_STOP_TO_BREAKEVEN,
    PLAN_ACTION_PROTECT_PROFIT,
    PLAN_ACTION_TRAILING_STOP,
}


def _new_activation_id() -> str:
    return f"aact_{uuid4().hex}"


@dataclass(frozen=True)
class AdaptiveAdjustmentActivationDecision:
    status: str
    activation_action: str
    reason: str
    symbol: str = ""
    plan_id: str | None = None
    plan_action: str | None = None
    trade_plan_id: str | None = None
    execution_enabled: bool = False
    applied: bool = False
    can_use_for_execution: bool = False
    paper_only: bool = True
    testnet_only: bool = False
    can_activate_testnet: bool = False
    can_activate_real: bool = False
    decision_id: str = field(default_factory=_new_activation_id)
    version: str = ADAPTIVE_ADJUSTMENT_ACTIVATION_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class AdaptiveAdjustmentActivationPolicy:
    """Decide quando o ajuste adaptativo pode sair do shadow.

    Fase 7B:
    - aplica apenas em PAPER_ACTIVE
    - aplica apenas ajustes de protecao: breakeven, protect profit e trailing
    - nao aplica reducao parcial automaticamente
    - nao aplica saida total automaticamente
    - nao aplica em testnet ou real
    """

    def evaluate(
        self,
        *,
        adjustment_plan: AdaptiveAdjustmentPlan | None,
        architecture_mode: str,
        intelligent_activation_level: str,
        execution_mode: str,
        adaptive_execution_enabled: bool,
        dynamic_exit_enabled: bool,
    ) -> AdaptiveAdjustmentActivationDecision:
        if adjustment_plan is None:
            return self._build(
                status=ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_NO_PLAN,
                activation_action=ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Plano adaptativo nao esta disponivel",
                adjustment_plan=None,
                diagnostics={
                    "architecture_mode": architecture_mode,
                    "intelligent_activation_level": intelligent_activation_level,
                    "execution_mode": execution_mode,
                    "adaptive_execution_enabled": adaptive_execution_enabled,
                    "dynamic_exit_enabled": dynamic_exit_enabled,
                },
            )

        base_diagnostics = {
            "architecture_mode": architecture_mode,
            "intelligent_activation_level": intelligent_activation_level,
            "execution_mode": execution_mode,
            "adaptive_execution_enabled": adaptive_execution_enabled,
            "dynamic_exit_enabled": dynamic_exit_enabled,
            "plan_status": adjustment_plan.status,
            "plan_action": adjustment_plan.action,
            "requires_order_change": adjustment_plan.requires_order_change,
            "requires_manual_review": adjustment_plan.requires_manual_review,
            "should_reduce_position": adjustment_plan.should_reduce_position,
            "should_exit_position": adjustment_plan.should_exit_position,
            "management_mode": adjustment_plan.management_mode,
            "safety_note": (
                "Fase 7B: gerenciamento adaptativo automatico somente "
                "em PAPER_ACTIVE para ajustes de protecao. Reducao parcial, "
                "saida total, testnet e real continuam protegidos."
            ),
        }

        if not adaptive_execution_enabled:
            return self._build(
                status=ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_DISABLED,
                activation_action=ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Adaptive execution esta desligado",
                adjustment_plan=adjustment_plan,
                diagnostics=base_diagnostics,
            )

        if architecture_mode != "INTELLIGENT_FLOW":
            return self._build(
                status=ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_NOT_INTELLIGENT_FLOW,
                activation_action=ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Gerenciamento adaptativo exige arquitetura inteligente",
                adjustment_plan=adjustment_plan,
                diagnostics=base_diagnostics,
            )

        paper_active = (
            execution_mode == "PAPER_LIVE" and intelligent_activation_level == "PAPER_ACTIVE"
        )
        if not paper_active:
            return self._build(
                status=ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_NOT_PAPER_ACTIVE,
                activation_action=ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Gerenciamento adaptativo automatico somente em PAPER_ACTIVE",
                adjustment_plan=adjustment_plan,
                diagnostics={
                    **base_diagnostics,
                    "paper_active": paper_active,
                },
            )

        if not dynamic_exit_enabled:
            return self._build(
                status=ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_DYNAMIC_EXIT_DISABLED,
                activation_action=ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Dynamic exit esta desligado",
                adjustment_plan=adjustment_plan,
                diagnostics=base_diagnostics,
            )

        if adjustment_plan.requires_manual_review:
            return self._build(
                status=(ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_MANUAL_REVIEW_REQUIRED),
                activation_action=ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Plano exige revisao manual",
                adjustment_plan=adjustment_plan,
                diagnostics=base_diagnostics,
            )

        if (
            adjustment_plan.status != PLAN_STATUS_SHADOW_READY
            or not adjustment_plan.requires_order_change
        ):
            return self._build(
                status=ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_PLAN_NOT_READY,
                activation_action=ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Plano adaptativo ainda nao esta pronto para execucao",
                adjustment_plan=adjustment_plan,
                diagnostics=base_diagnostics,
            )

        if adjustment_plan.action not in ADAPTIVE_ADJUSTMENT_EXECUTABLE_ACTIONS:
            return self._build(
                status=ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_ACTION_NOT_SUPPORTED,
                activation_action=ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Acao adaptativa nao suportada para execucao automatica",
                adjustment_plan=adjustment_plan,
                diagnostics=base_diagnostics,
            )

        return self._build(
            status=ADAPTIVE_ADJUSTMENT_ACTIVATION_STATUS_APPLIED,
            activation_action=(ADAPTIVE_ADJUSTMENT_ACTIVATION_ACTION_APPLY_PAPER_ADJUSTMENT),
            reason="Ajuste adaptativo aplicado em PAPER_ACTIVE",
            adjustment_plan=adjustment_plan,
            execution_enabled=True,
            applied=True,
            can_use_for_execution=True,
            paper_only=True,
            diagnostics={
                **base_diagnostics,
                "paper_active": paper_active,
            },
        )

    def _build(
        self,
        *,
        status: str,
        activation_action: str,
        reason: str,
        adjustment_plan: AdaptiveAdjustmentPlan | None,
        execution_enabled: bool = False,
        applied: bool = False,
        can_use_for_execution: bool = False,
        paper_only: bool = True,
        diagnostics: dict[str, Any] | None = None,
    ) -> AdaptiveAdjustmentActivationDecision:
        return AdaptiveAdjustmentActivationDecision(
            status=status,
            activation_action=activation_action,
            reason=reason,
            symbol=adjustment_plan.symbol if adjustment_plan is not None else "",
            plan_id=adjustment_plan.plan_id if adjustment_plan is not None else None,
            plan_action=adjustment_plan.action if adjustment_plan is not None else None,
            trade_plan_id=(adjustment_plan.trade_plan_id if adjustment_plan is not None else None),
            execution_enabled=execution_enabled,
            applied=applied,
            can_use_for_execution=can_use_for_execution,
            paper_only=paper_only,
            testnet_only=False,
            can_activate_testnet=False,
            can_activate_real=False,
            diagnostics=diagnostics or {},
        )
