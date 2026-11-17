from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.intelligent_exit_builder import (
    INTELLIGENT_EXIT_STATUS_SHADOW_READY,
    IntelligentExitPlan,
)

INTELLIGENT_EXIT_ACTIVATION_VERSION = "0.2.0"

INTELLIGENT_EXIT_ACTIVATION_STATUS_NO_EXIT_PLAN = "NO_INTELLIGENT_EXIT_PLAN"
INTELLIGENT_EXIT_ACTIVATION_STATUS_NOT_PAPER_ACTIVE = "NOT_PAPER_ACTIVE"
INTELLIGENT_EXIT_ACTIVATION_STATUS_NOT_ENTRY_ACTION = "NOT_ENTRY_ACTION"
INTELLIGENT_EXIT_ACTIVATION_STATUS_PLAN_NOT_READY = "PLAN_NOT_READY"
INTELLIGENT_EXIT_ACTIVATION_STATUS_DYNAMIC_EXIT_DISABLED = "DYNAMIC_EXIT_DISABLED"
INTELLIGENT_EXIT_ACTIVATION_STATUS_PROTECTION_INCOMPLETE = "PROTECTION_INCOMPLETE"
INTELLIGENT_EXIT_ACTIVATION_STATUS_APPLIED = "APPLIED"

INTELLIGENT_EXIT_ACTIVATION_ACTION_KEEP_SHADOW = "KEEP_SHADOW"
INTELLIGENT_EXIT_ACTIVATION_ACTION_APPLY_PAPER_EXIT = "APPLY_PAPER_INTELLIGENT_EXIT"
INTELLIGENT_EXIT_ACTIVATION_ACTION_APPLY_TESTNET_EXIT = "APPLY_TESTNET_INTELLIGENT_EXIT"
EXIT_ACTIVATION_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}
INTELLIGENT_EXIT_SOURCE_RECOMMENDED = "RECOMMENDED"
INTELLIGENT_EXIT_SOURCE_ADAPTIVE = "ADAPTIVE"


def _new_exit_activation_id() -> str:
    return f"iexitact_{uuid4().hex}"


