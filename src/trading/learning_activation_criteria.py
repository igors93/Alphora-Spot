from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.learning_shadow_confidence import (
    CONFIDENCE_STATUS_PROMISING,
    CONFIDENCE_STATUS_STRONG,
    READINESS_PAPER_BLOCK_CANDIDATE,
    READINESS_PAPER_REDUCTION_CANDIDATE,
    LearningShadowConfidenceEvaluation,
)
from trading.learning_shadow_impact import LearningShadowImpactSummary

LEARNING_ACTIVATION_CRITERIA_VERSION = "1.0.0"

ACTIVATION_STATUS_NOT_READY = "NOT_READY"
ACTIVATION_STATUS_OBSERVE_MORE = "OBSERVE_MORE"
ACTIVATION_STATUS_PAPER_REDUCTION_ELIGIBLE = "PAPER_REDUCTION_ELIGIBLE"
ACTIVATION_STATUS_PAPER_BLOCK_ELIGIBLE = "PAPER_BLOCK_ELIGIBLE"

ACTIVATION_ACTION_COLLECT_MORE_DATA = "COLLECT_MORE_DATA"
ACTIVATION_ACTION_KEEP_SHADOW_OBSERVATION = "KEEP_SHADOW_OBSERVATION"
ACTIVATION_ACTION_REVIEW_LEARNING_RULES = "REVIEW_LEARNING_RULES"
ACTIVATION_ACTION_CONSIDER_PAPER_REDUCTION = "CONSIDER_PAPER_REDUCTION"
ACTIVATION_ACTION_CONSIDER_PAPER_BLOCK = "CONSIDER_PAPER_BLOCK"

CRITERIA_SCOPE_REDUCTION = "PAPER_REDUCTION"
CRITERIA_SCOPE_BLOCK = "PAPER_BLOCK"


def _new_activation_id() -> str:
    return f"lact_{uuid4().hex}"


@dataclass(frozen=True)
class ActivationCriterionCheck:
    name: str
    passed: bool
    value: str
    threshold: str
    scope: str
    reason: str = ""


