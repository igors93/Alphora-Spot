from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.learning_execution_shadow import (
    LEARNING_EXECUTION_STATUS_WOULD_BLOCK,
    LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE,
    LearningExecutionShadowDecision,
)

LEARNING_TESTNET_EXECUTION_VERSION = "1.0.0"

TESTNET_EXECUTION_MODE = "TESTNET_LIVE"
INTELLIGENT_ARCHITECTURE_MODE = "INTELLIGENT_FLOW"
TESTNET_EXECUTION_REDUCE_LEVEL = "TESTNET_REDUCE"
TESTNET_EXECUTION_BLOCK_LEVEL = "TESTNET_BLOCK"
TESTNET_EXECUTION_ACTIVATION_LEVEL = TESTNET_EXECUTION_REDUCE_LEVEL

TESTNET_EXECUTION_STATUS_STORE_DISABLED = "STORE_DISABLED"
TESTNET_EXECUTION_STATUS_NOT_INTELLIGENT_FLOW = "NOT_INTELLIGENT_FLOW"
TESTNET_EXECUTION_STATUS_NOT_TESTNET_REDUCE_LEVEL = "NOT_TESTNET_REDUCE_LEVEL"
TESTNET_EXECUTION_STATUS_NOT_TESTNET_MODE = "NOT_TESTNET_MODE"
TESTNET_EXECUTION_STATUS_NO_SHADOW_DECISION = "NO_SHADOW_DECISION"
TESTNET_EXECUTION_STATUS_NO_ACTIVATION_REPORT = "NO_ACTIVATION_REPORT"
TESTNET_EXECUTION_STATUS_NOT_ENTRY_ACTION = "NOT_ENTRY_ACTION"
TESTNET_EXECUTION_STATUS_ALLOWED = "ALLOWED"
TESTNET_EXECUTION_STATUS_REDUCTION_APPLIED = "REDUCTION_APPLIED"
TESTNET_EXECUTION_STATUS_REDUCTION_NOT_ELIGIBLE = "REDUCTION_NOT_ELIGIBLE"
TESTNET_EXECUTION_STATUS_BLOCK_SIGNAL_NOT_APPLIED = "BLOCK_SIGNAL_NOT_APPLIED"
TESTNET_EXECUTION_STATUS_BLOCK_APPLIED = "BLOCK_APPLIED"
TESTNET_EXECUTION_STATUS_INVALID_REDUCTION_QUANTITY = "INVALID_REDUCTION_QUANTITY"

TESTNET_EXECUTION_ACTION_ALLOW = "ALLOW_TESTNET_EXECUTION"
TESTNET_EXECUTION_ACTION_REDUCE_SIZE = "APPLY_TESTNET_REDUCTION"
TESTNET_EXECUTION_ACTION_BLOCK = "APPLY_TESTNET_BLOCK"
TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL = "KEEP_ORIGINAL_EXECUTION"

TESTNET_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}


def _new_testnet_execution_id() -> str:
    return f"ltexec_{uuid4().hex}"


@dataclass(frozen=True)
class LearningTestnetExecutionSettings:
    execution_mode: str = ""
    architecture_mode: str = ""
    intelligent_activation_level: str = ""
    learning_store_enabled: bool = False

    @classmethod
    def from_config(cls, config) -> "LearningTestnetExecutionSettings":
        return cls(
            execution_mode=_enum_value(config.execution_mode),
            architecture_mode=_enum_value(config.architecture_mode),
            intelligent_activation_level=_enum_value(
                config.intelligent_activation_level,
            ),
            learning_store_enabled=config.learning_store_enabled,
        )


