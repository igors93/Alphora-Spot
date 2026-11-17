from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.learning_shadow_impact import LearningShadowImpactSummary

LEARNING_SHADOW_CONFIDENCE_VERSION = "1.0.0"

CONFIDENCE_STATUS_NO_DATA = "NO_DATA"
CONFIDENCE_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
CONFIDENCE_STATUS_LOW_MATCH_RATE = "LOW_MATCH_RATE"
CONFIDENCE_STATUS_NO_ACTIONABLE_SIGNAL = "NO_ACTIONABLE_SIGNAL"
CONFIDENCE_STATUS_NEGATIVE_IMPACT = "NEGATIVE_IMPACT"
CONFIDENCE_STATUS_RISKY = "RISKY"
CONFIDENCE_STATUS_NEUTRAL = "NEUTRAL"
CONFIDENCE_STATUS_PROMISING = "PROMISING"
CONFIDENCE_STATUS_STRONG = "STRONG"

CONFIDENCE_GRADE_NONE = "NONE"
CONFIDENCE_GRADE_LOW = "LOW"
CONFIDENCE_GRADE_NEGATIVE = "NEGATIVE"
CONFIDENCE_GRADE_NEUTRAL = "NEUTRAL"
CONFIDENCE_GRADE_MEDIUM = "MEDIUM"
CONFIDENCE_GRADE_HIGH = "HIGH"

READINESS_NOT_READY = "NOT_READY"
READINESS_OBSERVE_MORE = "OBSERVE_MORE"
READINESS_PAPER_REDUCTION_CANDIDATE = "PAPER_REDUCTION_CANDIDATE"
READINESS_PAPER_BLOCK_CANDIDATE = "PAPER_BLOCK_CANDIDATE"

ACTION_COLLECT_MORE_SHADOW_DATA = "COLLECT_MORE_SHADOW_DATA"
ACTION_KEEP_SHADOW_OBSERVATION = "KEEP_SHADOW_OBSERVATION"
ACTION_REVIEW_LEARNING_RULES = "REVIEW_LEARNING_RULES"
ACTION_CONSIDER_PAPER_REDUCTION_TEST = "CONSIDER_PAPER_REDUCTION_TEST"
ACTION_CONSIDER_PAPER_BLOCK_TEST = "CONSIDER_PAPER_BLOCK_TEST"


def _new_confidence_id() -> str:
    return f"lconf_{uuid4().hex}"


