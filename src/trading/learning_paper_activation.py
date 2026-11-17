from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from trading.learning_activation_criteria import (
    ACTIVATION_STATUS_PAPER_BLOCK_ELIGIBLE,
    ACTIVATION_STATUS_PAPER_REDUCTION_ELIGIBLE,
    LearningActivationCriteriaEvaluation,
)

LEARNING_PAPER_ACTIVATION_VERSION = "1.0.0"

PAPER_ACTIVATION_STATUS_DISABLED = "DISABLED"
PAPER_ACTIVATION_STATUS_STORE_DISABLED = "STORE_DISABLED"
PAPER_ACTIVATION_STATUS_NOT_PAPER_MODE = "NOT_PAPER_MODE"
PAPER_ACTIVATION_STATUS_OBSERVE_ONLY = "OBSERVE_ONLY"
PAPER_ACTIVATION_STATUS_CRITERIA_NOT_MET = "CRITERIA_NOT_MET"
PAPER_ACTIVATION_STATUS_REDUCTION_READY = "PAPER_REDUCTION_READY"
PAPER_ACTIVATION_STATUS_BLOCK_READY = "PAPER_BLOCK_READY"

PAPER_ACTIVATION_ACTION_KEEP_DISABLED = "KEEP_DISABLED"
PAPER_ACTIVATION_ACTION_ENABLE_LEARNING_STORE = "ENABLE_LEARNING_STORE"
PAPER_ACTIVATION_ACTION_SWITCH_TO_PAPER = "SWITCH_TO_PAPER"
PAPER_ACTIVATION_ACTION_KEEP_OBSERVING = "KEEP_OBSERVING"
PAPER_ACTIVATION_ACTION_WAIT_FOR_CRITERIA = "WAIT_FOR_CRITERIA"
PAPER_ACTIVATION_ACTION_ALLOW_PAPER_REDUCTION = "ALLOW_PAPER_REDUCTION"
PAPER_ACTIVATION_ACTION_ALLOW_PAPER_BLOCK = "ALLOW_PAPER_BLOCK"

PAPER_EXECUTION_MODE = "PAPER_LIVE"


def _new_paper_activation_id() -> str:
    return f"lpact_{uuid4().hex}"


@dataclass(frozen=True)
class LearningPaperActivationSettings:
    execution_mode: str = ""
    learning_store_enabled: bool = False
    paper_activation_enabled: bool = False
    paper_reduction_enabled: bool = False
    paper_block_enabled: bool = False

    @classmethod
    def from_config(cls, config) -> "LearningPaperActivationSettings":
        return cls(
            execution_mode=config.execution_mode.value,
            learning_store_enabled=config.learning_store_enabled,
            paper_activation_enabled=config.learning_paper_activation_enabled,
            paper_reduction_enabled=config.learning_paper_reduction_enabled,
            paper_block_enabled=config.learning_paper_block_enabled,
        )


