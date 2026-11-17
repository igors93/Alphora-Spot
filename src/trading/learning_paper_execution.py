from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.learning_execution_shadow import (
    LEARNING_EXECUTION_STATUS_WOULD_BLOCK,
    LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE,
    LearningExecutionShadowDecision,
)
from trading.learning_paper_activation import (
    PAPER_EXECUTION_MODE,
    LearningPaperActivationSettings,
)

LEARNING_PAPER_EXECUTION_VERSION = "1.0.0"

PAPER_EXECUTION_STATUS_DISABLED = "DISABLED"
PAPER_EXECUTION_STATUS_NO_SHADOW_DECISION = "NO_SHADOW_DECISION"
PAPER_EXECUTION_STATUS_NO_ACTIVATION_REPORT = "NO_ACTIVATION_REPORT"
PAPER_EXECUTION_STATUS_NOT_PAPER_MODE = "NOT_PAPER_MODE"
PAPER_EXECUTION_STATUS_NOT_ENTRY_ACTION = "NOT_ENTRY_ACTION"
PAPER_EXECUTION_STATUS_ALLOWED = "ALLOWED"
PAPER_EXECUTION_STATUS_REDUCTION_APPLIED = "REDUCTION_APPLIED"
PAPER_EXECUTION_STATUS_BLOCK_APPLIED = "BLOCK_APPLIED"
PAPER_EXECUTION_STATUS_REDUCTION_NOT_ELIGIBLE = "REDUCTION_NOT_ELIGIBLE"
PAPER_EXECUTION_STATUS_BLOCK_NOT_ELIGIBLE = "BLOCK_NOT_ELIGIBLE"
PAPER_EXECUTION_STATUS_INVALID_REDUCTION_QUANTITY = "INVALID_REDUCTION_QUANTITY"

PAPER_EXECUTION_ACTION_ALLOW = "ALLOW_PAPER_EXECUTION"
PAPER_EXECUTION_ACTION_REDUCE_SIZE = "APPLY_PAPER_REDUCTION"
PAPER_EXECUTION_ACTION_BLOCK = "APPLY_PAPER_BLOCK"
PAPER_EXECUTION_ACTION_KEEP_ORIGINAL = "KEEP_ORIGINAL_EXECUTION"

PAPER_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}


def _new_paper_execution_id() -> str:
    return f"lpexec_{uuid4().hex}"