@dataclass(frozen=True)
class LearningActivationCriteriaEvaluation:
    status: str
    recommended_action: str
    reason: str
    eligible_for_paper_reduction: bool
    eligible_for_paper_block: bool
    can_activate_testnet: bool
    can_activate_real: bool
    can_use_for_execution: bool
    shadow_only: bool
    confidence_status: str
    confidence_score: int
    activation_readiness: str
    total_shadow_decisions: int
    matched_outcomes: int
    coverage_rate: Decimal
    help_rate: Decimal
    hurt_rate: Decimal
    estimated_total_shadow_impact: Decimal
    checks: list[ActivationCriterionCheck] = field(default_factory=list)
    activation_id: str = field(default_factory=_new_activation_id)
    version: str = LEARNING_ACTIVATION_CRITERIA_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningActivationCriteriaEvaluator:
    def __init__(
        self,
        *,
        min_reduction_matched_outcomes: int = 30,
        min_block_matched_outcomes: int = 60,
        min_coverage_rate: Decimal = Decimal("0.70"),
        min_reduction_help_rate: Decimal = Decimal("0.55"),
        min_block_help_rate: Decimal = Decimal("0.70"),
        max_reduction_hurt_rate: Decimal = Decimal("0.30"),
        max_block_hurt_rate: Decimal = Decimal("0.15"),
        min_positive_impact: Decimal = Decimal("0.01"),
        min_reduction_confidence_score: int = 60,
        min_block_confidence_score: int = 80,
    ):
        self.min_reduction_matched_outcomes = min_reduction_matched_outcomes
        self.min_block_matched_outcomes = min_block_matched_outcomes
        self.min_coverage_rate = min_coverage_rate
        self.min_reduction_help_rate = min_reduction_help_rate
        self.min_block_help_rate = min_block_help_rate
        self.max_reduction_hurt_rate = max_reduction_hurt_rate
        self.max_block_hurt_rate = max_block_hurt_rate
        self.min_positive_impact = min_positive_impact
        self.min_reduction_confidence_score = min_reduction_confidence_score
        self.min_block_confidence_score = min_block_confidence_score

    def evaluate(
        self,
        *,
        summary: LearningShadowImpactSummary,
        confidence: LearningShadowConfidenceEvaluation,
    ) -> LearningActivationCriteriaEvaluation:
        if summary.total_shadow_decisions <= 0:
            return self._build(
                summary=summary,
                confidence=confidence,
                checks=[],
                status=ACTIVATION_STATUS_NOT_READY,
                recommended_action=ACTIVATION_ACTION_COLLECT_MORE_DATA,
                reason="Nao existem decisoes shadow suficientes para avaliar ativacao",
                eligible_for_paper_reduction=False,
                eligible_for_paper_block=False,
            )

        reduction_checks = self._reduction_checks(
            summary=summary,
            confidence=confidence,
        )
        block_checks = self._block_checks(
            summary=summary,
            confidence=confidence,
        )

        reduction_passed = _all_passed(reduction_checks)
        block_passed = _all_passed(block_checks)

        if block_passed:
            return self._build(
                summary=summary,
                confidence=confidence,
                checks=block_checks + reduction_checks,
                status=ACTIVATION_STATUS_PAPER_BLOCK_ELIGIBLE,
                recommended_action=ACTIVATION_ACTION_CONSIDER_PAPER_BLOCK,
                reason="Criterios minimos para teste de bloqueio em paper foram atendidos",
                eligible_for_paper_reduction=True,
                eligible_for_paper_block=True,
            )

        if reduction_passed:
            return self._build(
                summary=summary,
                confidence=confidence,
                checks=reduction_checks + block_checks,
                status=ACTIVATION_STATUS_PAPER_REDUCTION_ELIGIBLE,
                recommended_action=ACTIVATION_ACTION_CONSIDER_PAPER_REDUCTION,
                reason="Criterios minimos para teste de reducao em paper foram atendidos",
                eligible_for_paper_reduction=True,
                eligible_for_paper_block=False,
            )

        if summary.estimated_total_shadow_impact < 0:
            action = ACTIVATION_ACTION_REVIEW_LEARNING_RULES
            status = ACTIVATION_STATUS_NOT_READY
            reason = "Impacto estimado negativo impede qualquer ativacao"
        else:
            action = ACTIVATION_ACTION_KEEP_SHADOW_OBSERVATION
            status = ACTIVATION_STATUS_OBSERVE_MORE
            reason = "Criterios minimos ainda nao foram atendidos"

        return self._build(
            summary=summary,
            confidence=confidence,
            checks=reduction_checks + block_checks,
            status=status,
            recommended_action=action,
            reason=reason,
            eligible_for_paper_reduction=False,
            eligible_for_paper_block=False,
        )

    def _reduction_checks(
        self,
        *,
        summary: LearningShadowImpactSummary,
        confidence: LearningShadowConfidenceEvaluation,
    ) -> list[ActivationCriterionCheck]:
        return [
            _check(
                name="confidence_status",
                passed=confidence.status in {CONFIDENCE_STATUS_PROMISING, CONFIDENCE_STATUS_STRONG},
                value=confidence.status,
                threshold=f"{CONFIDENCE_STATUS_PROMISING} ou {CONFIDENCE_STATUS_STRONG}",
                scope=CRITERIA_SCOPE_REDUCTION,
                reason="A confianca precisa estar pelo menos promissora",
            ),
            _check(
                name="confidence_score",
                passed=confidence.confidence_score >= self.min_reduction_confidence_score,
                value=str(confidence.confidence_score),
                threshold=str(self.min_reduction_confidence_score),
                scope=CRITERIA_SCOPE_REDUCTION,
                reason="Score minimo para teste de reducao em paper",
            ),
            _check(
                name="activation_readiness",
                passed=confidence.activation_readiness
                in {
                    READINESS_PAPER_REDUCTION_CANDIDATE,
                    READINESS_PAPER_BLOCK_CANDIDATE,
                },
                value=confidence.activation_readiness,
                threshold=(
                    f"{READINESS_PAPER_REDUCTION_CANDIDATE} ou {READINESS_PAPER_BLOCK_CANDIDATE}"
                ),
                scope=CRITERIA_SCOPE_REDUCTION,
                reason="A nota de confianca precisa indicar candidato para paper",
            ),
            _check(
                name="matched_outcomes",
                passed=summary.matched_outcomes >= self.min_reduction_matched_outcomes,
                value=str(summary.matched_outcomes),
                threshold=str(self.min_reduction_matched_outcomes),
                scope=CRITERIA_SCOPE_REDUCTION,
                reason="Amostra minima para reducao em paper",
            ),
            _check(
                name="coverage_rate",
                passed=confidence.coverage_rate >= self.min_coverage_rate,
                value=str(confidence.coverage_rate),
                threshold=str(self.min_coverage_rate),
                scope=CRITERIA_SCOPE_REDUCTION,
                reason="Percentual minimo de decisoes shadow com outcome casado",
            ),
            _check(
                name="help_rate",
                passed=confidence.help_rate >= self.min_reduction_help_rate,
                value=str(confidence.help_rate),
                threshold=str(self.min_reduction_help_rate),
                scope=CRITERIA_SCOPE_REDUCTION,
                reason="Taxa minima de casos em que o learning teria ajudado",
            ),
            _check(
                name="hurt_rate",
                passed=confidence.hurt_rate <= self.max_reduction_hurt_rate,
                value=str(confidence.hurt_rate),
                threshold=str(self.max_reduction_hurt_rate),
                scope=CRITERIA_SCOPE_REDUCTION,
                reason="Taxa maxima aceitavel de casos em que o learning teria atrapalhado",
            ),
            _check(
                name="estimated_total_shadow_impact",
                passed=summary.estimated_total_shadow_impact >= self.min_positive_impact,
                value=str(summary.estimated_total_shadow_impact),
                threshold=str(self.min_positive_impact),
                scope=CRITERIA_SCOPE_REDUCTION,
                reason="Impacto liquido estimado precisa ser positivo",
            ),
        ]

    def _block_checks(
        self,
        *,
        summary: LearningShadowImpactSummary,
        confidence: LearningShadowConfidenceEvaluation,
    ) -> list[ActivationCriterionCheck]:
        return [
            _check(
                name="confidence_status",
                passed=confidence.status == CONFIDENCE_STATUS_STRONG,
                value=confidence.status,
                threshold=CONFIDENCE_STATUS_STRONG,
                scope=CRITERIA_SCOPE_BLOCK,
                reason="Bloqueio em paper exige confianca forte",
            ),
            _check(
                name="confidence_score",
                passed=confidence.confidence_score >= self.min_block_confidence_score,
                value=str(confidence.confidence_score),
                threshold=str(self.min_block_confidence_score),
                scope=CRITERIA_SCOPE_BLOCK,
                reason="Score minimo para teste de bloqueio em paper",
            ),
            _check(
                name="activation_readiness",
                passed=confidence.activation_readiness == READINESS_PAPER_BLOCK_CANDIDATE,
                value=confidence.activation_readiness,
                threshold=READINESS_PAPER_BLOCK_CANDIDATE,
                scope=CRITERIA_SCOPE_BLOCK,
                reason="A confianca precisa indicar candidato a bloqueio em paper",
            ),
            _check(
                name="matched_outcomes",
                passed=summary.matched_outcomes >= self.min_block_matched_outcomes,
                value=str(summary.matched_outcomes),
                threshold=str(self.min_block_matched_outcomes),
                scope=CRITERIA_SCOPE_BLOCK,
                reason="Amostra minima para bloqueio em paper",
            ),
            _check(
                name="coverage_rate",
                passed=confidence.coverage_rate >= self.min_coverage_rate,
                value=str(confidence.coverage_rate),
                threshold=str(self.min_coverage_rate),
                scope=CRITERIA_SCOPE_BLOCK,
                reason="Percentual minimo de decisoes shadow com outcome casado",
            ),
            _check(
                name="block_decisions",
                passed=summary.would_block_count > 0,
                value=str(summary.would_block_count),
                threshold="> 0",
                scope=CRITERIA_SCOPE_BLOCK,
                reason="Bloqueio em paper exige historico de decisoes de bloqueio",
            ),
            _check(
                name="help_rate",
                passed=confidence.help_rate >= self.min_block_help_rate,
                value=str(confidence.help_rate),
                threshold=str(self.min_block_help_rate),
                scope=CRITERIA_SCOPE_BLOCK,
                reason="Taxa minima de ajuda para bloqueio",
            ),
            _check(
                name="hurt_rate",
                passed=confidence.hurt_rate <= self.max_block_hurt_rate,
                value=str(confidence.hurt_rate),
                threshold=str(self.max_block_hurt_rate),
                scope=CRITERIA_SCOPE_BLOCK,
                reason="Taxa maxima de erro para bloqueio",
            ),
            _check(
                name="estimated_total_shadow_impact",
                passed=summary.estimated_total_shadow_impact >= self.min_positive_impact,
                value=str(summary.estimated_total_shadow_impact),
                threshold=str(self.min_positive_impact),
                scope=CRITERIA_SCOPE_BLOCK,
                reason="Impacto liquido estimado precisa ser positivo",
            ),
        ]

    def _build(
        self,
        *,
        summary: LearningShadowImpactSummary,
        confidence: LearningShadowConfidenceEvaluation,
        checks: list[ActivationCriterionCheck],
        status: str,
        recommended_action: str,
        reason: str,
        eligible_for_paper_reduction: bool,
        eligible_for_paper_block: bool,
    ) -> LearningActivationCriteriaEvaluation:
        return LearningActivationCriteriaEvaluation(
            status=status,
            recommended_action=recommended_action,
            reason=reason,
            eligible_for_paper_reduction=eligible_for_paper_reduction,
            eligible_for_paper_block=eligible_for_paper_block,
            can_activate_testnet=False,
            can_activate_real=False,
            can_use_for_execution=False,
            shadow_only=True,
            confidence_status=confidence.status,
            confidence_score=confidence.confidence_score,
            activation_readiness=confidence.activation_readiness,
            total_shadow_decisions=summary.total_shadow_decisions,
            matched_outcomes=summary.matched_outcomes,
            coverage_rate=confidence.coverage_rate,
            help_rate=confidence.help_rate,
            hurt_rate=confidence.hurt_rate,
            estimated_total_shadow_impact=summary.estimated_total_shadow_impact,
            checks=checks,
            diagnostics={
                "min_reduction_matched_outcomes": self.min_reduction_matched_outcomes,
                "min_block_matched_outcomes": self.min_block_matched_outcomes,
                "min_coverage_rate": str(self.min_coverage_rate),
                "min_reduction_help_rate": str(self.min_reduction_help_rate),
                "min_block_help_rate": str(self.min_block_help_rate),
                "max_reduction_hurt_rate": str(self.max_reduction_hurt_rate),
                "max_block_hurt_rate": str(self.max_block_hurt_rate),
                "min_positive_impact": str(self.min_positive_impact),
                "min_reduction_confidence_score": self.min_reduction_confidence_score,
                "min_block_confidence_score": self.min_block_confidence_score,
            },
        )