@dataclass(frozen=True)
class LearningPaperActivationEvaluation:
    status: str
    recommended_action: str
    reason: str
    execution_mode: str
    learning_store_enabled: bool
    paper_activation_enabled: bool
    paper_reduction_enabled: bool
    paper_block_enabled: bool
    activation_status: str
    eligible_for_paper_reduction: bool
    eligible_for_paper_block: bool
    can_apply_reduction_in_paper: bool
    can_apply_block_in_paper: bool
    can_activate_testnet: bool = False
    can_activate_real: bool = False
    can_use_for_execution: bool = False
    paper_only: bool = True
    activation_id: str = field(default_factory=_new_paper_activation_id)
    version: str = LEARNING_PAPER_ACTIVATION_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningPaperActivationGate:
    def __init__(self, settings: LearningPaperActivationSettings | None = None):
        self.settings = settings or LearningPaperActivationSettings()

    def evaluate(
        self,
        activation: LearningActivationCriteriaEvaluation,
    ) -> LearningPaperActivationEvaluation:
        settings = self.settings

        if not settings.learning_store_enabled:
            return self._build(
                activation=activation,
                status=PAPER_ACTIVATION_STATUS_STORE_DISABLED,
                recommended_action=PAPER_ACTIVATION_ACTION_ENABLE_LEARNING_STORE,
                reason="Learning store esta desligado",
                can_apply_reduction_in_paper=False,
                can_apply_block_in_paper=False,
            )

        if not settings.paper_activation_enabled:
            return self._build(
                activation=activation,
                status=PAPER_ACTIVATION_STATUS_DISABLED,
                recommended_action=PAPER_ACTIVATION_ACTION_KEEP_DISABLED,
                reason="Ativacao do learning em paper esta desligada por flag",
                can_apply_reduction_in_paper=False,
                can_apply_block_in_paper=False,
            )

        if settings.execution_mode != PAPER_EXECUTION_MODE:
            return self._build(
                activation=activation,
                status=PAPER_ACTIVATION_STATUS_NOT_PAPER_MODE,
                recommended_action=PAPER_ACTIVATION_ACTION_SWITCH_TO_PAPER,
                reason="Flags de ativacao do learning so podem agir em PAPER_LIVE",
                can_apply_reduction_in_paper=False,
                can_apply_block_in_paper=False,
            )

        if not settings.paper_reduction_enabled and not settings.paper_block_enabled:
            return self._build(
                activation=activation,
                status=PAPER_ACTIVATION_STATUS_OBSERVE_ONLY,
                recommended_action=PAPER_ACTIVATION_ACTION_KEEP_OBSERVING,
                reason="Ativacao geral esta ligada, mas reducao/bloqueio seguem desligados",
                can_apply_reduction_in_paper=False,
                can_apply_block_in_paper=False,
            )

        if (
            settings.paper_block_enabled
            and activation.eligible_for_paper_block
            and activation.status == ACTIVATION_STATUS_PAPER_BLOCK_ELIGIBLE
        ):
            return self._build(
                activation=activation,
                status=PAPER_ACTIVATION_STATUS_BLOCK_READY,
                recommended_action=PAPER_ACTIVATION_ACTION_ALLOW_PAPER_BLOCK,
                reason="Flags e criterios permitem teste de bloqueio em paper",
                can_apply_reduction_in_paper=True,
                can_apply_block_in_paper=True,
            )

        if (
            settings.paper_reduction_enabled
            and activation.eligible_for_paper_reduction
            and activation.status
            in {
                ACTIVATION_STATUS_PAPER_REDUCTION_ELIGIBLE,
                ACTIVATION_STATUS_PAPER_BLOCK_ELIGIBLE,
            }
        ):
            return self._build(
                activation=activation,
                status=PAPER_ACTIVATION_STATUS_REDUCTION_READY,
                recommended_action=PAPER_ACTIVATION_ACTION_ALLOW_PAPER_REDUCTION,
                reason="Flags e criterios permitem teste de reducao em paper",
                can_apply_reduction_in_paper=True,
                can_apply_block_in_paper=False,
            )

        return self._build(
            activation=activation,
            status=PAPER_ACTIVATION_STATUS_CRITERIA_NOT_MET,
            recommended_action=PAPER_ACTIVATION_ACTION_WAIT_FOR_CRITERIA,
            reason="Flags foram ligadas, mas criterios minimos ainda nao foram atendidos",
            can_apply_reduction_in_paper=False,
            can_apply_block_in_paper=False,
        )

    def _build(
        self,
        *,
        activation: LearningActivationCriteriaEvaluation,
        status: str,
        recommended_action: str,
        reason: str,
        can_apply_reduction_in_paper: bool,
        can_apply_block_in_paper: bool,
    ) -> LearningPaperActivationEvaluation:
        settings = self.settings
        return LearningPaperActivationEvaluation(
            status=status,
            recommended_action=recommended_action,
            reason=reason,
            execution_mode=settings.execution_mode,
            learning_store_enabled=settings.learning_store_enabled,
            paper_activation_enabled=settings.paper_activation_enabled,
            paper_reduction_enabled=settings.paper_reduction_enabled,
            paper_block_enabled=settings.paper_block_enabled,
            activation_status=activation.status,
            eligible_for_paper_reduction=activation.eligible_for_paper_reduction,
            eligible_for_paper_block=activation.eligible_for_paper_block,
            can_apply_reduction_in_paper=can_apply_reduction_in_paper,
            can_apply_block_in_paper=can_apply_block_in_paper,
            can_activate_testnet=False,
            can_activate_real=False,
            can_use_for_execution=False,
            paper_only=True,
            diagnostics={
                "activation_id": activation.activation_id,
                "activation_version": activation.version,
                "safety_note": "Este gate apenas libera elegibilidade para paper; nao executa ordens.",
            },
        )


def format_learning_paper_activation(
    evaluation: LearningPaperActivationEvaluation,
) -> str:
    lines = [
        "Learning paper activation flags:",
        f"- Status: {evaluation.status}",
        f"- Recommended action: {evaluation.recommended_action}",
        f"- Reason: {evaluation.reason}",
        f"- Execution mode: {evaluation.execution_mode}",
        f"- Learning store enabled: {evaluation.learning_store_enabled}",
        f"- Paper activation enabled: {evaluation.paper_activation_enabled}",
        f"- Paper reduction enabled: {evaluation.paper_reduction_enabled}",
        f"- Paper block enabled: {evaluation.paper_block_enabled}",
        f"- Can apply reduction in paper: {evaluation.can_apply_reduction_in_paper}",
        f"- Can apply block in paper: {evaluation.can_apply_block_in_paper}",
        f"- Can activate testnet: {evaluation.can_activate_testnet}",
        f"- Can activate real: {evaluation.can_activate_real}",
        f"- Can use for execution: {evaluation.can_use_for_execution}",
    ]
    return "\n".join(lines) + "\n"