@dataclass(frozen=True)
class LearningShadowConfidenceEvaluation:
    status: str
    grade: str
    confidence_score: int
    activation_readiness: str
    recommended_action: str
    reason: str
    total_shadow_decisions: int
    matched_outcomes: int
    actionable_decisions: int
    help_count: int
    hurt_count: int
    neutral_count: int
    coverage_rate: Decimal
    help_rate: Decimal
    hurt_rate: Decimal
    estimated_total_shadow_impact: Decimal
    estimated_impact_per_matched_outcome: Decimal
    can_use_for_execution: bool = False
    shadow_only: bool = True
    confidence_id: str = field(default_factory=_new_confidence_id)
    version: str = LEARNING_SHADOW_CONFIDENCE_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningShadowConfidenceEvaluator:
    def __init__(
        self,
        *,
        min_matched_outcomes: int = 30,
        strong_min_matched_outcomes: int = 50,
        min_coverage_rate: Decimal = Decimal("0.50"),
        promising_help_rate: Decimal = Decimal("0.55"),
        strong_help_rate: Decimal = Decimal("0.70"),
        max_promising_hurt_rate: Decimal = Decimal("0.30"),
        max_strong_hurt_rate: Decimal = Decimal("0.15"),
    ):
        self.min_matched_outcomes = min_matched_outcomes
        self.strong_min_matched_outcomes = strong_min_matched_outcomes
        self.min_coverage_rate = min_coverage_rate
        self.promising_help_rate = promising_help_rate
        self.strong_help_rate = strong_help_rate
        self.max_promising_hurt_rate = max_promising_hurt_rate
        self.max_strong_hurt_rate = max_strong_hurt_rate

    def evaluate(
        self,
        summary: LearningShadowImpactSummary,
    ) -> LearningShadowConfidenceEvaluation:
        metrics = _confidence_metrics(summary)

        if summary.total_shadow_decisions <= 0:
            return self._build(
                summary=summary,
                metrics=metrics,
                status=CONFIDENCE_STATUS_NO_DATA,
                grade=CONFIDENCE_GRADE_NONE,
                confidence_score=0,
                activation_readiness=READINESS_NOT_READY,
                recommended_action=ACTION_COLLECT_MORE_SHADOW_DATA,
                reason="Nenhuma decisao shadow de learning foi registrada ainda",
            )

        if summary.matched_outcomes < self.min_matched_outcomes:
            return self._build(
                summary=summary,
                metrics=metrics,
                status=CONFIDENCE_STATUS_INSUFFICIENT_DATA,
                grade=CONFIDENCE_GRADE_LOW,
                confidence_score=_sample_score(
                    summary.matched_outcomes,
                    self.min_matched_outcomes,
                ),
                activation_readiness=READINESS_OBSERVE_MORE,
                recommended_action=ACTION_COLLECT_MORE_SHADOW_DATA,
                reason="Ainda existem poucos outcomes casados com decisoes shadow",
            )

        if metrics["coverage_rate"] < self.min_coverage_rate:
            return self._build(
                summary=summary,
                metrics=metrics,
                status=CONFIDENCE_STATUS_LOW_MATCH_RATE,
                grade=CONFIDENCE_GRADE_LOW,
                confidence_score=35,
                activation_readiness=READINESS_OBSERVE_MORE,
                recommended_action=ACTION_COLLECT_MORE_SHADOW_DATA,
                reason="Muitas decisoes shadow ainda nao possuem outcome correspondente",
            )

        if metrics["actionable_decisions"] <= 0:
            return self._build(
                summary=summary,
                metrics=metrics,
                status=CONFIDENCE_STATUS_NO_ACTIONABLE_SIGNAL,
                grade=CONFIDENCE_GRADE_NEUTRAL,
                confidence_score=45,
                activation_readiness=READINESS_OBSERVE_MORE,
                recommended_action=ACTION_KEEP_SHADOW_OBSERVATION,
                reason="O learning ainda nao gerou bloqueios ou reducoes suficientes",
            )

        if summary.estimated_total_shadow_impact < 0:
            return self._build(
                summary=summary,
                metrics=metrics,
                status=CONFIDENCE_STATUS_NEGATIVE_IMPACT,
                grade=CONFIDENCE_GRADE_NEGATIVE,
                confidence_score=20,
                activation_readiness=READINESS_NOT_READY,
                recommended_action=ACTION_REVIEW_LEARNING_RULES,
                reason="O impacto estimado do learning shadow foi negativo",
            )

        if (
            metrics["hurt_rate"] > self.max_promising_hurt_rate
            and metrics["hurt_count"] >= metrics["help_count"]
        ):
            return self._build(
                summary=summary,
                metrics=metrics,
                status=CONFIDENCE_STATUS_RISKY,
                grade=CONFIDENCE_GRADE_LOW,
                confidence_score=40,
                activation_readiness=READINESS_OBSERVE_MORE,
                recommended_action=ACTION_REVIEW_LEARNING_RULES,
                reason="O learning ainda atrapalha em proporcao alta",
            )

        if summary.estimated_total_shadow_impact == 0:
            return self._build(
                summary=summary,
                metrics=metrics,
                status=CONFIDENCE_STATUS_NEUTRAL,
                grade=CONFIDENCE_GRADE_NEUTRAL,
                confidence_score=50,
                activation_readiness=READINESS_OBSERVE_MORE,
                recommended_action=ACTION_KEEP_SHADOW_OBSERVATION,
                reason="O impacto estimado ainda esta neutro",
            )

        if (
            summary.matched_outcomes >= self.strong_min_matched_outcomes
            and metrics["help_rate"] >= self.strong_help_rate
            and metrics["hurt_rate"] <= self.max_strong_hurt_rate
        ):
            return self._build(
                summary=summary,
                metrics=metrics,
                status=CONFIDENCE_STATUS_STRONG,
                grade=CONFIDENCE_GRADE_HIGH,
                confidence_score=85,
                activation_readiness=_paper_readiness(summary),
                recommended_action=_paper_action(summary),
                reason="O learning shadow tem impacto positivo com boa amostra",
            )

        if (
            metrics["help_rate"] >= self.promising_help_rate
            and metrics["hurt_rate"] <= self.max_promising_hurt_rate
        ):
            return self._build(
                summary=summary,
                metrics=metrics,
                status=CONFIDENCE_STATUS_PROMISING,
                grade=CONFIDENCE_GRADE_MEDIUM,
                confidence_score=65,
                activation_readiness=READINESS_PAPER_REDUCTION_CANDIDATE,
                recommended_action=ACTION_CONSIDER_PAPER_REDUCTION_TEST,
                reason="O learning shadow parece promissor, mas ainda requer validacao",
            )

        return self._build(
            summary=summary,
            metrics=metrics,
            status=CONFIDENCE_STATUS_NEUTRAL,
            grade=CONFIDENCE_GRADE_NEUTRAL,
            confidence_score=55,
            activation_readiness=READINESS_OBSERVE_MORE,
            recommended_action=ACTION_KEEP_SHADOW_OBSERVATION,
            reason="O learning tem impacto positivo, mas a qualidade do sinal ainda e moderada",
        )

    def _build(
        self,
        *,
        summary: LearningShadowImpactSummary,
        metrics: dict[str, Any],
        status: str,
        grade: str,
        confidence_score: int,
        activation_readiness: str,
        recommended_action: str,
        reason: str,
    ) -> LearningShadowConfidenceEvaluation:
        return LearningShadowConfidenceEvaluation(
            status=status,
            grade=grade,
            confidence_score=confidence_score,
            activation_readiness=activation_readiness,
            recommended_action=recommended_action,
            reason=reason,
            total_shadow_decisions=summary.total_shadow_decisions,
            matched_outcomes=summary.matched_outcomes,
            actionable_decisions=metrics["actionable_decisions"],
            help_count=metrics["help_count"],
            hurt_count=metrics["hurt_count"],
            neutral_count=metrics["neutral_count"],
            coverage_rate=metrics["coverage_rate"],
            help_rate=metrics["help_rate"],
            hurt_rate=metrics["hurt_rate"],
            estimated_total_shadow_impact=summary.estimated_total_shadow_impact,
            estimated_impact_per_matched_outcome=metrics["estimated_impact_per_matched_outcome"],
            can_use_for_execution=False,
            shadow_only=True,
            diagnostics={
                "min_matched_outcomes": self.min_matched_outcomes,
                "strong_min_matched_outcomes": self.strong_min_matched_outcomes,
                "min_coverage_rate": str(self.min_coverage_rate),
                "promising_help_rate": str(self.promising_help_rate),
                "strong_help_rate": str(self.strong_help_rate),
                "max_promising_hurt_rate": str(self.max_promising_hurt_rate),
                "max_strong_hurt_rate": str(self.max_strong_hurt_rate),
            },
        )


