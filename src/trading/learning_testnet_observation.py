from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.learning_execution_shadow import (
    LEARNING_EXECUTION_STATUS_WOULD_BLOCK,
    LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE,
    LearningExecutionShadowDecision,
)

LEARNING_TESTNET_OBSERVATION_VERSION = "1.0.0"

TESTNET_OBSERVATION_STATUS_DISABLED = "DISABLED"
TESTNET_OBSERVATION_STATUS_STORE_DISABLED = "STORE_DISABLED"
TESTNET_OBSERVATION_STATUS_NOT_TESTNET_MODE = "NOT_TESTNET_MODE"
TESTNET_OBSERVATION_STATUS_NO_SHADOW_DECISION = "NO_SHADOW_DECISION"
TESTNET_OBSERVATION_STATUS_NO_ACTIVATION_REPORT = "NO_ACTIVATION_REPORT"
TESTNET_OBSERVATION_STATUS_OBSERVED_NO_ACTION = "OBSERVED_NO_ACTION"
TESTNET_OBSERVATION_STATUS_OBSERVED_REDUCTION_SIGNAL = "OBSERVED_REDUCTION_SIGNAL"
TESTNET_OBSERVATION_STATUS_OBSERVED_BLOCK_SIGNAL = "OBSERVED_BLOCK_SIGNAL"

TESTNET_OBSERVATION_ACTION_KEEP_OBSERVING = "KEEP_OBSERVING"
TESTNET_OBSERVATION_ACTION_COLLECT_MORE_DATA = "COLLECT_MORE_DATA"
TESTNET_OBSERVATION_ACTION_OBSERVE_NO_CHANGE = "OBSERVE_NO_CHANGE"
TESTNET_OBSERVATION_ACTION_OBSERVE_REDUCTION_SIGNAL = "OBSERVE_REDUCTION_SIGNAL"
TESTNET_OBSERVATION_ACTION_OBSERVE_BLOCK_SIGNAL = "OBSERVE_BLOCK_SIGNAL"

TESTNET_EXECUTION_MODE = "TESTNET_LIVE"
TESTNET_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}


def _new_testnet_observation_id() -> str:
    return f"ltobs_{uuid4().hex}"


@dataclass(frozen=True)
class LearningTestnetObservationSettings:
    execution_mode: str = ""
    learning_store_enabled: bool = False
    testnet_observation_enabled: bool = False

    @classmethod
    def from_config(cls, config) -> "LearningTestnetObservationSettings":
        return cls(
            execution_mode=config.execution_mode.value,
            learning_store_enabled=config.learning_store_enabled,
            testnet_observation_enabled=config.learning_testnet_observation_enabled,
        )