@dataclass(frozen=True)
class LearningPaperExecutionDecision:
    status: str
    action: str
    reason: str
    symbol: str = ""
    trade_action: str = ""
    plan_id: str | None = None
    shadow_decision_id: str | None = None
    original_quantity: Decimal | None = None
    final_quantity: Decimal | None = None
    applied: bool = False
    reduced: bool = False
    blocked: bool = False
    can_use_for_execution: bool = False
    paper_only: bool = True
    can_activate_testnet: bool = False
    can_activate_real: bool = False
    decision_id: str = field(default_factory=_new_paper_execution_id)
    version: str = LEARNING_PAPER_EXECUTION_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningPaperExecutionPolicy:
    def __init__(self, settings: LearningPaperActivationSettings | None = None):
        self.settings = settings or LearningPaperActivationSettings()

    def evaluate(
        self,
        *,
        shadow_decision: LearningExecutionShadowDecision | None,
        activation_report_payload: dict[str, Any] | None,
    ) -> LearningPaperExecutionDecision:
        settings = self.settings

        if shadow_decision is None:
            return self._build(
                status=PAPER_EXECUTION_STATUS_NO_SHADOW_DECISION,
                action=PAPER_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Decisao shadow de learning nao esta disponivel",
                shadow_decision=None,
                final_quantity=None,
            )

        if not settings.learning_store_enabled or not settings.paper_activation_enabled:
            return self._build(
                status=PAPER_EXECUTION_STATUS_DISABLED,
                action=PAPER_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Execucao ativa do learning em paper esta desligada por flag",
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        if settings.execution_mode != PAPER_EXECUTION_MODE:
            return self._build(
                status=PAPER_EXECUTION_STATUS_NOT_PAPER_MODE,
                action=PAPER_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Learning ativo so pode alterar execucao em PAPER_LIVE",
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        if shadow_decision.action not in PAPER_ENTRY_ACTIONS:
            return self._build(
                status=PAPER_EXECUTION_STATUS_NOT_ENTRY_ACTION,
                action=PAPER_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Learning em paper nao bloqueia nem reduz saidas",
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        if activation_report_payload is None:
            return self._build(
                status=PAPER_EXECUTION_STATUS_NO_ACTIVATION_REPORT,
                action=PAPER_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Relatorio de ativacao do learning ainda nao esta disponivel",
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        if shadow_decision.status == LEARNING_EXECUTION_STATUS_WOULD_BLOCK:
            if settings.paper_block_enabled and _bool_payload(
                activation_report_payload, "can_apply_block_in_paper"
            ):
                return self._build(
                    status=PAPER_EXECUTION_STATUS_BLOCK_APPLIED,
                    action=PAPER_EXECUTION_ACTION_BLOCK,
                    reason="Learning bloqueou a entrada em PAPER_LIVE",
                    shadow_decision=shadow_decision,
                    final_quantity=Decimal("0"),
                    applied=True,
                    blocked=True,
                    can_use_for_execution=True,
                )

            return self._build(
                status=PAPER_EXECUTION_STATUS_BLOCK_NOT_ELIGIBLE,
                action=PAPER_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Learning sugeriu bloqueio, mas bloqueio em paper nao esta elegivel",
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        if shadow_decision.status == LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE:
            if not _valid_reduction_quantity(
                original_quantity=shadow_decision.original_quantity,
                final_quantity=shadow_decision.shadow_quantity,
            ):
                return self._build(
                    status=PAPER_EXECUTION_STATUS_INVALID_REDUCTION_QUANTITY,
                    action=PAPER_EXECUTION_ACTION_KEEP_ORIGINAL,
                    reason="Learning sugeriu reducao com quantidade invalida",
                    shadow_decision=shadow_decision,
                    final_quantity=shadow_decision.original_quantity,
                )

            if settings.paper_reduction_enabled and _bool_payload(
                activation_report_payload,
                "can_apply_reduction_in_paper",
            ):
                return self._build(
                    status=PAPER_EXECUTION_STATUS_REDUCTION_APPLIED,
                    action=PAPER_EXECUTION_ACTION_REDUCE_SIZE,
                    reason="Learning reduziu a quantidade em PAPER_LIVE",
                    shadow_decision=shadow_decision,
                    final_quantity=shadow_decision.shadow_quantity,
                    applied=True,
                    reduced=True,
                    can_use_for_execution=True,
                )

            return self._build(
                status=PAPER_EXECUTION_STATUS_REDUCTION_NOT_ELIGIBLE,
                action=PAPER_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Learning sugeriu reducao, mas reducao em paper nao esta elegivel",
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        return self._build(
            status=PAPER_EXECUTION_STATUS_ALLOWED,
            action=PAPER_EXECUTION_ACTION_ALLOW,
            reason="Learning nao alterou a execucao em paper",
            shadow_decision=shadow_decision,
            final_quantity=shadow_decision.original_quantity,
        )

    def _build(
        self,
        *,
        status: str,
        action: str,
        reason: str,
        shadow_decision: LearningExecutionShadowDecision | None,
        final_quantity: Decimal | None,
        applied: bool = False,
        reduced: bool = False,
        blocked: bool = False,
        can_use_for_execution: bool = False,
    ) -> LearningPaperExecutionDecision:
        return LearningPaperExecutionDecision(
            status=status,
            action=action,
            reason=reason,
            symbol=shadow_decision.symbol if shadow_decision is not None else "",
            trade_action=shadow_decision.action if shadow_decision is not None else "",
            plan_id=shadow_decision.plan_id if shadow_decision is not None else None,
            shadow_decision_id=(
                shadow_decision.decision_id if shadow_decision is not None else None
            ),
            original_quantity=(
                shadow_decision.original_quantity if shadow_decision is not None else None
            ),
            final_quantity=final_quantity,
            applied=applied,
            reduced=reduced,
            blocked=blocked,
            can_use_for_execution=can_use_for_execution,
            paper_only=True,
            can_activate_testnet=False,
            can_activate_real=False,
            diagnostics={
                "execution_mode": self.settings.execution_mode,
                "learning_store_enabled": self.settings.learning_store_enabled,
                "paper_activation_enabled": self.settings.paper_activation_enabled,
                "paper_reduction_enabled": self.settings.paper_reduction_enabled,
                "paper_block_enabled": self.settings.paper_block_enabled,
            },
        )


def _bool_payload(payload: dict[str, Any], key: str) -> bool:
    return bool(payload.get(key, False))


def _valid_reduction_quantity(
    *,
    original_quantity: Decimal | None,
    final_quantity: Decimal | None,
) -> bool:
    if final_quantity is None:
        return False

    if final_quantity <= 0:
        return False

    if original_quantity is None:
        return True

    if original_quantity <= 0:
        return False

    return final_quantity < original_quantity