def format_learning_shadow_confidence(
    evaluation: LearningShadowConfidenceEvaluation,
) -> str:
    lines = [
        "Learning confidence:",
        f"- Status: {evaluation.status}",
        f"- Grade: {evaluation.grade}",
        f"- Score: {evaluation.confidence_score}/100",
        f"- Readiness: {evaluation.activation_readiness}",
        f"- Recommended action: {evaluation.recommended_action}",
        f"- Reason: {evaluation.reason}",
        f"- Coverage rate: {evaluation.coverage_rate}",
        f"- Help rate: {evaluation.help_rate}",
        f"- Hurt rate: {evaluation.hurt_rate}",
        f"- Estimated total impact: {evaluation.estimated_total_shadow_impact}",
        f"- Can use for execution: {evaluation.can_use_for_execution}",
    ]
    return "\n".join(lines) + "\n"


def _confidence_metrics(summary: LearningShadowImpactSummary) -> dict[str, Any]:
    help_count = summary.block_would_help_count + summary.reduce_size_would_help_count
    hurt_count = summary.block_would_hurt_count + summary.reduce_size_would_hurt_count
    neutral_count = summary.block_neutral_count + summary.reduce_size_neutral_count
    actionable_decisions = summary.would_block_count + summary.would_reduce_size_count

    return {
        "help_count": help_count,
        "hurt_count": hurt_count,
        "neutral_count": neutral_count,
        "actionable_decisions": actionable_decisions,
        "coverage_rate": _ratio(
            summary.matched_outcomes,
            summary.total_shadow_decisions,
        ),
        "help_rate": _ratio(help_count, actionable_decisions),
        "hurt_rate": _ratio(hurt_count, actionable_decisions),
        "estimated_impact_per_matched_outcome": _decimal_ratio(
            summary.estimated_total_shadow_impact,
            summary.matched_outcomes,
        ),
    }


def _paper_readiness(summary: LearningShadowImpactSummary) -> str:
    if summary.would_block_count > 0:
        return READINESS_PAPER_BLOCK_CANDIDATE

    return READINESS_PAPER_REDUCTION_CANDIDATE


def _paper_action(summary: LearningShadowImpactSummary) -> str:
    if summary.would_block_count > 0:
        return ACTION_CONSIDER_PAPER_BLOCK_TEST

    return ACTION_CONSIDER_PAPER_REDUCTION_TEST


def _sample_score(current: int, required: int) -> int:
    if required <= 0:
        return 0

    score = Decimal(str(current)) / Decimal(str(required)) * Decimal("35")
    return min(35, max(0, int(score)))


def _ratio(part: int, total: int) -> Decimal:
    if total <= 0:
        return Decimal("0")

    return Decimal(str(part)) / Decimal(str(total))


def _decimal_ratio(value: Decimal, total: int) -> Decimal:
    if total <= 0:
        return Decimal("0")

    return value / Decimal(str(total))