@dataclass(frozen=True)
class LearningTestnetObservationEvaluation:
    status: str
    observed_action: str
    reason: str
    symbol: str = ""
    trade_action: str = ""
    plan_id: str | None = None
    shadow_decision_id: str | None = None
    shadow_status: str = ""
    original_quantity: Decimal | None = None
    shadow_quantity: Decimal | None = None
    activation_report_available: bool = False
    report_can_apply_reduction_in_paper: bool = False
    report_can_apply_block_in_paper: bool = False
    would_observe_reduction_signal: bool = False
    would_observe_block_signal: bool = False
    can_use_for_execution: bool = False
    observation_only: bool = True
    testnet_only: bool = True
    can_activate_testnet: bool = False
    can_activate_real: bool = False
    observation_id: str = field(default_factory=_new_testnet_observation_id)
    version: str = LEARNING_TESTNET_OBSERVATION_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningTestnetObservationPolicy:
    def __init__(self, settings: LearningTestnetObservationSettings | None = None):
        self.settings = settings or LearningTestnetObservationSettings()

    def evaluate(
        self,
        *,
        shadow_decision: LearningExecutionShadowDecision | None,
        activation_report_payload: dict[str, Any] | None,
    ) -> LearningTestnetObservationEvaluation:
        settings = self.settings

        if not settings.learning_store_enabled:
            return self._build(
                status=TESTNET_OBSERVATION_STATUS_STORE_DISABLED,
                observed_action=TESTNET_OBSERVATION_ACTION_COLLECT_MORE_DATA,
                reason="Learning store esta desligado",
                shadow_decision=shadow_decision,
                activation_report_payload=activation_report_payload,
            )

        if not settings.testnet_observation_enabled:
            return self._build(
                status=TESTNET_OBSERVATION_STATUS_DISABLED,
                observed_action=TESTNET_OBSERVATION_ACTION_KEEP_OBSERVING,
                reason="Observacao do learning em testnet esta desligada por flag",
                shadow_decision=shadow_decision,
                activation_report_payload=activation_report_payload,
            )

        if settings.execution_mode != TESTNET_EXECUTION_MODE:
            return self._build(
                status=TESTNET_OBSERVATION_STATUS_NOT_TESTNET_MODE,
                observed_action=TESTNET_OBSERVATION_ACTION_KEEP_OBSERVING,
                reason="Observacao testnet so roda em TESTNET_LIVE",
                shadow_decision=shadow_decision,
                activation_report_payload=activation_report_payload,
            )

        if shadow_decision is None:
            return self._build(
                status=TESTNET_OBSERVATION_STATUS_NO_SHADOW_DECISION,
                observed_action=TESTNET_OBSERVATION_ACTION_COLLECT_MORE_DATA,
                reason="Decisao shadow de learning nao esta disponivel",
                shadow_decision=None,
                activation_report_payload=activation_report_payload,
            )

        if activation_report_payload is None:
            return self._build(
                status=TESTNET_OBSERVATION_STATUS_NO_ACTIVATION_REPORT,
                observed_action=TESTNET_OBSERVATION_ACTION_COLLECT_MORE_DATA,
                reason="Relatorio de ativacao ainda nao esta disponivel",
                shadow_decision=shadow_decision,
                activation_report_payload=None,
            )

        if shadow_decision.action not in TESTNET_ENTRY_ACTIONS:
            return self._build(
                status=TESTNET_OBSERVATION_STATUS_OBSERVED_NO_ACTION,
                observed_action=TESTNET_OBSERVATION_ACTION_OBSERVE_NO_CHANGE,
                reason="Observacao testnet nao considera saidas como reducao/bloqueio",
                shadow_decision=shadow_decision,
                activation_report_payload=activation_report_payload,
            )

        if shadow_decision.status == LEARNING_EXECUTION_STATUS_WOULD_BLOCK:
            return self._build(
                status=TESTNET_OBSERVATION_STATUS_OBSERVED_BLOCK_SIGNAL,
                observed_action=TESTNET_OBSERVATION_ACTION_OBSERVE_BLOCK_SIGNAL,
                reason="Learning teria sinalizado bloqueio; testnet apenas observou",
                shadow_decision=shadow_decision,
                activation_report_payload=activation_report_payload,
                would_observe_block_signal=True,
            )

        if shadow_decision.status == LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE:
            return self._build(
                status=TESTNET_OBSERVATION_STATUS_OBSERVED_REDUCTION_SIGNAL,
                observed_action=TESTNET_OBSERVATION_ACTION_OBSERVE_REDUCTION_SIGNAL,
                reason="Learning teria sinalizado reducao; testnet apenas observou",
                shadow_decision=shadow_decision,
                activation_report_payload=activation_report_payload,
                would_observe_reduction_signal=True,
            )

        return self._build(
            status=TESTNET_OBSERVATION_STATUS_OBSERVED_NO_ACTION,
            observed_action=TESTNET_OBSERVATION_ACTION_OBSERVE_NO_CHANGE,
            reason="Learning nao sinalizou alteracao observavel em testnet",
            shadow_decision=shadow_decision,
            activation_report_payload=activation_report_payload,
        )

    def _build(
        self,
        *,
        status: str,
        observed_action: str,
        reason: str,
        shadow_decision: LearningExecutionShadowDecision | None,
        activation_report_payload: dict[str, Any] | None,
        would_observe_reduction_signal: bool = False,
        would_observe_block_signal: bool = False,
    ) -> LearningTestnetObservationEvaluation:
        report_available = activation_report_payload is not None
        return LearningTestnetObservationEvaluation(
            status=status,
            observed_action=observed_action,
            reason=reason,
            symbol=shadow_decision.symbol if shadow_decision is not None else "",
            trade_action=shadow_decision.action if shadow_decision is not None else "",
            plan_id=shadow_decision.plan_id if shadow_decision is not None else None,
            shadow_decision_id=(
                shadow_decision.decision_id if shadow_decision is not None else None
            ),
            shadow_status=shadow_decision.status if shadow_decision is not None else "",
            original_quantity=(
                shadow_decision.original_quantity if shadow_decision is not None else None
            ),
            shadow_quantity=(
                shadow_decision.shadow_quantity if shadow_decision is not None else None
            ),
            activation_report_available=report_available,
            report_can_apply_reduction_in_paper=_bool_payload(
                activation_report_payload,
                "can_apply_reduction_in_paper",
            ),
            report_can_apply_block_in_paper=_bool_payload(
                activation_report_payload,
                "can_apply_block_in_paper",
            ),
            would_observe_reduction_signal=would_observe_reduction_signal,
            would_observe_block_signal=would_observe_block_signal,
            can_use_for_execution=False,
            observation_only=True,
            testnet_only=True,
            can_activate_testnet=False,
            can_activate_real=False,
            diagnostics={
                "execution_mode": self.settings.execution_mode,
                "learning_store_enabled": self.settings.learning_store_enabled,
                "testnet_observation_enabled": (self.settings.testnet_observation_enabled),
                "safety_note": (
                    "Observacao testnet nao altera quantidade, nao bloqueia trade "
                    "e nao libera execucao real."
                ),
            },
        )


def _bool_payload(payload: dict[str, Any] | None, key: str) -> bool:
    if payload is None:
        return False

    return bool(payload.get(key, False))