def format_learning_activation_criteria(
    evaluation: LearningActivationCriteriaEvaluation,
) -> str:
    lines = [
        "Learning activation criteria:",
        f"- Status: {evaluation.status}",
        f"- Recommended action: {evaluation.recommended_action}",
        f"- Reason: {evaluation.reason}",
        f"- Eligible for paper reduction: {evaluation.eligible_for_paper_reduction}",
        f"- Eligible for paper block: {evaluation.eligible_for_paper_block}",
        f"- Can activate testnet: {evaluation.can_activate_testnet}",
        f"- Can activate real: {evaluation.can_activate_real}",
        f"- Can use for execution: {evaluation.can_use_for_execution}",
        f"- Checks passed: {_passed_count(evaluation.checks)}/{len(evaluation.checks)}",
    ]

    failed = [check for check in evaluation.checks if not check.passed]
    if failed:
        lines.append("- Failed checks:")
        for check in failed:
            lines.append(
                f"  - {check.scope}.{check.name}: value={check.value} threshold={check.threshold}"
            )

    return "\n".join(lines) + "\n"


def _check(
    *,
    name: str,
    passed: bool,
    value: str,
    threshold: str,
    scope: str,
    reason: str,
) -> ActivationCriterionCheck:
    return ActivationCriterionCheck(
        name=name,
        passed=passed,
        value=value,
        threshold=threshold,
        scope=scope,
        reason=reason,
    )


def _all_passed(checks: list[ActivationCriterionCheck]) -> bool:
    return bool(checks) and all(check.passed for check in checks)


def _passed_count(checks: list[ActivationCriterionCheck]) -> int:
    return sum(1 for check in checks if check.passed)