@dataclass(frozen=True)
class LearningTestnetExecutionDecision:
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
    testnet_only: bool = True
    can_activate_testnet: bool = False
    can_activate_real: bool = False
    decision_id: str = field(default_factory=_new_testnet_execution_id)
    version: str = LEARNING_TESTNET_EXECUTION_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningTestnetExecutionPolicy:
    def __init__(
        self,
        settings: LearningTestnetExecutionSettings | None = None,
    ):
        self.settings = settings or LearningTestnetExecutionSettings()

    def evaluate(
        self,
        *,
        shadow_decision: LearningExecutionShadowDecision | None,
        activation_report_payload: dict[str, Any] | None,
    ) -> LearningTestnetExecutionDecision:
        settings = self.settings

        if not settings.learning_store_enabled:
            return self._build(
                status=TESTNET_EXECUTION_STATUS_STORE_DISABLED,
                action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Learning store esta desligado",
                shadow_decision=shadow_decision,
                final_quantity=_original_quantity(shadow_decision),
            )

        if settings.architecture_mode != INTELLIGENT_ARCHITECTURE_MODE:
            return self._build(
                status=TESTNET_EXECUTION_STATUS_NOT_INTELLIGENT_FLOW,
                action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Reducao testnet exige arquitetura inteligente",
                shadow_decision=shadow_decision,
                final_quantity=_original_quantity(shadow_decision),
            )

        testnet_reduce = settings.intelligent_activation_level == TESTNET_EXECUTION_REDUCE_LEVEL
        testnet_block = settings.intelligent_activation_level == TESTNET_EXECUTION_BLOCK_LEVEL

        if not testnet_reduce and not testnet_block:
            return self._build(
                status=TESTNET_EXECUTION_STATUS_NOT_TESTNET_REDUCE_LEVEL,
                action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason=("Reducao/bloqueio testnet exige nivel TESTNET_REDUCE ou TESTNET_BLOCK"),
                shadow_decision=shadow_decision,
                final_quantity=_original_quantity(shadow_decision),
            )

        if settings.execution_mode != TESTNET_EXECUTION_MODE:
            return self._build(
                status=TESTNET_EXECUTION_STATUS_NOT_TESTNET_MODE,
                action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Reducao testnet so pode alterar execucao em TESTNET_LIVE",
                shadow_decision=shadow_decision,
                final_quantity=_original_quantity(shadow_decision),
            )

        if shadow_decision is None:
            return self._build(
                status=TESTNET_EXECUTION_STATUS_NO_SHADOW_DECISION,
                action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Decisao shadow de learning nao esta disponivel",
                shadow_decision=None,
                final_quantity=None,
            )

        if shadow_decision.action not in TESTNET_ENTRY_ACTIONS:
            return self._build(
                status=TESTNET_EXECUTION_STATUS_NOT_ENTRY_ACTION,
                action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Learning testnet nao reduz saidas",
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        if activation_report_payload is None:
            return self._build(
                status=TESTNET_EXECUTION_STATUS_NO_ACTIVATION_REPORT,
                action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason="Relatorio de ativacao do learning ainda nao esta disponivel",
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        if shadow_decision.status == LEARNING_EXECUTION_STATUS_WOULD_BLOCK:
            if testnet_block and _bool_payload(
                activation_report_payload,
                "can_apply_block_in_paper",
            ):
                return self._build(
                    status=TESTNET_EXECUTION_STATUS_BLOCK_APPLIED,
                    action=TESTNET_EXECUTION_ACTION_BLOCK,
                    reason="Learning bloqueou a entrada em TESTNET_BLOCK",
                    shadow_decision=shadow_decision,
                    final_quantity=Decimal("0"),
                    applied=True,
                    blocked=True,
                    can_use_for_execution=True,
                    can_activate_testnet=True,
                )

            return self._build(
                status=TESTNET_EXECUTION_STATUS_BLOCK_SIGNAL_NOT_APPLIED,
                action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason=(
                    "Learning sugeriu bloqueio, mas bloqueio em testnet "
                    "nao esta elegivel neste nivel"
                ),
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        if shadow_decision.status == LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE:
            if not _valid_reduction_quantity(
                original_quantity=shadow_decision.original_quantity,
                final_quantity=shadow_decision.shadow_quantity,
            ):
                return self._build(
                    status=TESTNET_EXECUTION_STATUS_INVALID_REDUCTION_QUANTITY,
                    action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                    reason="Learning sugeriu reducao testnet com quantidade invalida",
                    shadow_decision=shadow_decision,
                    final_quantity=shadow_decision.original_quantity,
                )

            if _bool_payload(
                activation_report_payload,
                "can_apply_reduction_in_paper",
            ):
                return self._build(
                    status=TESTNET_EXECUTION_STATUS_REDUCTION_APPLIED,
                    action=TESTNET_EXECUTION_ACTION_REDUCE_SIZE,
                    reason="Learning reduziu a quantidade em TESTNET_LIVE",
                    shadow_decision=shadow_decision,
                    final_quantity=shadow_decision.shadow_quantity,
                    applied=True,
                    reduced=True,
                    can_use_for_execution=True,
                    can_activate_testnet=True,
                )

            return self._build(
                status=TESTNET_EXECUTION_STATUS_REDUCTION_NOT_ELIGIBLE,
                action=TESTNET_EXECUTION_ACTION_KEEP_ORIGINAL,
                reason=(
                    "Learning sugeriu reducao, mas criterios minimos ainda "
                    "nao permitem reducao em testnet"
                ),
                shadow_decision=shadow_decision,
                final_quantity=shadow_decision.original_quantity,
            )

        return self._build(
            status=TESTNET_EXECUTION_STATUS_ALLOWED,
            action=TESTNET_EXECUTION_ACTION_ALLOW,
            reason="Learning nao alterou a execucao em testnet",
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
        can_activate_testnet: bool = False,
    ) -> LearningTestnetExecutionDecision:
        return LearningTestnetExecutionDecision(
            status=status,
            action=action,
            reason=reason,
            symbol=shadow_decision.symbol if shadow_decision is not None else "",
            trade_action=shadow_decision.action if shadow_decision is not None else "",
            plan_id=shadow_decision.plan_id if shadow_decision is not None else None,
            shadow_decision_id=(
                shadow_decision.decision_id if shadow_decision is not None else None
            ),
            original_quantity=_original_quantity(shadow_decision),
            final_quantity=final_quantity,
            applied=applied,
            reduced=reduced,
            blocked=blocked,
            can_use_for_execution=can_use_for_execution,
            testnet_only=True,
            can_activate_testnet=can_activate_testnet,
            can_activate_real=False,
            diagnostics={
                "execution_mode": self.settings.execution_mode,
                "architecture_mode": self.settings.architecture_mode,
                "intelligent_activation_level": (self.settings.intelligent_activation_level),
                "learning_store_enabled": self.settings.learning_store_enabled,
                "safety_note": (
                    "Esta etapa permite reducao e bloqueio apenas em TESTNET_LIVE. "
                    "Qualquer acao no real continua desativada."
                ),
            },
        )


def _enum_value(value) -> str:
    return str(getattr(value, "value", value))


def _original_quantity(
    shadow_decision: LearningExecutionShadowDecision | None,
) -> Decimal | None:
    if shadow_decision is None:
        return None

    return shadow_decision.original_quantity


def _bool_payload(payload: dict[str, Any] | None, key: str) -> bool:
    if payload is None:
        return False

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