@dataclass(frozen=True)
class IntelligentExitActivationDecision:
    status: str
    action: str
    reason: str
    symbol: str = ""
    trade_action: str = ""
    plan_id: str | None = None
    trade_plan_id: str | None = None
    final_stop_price: Decimal | None = None
    final_take_profit_price: Decimal | None = None
    exit_source: str = INTELLIGENT_EXIT_SOURCE_RECOMMENDED
    used_adaptive_exit: bool = False
    adaptive_exit_applied: bool = False
    recommendation_mode: str | None = None
    protection_quality: str | None = None
    applied: bool = False
    exit_applied: bool = False
    can_use_for_execution: bool = False
    paper_only: bool = True
    testnet_only: bool = False
    can_activate_testnet: bool = False
    can_activate_real: bool = False
    decision_id: str = field(default_factory=_new_exit_activation_id)
    version: str = INTELLIGENT_EXIT_ACTIVATION_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class IntelligentExitActivationPolicy:
    """Decide quando a protecao inteligente pode sair do shadow.

    Fase 5D:
    - aplica stop/take em PAPER_ACTIVE
    - aplica stop/take em TESTNET_BLOCK
    - nao aplica em TESTNET_REDUCE
    - nao aplica no real
    - nao altera fechamento de posicao
    """

    def evaluate(
        self,
        *,
        intelligent_exit_plan: IntelligentExitPlan | None,
        action: str,
        architecture_mode: str,
        intelligent_activation_level: str,
        execution_mode: str,
        dynamic_exit_enabled: bool,
    ) -> IntelligentExitActivationDecision:
        if intelligent_exit_plan is None:
            return self._build(
                status=INTELLIGENT_EXIT_ACTIVATION_STATUS_NO_EXIT_PLAN,
                action=INTELLIGENT_EXIT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Plano inteligente de protecao nao esta disponivel",
                intelligent_exit_plan=None,
                trade_action=action,
                diagnostics={
                    "architecture_mode": architecture_mode,
                    "intelligent_activation_level": intelligent_activation_level,
                    "execution_mode": execution_mode,
                    "dynamic_exit_enabled": dynamic_exit_enabled,
                },
            )

        base_diagnostics = {
            "architecture_mode": architecture_mode,
            "intelligent_activation_level": intelligent_activation_level,
            "execution_mode": execution_mode,
            "dynamic_exit_enabled": dynamic_exit_enabled,
            "original_shadow_only": intelligent_exit_plan.shadow_only,
            "original_can_use_for_execution": (intelligent_exit_plan.can_use_for_execution),
            "protection_complete": intelligent_exit_plan.protection_complete,
            "stop_valid": intelligent_exit_plan.stop_valid,
            "take_profit_valid": intelligent_exit_plan.take_profit_valid,
            "adaptive_candidate_available": getattr(
                intelligent_exit_plan,
                "adaptive_candidate_available",
                False,
            ),
            "adaptive_changed": getattr(
                intelligent_exit_plan,
                "adaptive_changed",
                False,
            ),
            "recommendation_mode": getattr(
                intelligent_exit_plan,
                "recommendation_mode",
                None,
            ),
            "protection_quality": getattr(
                intelligent_exit_plan,
                "protection_quality",
                None,
            ),
        }

        paper_active = (
            architecture_mode == "INTELLIGENT_FLOW"
            and execution_mode == "PAPER_LIVE"
            and intelligent_activation_level == "PAPER_ACTIVE"
        )
        testnet_block = (
            architecture_mode == "INTELLIGENT_FLOW"
            and execution_mode == "TESTNET_LIVE"
            and intelligent_activation_level == "TESTNET_BLOCK"
        )

        if not paper_active and not testnet_block:
            return self._build(
                status=INTELLIGENT_EXIT_ACTIVATION_STATUS_NOT_PAPER_ACTIVE,
                action=INTELLIGENT_EXIT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason=(
                    "Protecao inteligente ativa nesta fase somente em PAPER_ACTIVE ou TESTNET_BLOCK"
                ),
                intelligent_exit_plan=intelligent_exit_plan,
                trade_action=action,
                diagnostics={
                    **base_diagnostics,
                    "paper_active": paper_active,
                    "testnet_block": testnet_block,
                },
            )

        if action not in EXIT_ACTIVATION_ENTRY_ACTIONS:
            return self._build(
                status=INTELLIGENT_EXIT_ACTIVATION_STATUS_NOT_ENTRY_ACTION,
                action=INTELLIGENT_EXIT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Protecao inteligente nao altera saidas nesta fase",
                intelligent_exit_plan=intelligent_exit_plan,
                trade_action=action,
                diagnostics=base_diagnostics,
            )

        if intelligent_exit_plan.status != INTELLIGENT_EXIT_STATUS_SHADOW_READY:
            return self._build(
                status=INTELLIGENT_EXIT_ACTIVATION_STATUS_PLAN_NOT_READY,
                action=INTELLIGENT_EXIT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Plano de protecao inteligente ainda nao esta pronto",
                intelligent_exit_plan=intelligent_exit_plan,
                trade_action=action,
                diagnostics=base_diagnostics,
            )

        if not dynamic_exit_enabled:
            return self._build(
                status=INTELLIGENT_EXIT_ACTIVATION_STATUS_DYNAMIC_EXIT_DISABLED,
                action=INTELLIGENT_EXIT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Dynamic exit esta desligado",
                intelligent_exit_plan=intelligent_exit_plan,
                trade_action=action,
                diagnostics=base_diagnostics,
            )

        adaptive_stop_price = getattr(
            intelligent_exit_plan,
            "adaptive_stop_price",
            None,
        )
        adaptive_take_profit_price = getattr(
            intelligent_exit_plan,
            "adaptive_take_profit_price",
            None,
        )
        adaptive_available = bool(
            getattr(intelligent_exit_plan, "adaptive_candidate_available", False)
            and adaptive_stop_price is not None
            and adaptive_take_profit_price is not None
        )

        final_stop_price = (
            adaptive_stop_price
            if adaptive_available
            else intelligent_exit_plan.recommended_stop_price
        )
        final_take_profit_price = (
            adaptive_take_profit_price
            if adaptive_available
            else intelligent_exit_plan.recommended_take_profit_price
        )
        exit_source = (
            INTELLIGENT_EXIT_SOURCE_ADAPTIVE
            if adaptive_available
            else INTELLIGENT_EXIT_SOURCE_RECOMMENDED
        )

        if (
            not intelligent_exit_plan.protection_complete
            or final_stop_price is None
            or final_take_profit_price is None
        ):
            return self._build(
                status=INTELLIGENT_EXIT_ACTIVATION_STATUS_PROTECTION_INCOMPLETE,
                action=INTELLIGENT_EXIT_ACTIVATION_ACTION_KEEP_SHADOW,
                reason="Protecao inteligente incompleta",
                intelligent_exit_plan=intelligent_exit_plan,
                trade_action=action,
                diagnostics={
                    **base_diagnostics,
                    "exit_source": exit_source,
                    "adaptive_available": adaptive_available,
                },
            )

        activation_action = INTELLIGENT_EXIT_ACTIVATION_ACTION_APPLY_PAPER_EXIT
        activation_reason = "Protecao inteligente aplicada em PAPER_ACTIVE"
        if testnet_block:
            activation_action = INTELLIGENT_EXIT_ACTIVATION_ACTION_APPLY_TESTNET_EXIT
            activation_reason = "Protecao inteligente aplicada em TESTNET_BLOCK"

        return self._build(
            status=INTELLIGENT_EXIT_ACTIVATION_STATUS_APPLIED,
            action=activation_action,
            reason=activation_reason,
            intelligent_exit_plan=intelligent_exit_plan,
            trade_action=action,
            final_stop_price=final_stop_price,
            final_take_profit_price=final_take_profit_price,
            exit_source=exit_source,
            used_adaptive_exit=adaptive_available,
            adaptive_exit_applied=adaptive_available,
            recommendation_mode=getattr(
                intelligent_exit_plan,
                "recommendation_mode",
                None,
            ),
            protection_quality=getattr(
                intelligent_exit_plan,
                "protection_quality",
                None,
            ),
            applied=True,
            exit_applied=True,
            can_use_for_execution=True,
            paper_only=paper_active,
            testnet_only=testnet_block,
            can_activate_testnet=testnet_block,
            diagnostics={
                **base_diagnostics,
                "paper_active": paper_active,
                "testnet_block": testnet_block,
                "exit_source": exit_source,
                "used_adaptive_exit": adaptive_available,
                "adaptive_exit_applied": adaptive_available,
                "final_stop_price": str(final_stop_price),
                "final_take_profit_price": str(final_take_profit_price),
                "safety_note": (
                    "Fase 5F: protecao adaptativa pode aplicar stop/take "
                    "em PAPER_ACTIVE e TESTNET_BLOCK. REAL continua protegido."
                ),
            },
        )

    def _build(
        self,
        *,
        status: str,
        action: str,
        reason: str,
        intelligent_exit_plan: IntelligentExitPlan | None,
        trade_action: str,
        final_stop_price: Decimal | None = None,
        final_take_profit_price: Decimal | None = None,
        exit_source: str = INTELLIGENT_EXIT_SOURCE_RECOMMENDED,
        used_adaptive_exit: bool = False,
        adaptive_exit_applied: bool = False,
        recommendation_mode: str | None = None,
        protection_quality: str | None = None,
        applied: bool = False,
        exit_applied: bool = False,
        can_use_for_execution: bool = False,
        paper_only: bool = True,
        testnet_only: bool = False,
        can_activate_testnet: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> IntelligentExitActivationDecision:
        return IntelligentExitActivationDecision(
            status=status,
            action=action,
            reason=reason,
            symbol=(intelligent_exit_plan.symbol if intelligent_exit_plan is not None else ""),
            trade_action=trade_action,
            plan_id=(intelligent_exit_plan.plan_id if intelligent_exit_plan is not None else None),
            trade_plan_id=(
                intelligent_exit_plan.trade_plan_id if intelligent_exit_plan is not None else None
            ),
            final_stop_price=final_stop_price,
            final_take_profit_price=final_take_profit_price,
            exit_source=exit_source,
            used_adaptive_exit=used_adaptive_exit,
            adaptive_exit_applied=adaptive_exit_applied,
            recommendation_mode=recommendation_mode,
            protection_quality=protection_quality,
            applied=applied,
            exit_applied=exit_applied,
            can_use_for_execution=can_use_for_execution,
            paper_only=paper_only,
            testnet_only=testnet_only,
            can_activate_testnet=can_activate_testnet,
            can_activate_real=False,
            diagnostics=diagnostics or {},
        )
