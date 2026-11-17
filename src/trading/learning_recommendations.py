from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.learning import OutcomeBucketStats, TradePlanLearningSummary

LEARNING_RECOMMENDATION_VERSION = "1.0.0"

REPORT_STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
REPORT_STATUS_READY = "READY"

SEVERITY_INFO = "INFO"
SEVERITY_WATCH = "WATCH"
SEVERITY_WARNING = "WARNING"

ACTION_COLLECT_MORE_DATA = "COLLECT_MORE_DATA"
ACTION_MAINTAIN_CURRENT_SETTINGS = "MAINTAIN_CURRENT_SETTINGS"
ACTION_REVIEW_GLOBAL_RISK = "REVIEW_GLOBAL_RISK"
ACTION_REVIEW_TRADE_FILTERS = "REVIEW_TRADE_FILTERS"
ACTION_REVIEW_ADAPTIVE_MANAGEMENT = "REVIEW_ADAPTIVE_MANAGEMENT"
ACTION_KEEP_ADAPTIVE_OBSERVATION = "KEEP_ADAPTIVE_OBSERVATION"
ACTION_REDUCE_SYMBOL_PRIORITY_REVIEW = "REDUCE_SYMBOL_PRIORITY_REVIEW"
ACTION_KEEP_SYMBOL_ACTIVE_REVIEW = "KEEP_SYMBOL_ACTIVE_REVIEW"

SCOPE_GLOBAL = "GLOBAL"
SCOPE_ADAPTIVE = "ADAPTIVE"
SCOPE_SYMBOL = "SYMBOL"


def _new_recommendation_id() -> str:
    return f"rec_{uuid4().hex}"


@dataclass(frozen=True)
class LearningRecommendation:
    action: str
    severity: str
    reason: str
    scope: str
    confidence: int
    symbol: str | None = None
    metric_name: str = ""
    metric_value: Decimal | None = None
    threshold: Decimal | None = None
    recommendation_id: str = field(default_factory=_new_recommendation_id)
    version: str = LEARNING_RECOMMENDATION_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LearningRecommendationReport:
    status: str
    recommendations: list[LearningRecommendation]
    total_outcomes: int
    can_use_for_execution: bool = False
    version: str = LEARNING_RECOMMENDATION_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class TradePlanLearningAdvisor:
    def __init__(
        self,
        *,
        min_total_outcomes: int = 20,
        min_bucket_outcomes: int = 5,
        min_symbol_outcomes: int = 5,
        low_win_rate: Decimal = Decimal("0.40"),
        healthy_win_rate: Decimal = Decimal("0.55"),
        meaningful_pnl_delta: Decimal = Decimal("0.01"),
    ):
        self.min_total_outcomes = min_total_outcomes
        self.min_bucket_outcomes = min_bucket_outcomes
        self.min_symbol_outcomes = min_symbol_outcomes
        self.low_win_rate = low_win_rate
        self.healthy_win_rate = healthy_win_rate
        self.meaningful_pnl_delta = meaningful_pnl_delta

    def build_report(
        self,
        summary: TradePlanLearningSummary,
    ) -> LearningRecommendationReport:
        recommendations: list[LearningRecommendation] = []

        if summary.total_outcomes < self.min_total_outcomes:
            recommendations.append(
                LearningRecommendation(
                    action=ACTION_COLLECT_MORE_DATA,
                    severity=SEVERITY_INFO,
                    reason="Ainda existem poucos outcomes para recomendacao confiavel",
                    scope=SCOPE_GLOBAL,
                    confidence=30,
                    metric_name="total_outcomes",
                    metric_value=Decimal(str(summary.total_outcomes)),
                    threshold=Decimal(str(self.min_total_outcomes)),
                    diagnostics={
                        "current_total_outcomes": summary.total_outcomes,
                        "required_total_outcomes": self.min_total_outcomes,
                    },
                )
            )
            return LearningRecommendationReport(
                status=REPORT_STATUS_INSUFFICIENT_DATA,
                recommendations=recommendations,
                total_outcomes=summary.total_outcomes,
                can_use_for_execution=False,
                diagnostics={
                    "reason": "INSUFFICIENT_SAMPLE_SIZE",
                    "advisor_version": LEARNING_RECOMMENDATION_VERSION,
                },
            )

        if summary.total_net_pnl < 0:
            recommendations.append(
                LearningRecommendation(
                    action=ACTION_REVIEW_GLOBAL_RISK,
                    severity=SEVERITY_WARNING,
                    reason="O PnL liquido total esta negativo",
                    scope=SCOPE_GLOBAL,
                    confidence=75,
                    metric_name="total_net_pnl",
                    metric_value=summary.total_net_pnl,
                    threshold=Decimal("0"),
                )
            )

        if summary.win_rate < self.low_win_rate:
            recommendations.append(
                LearningRecommendation(
                    action=ACTION_REVIEW_TRADE_FILTERS,
                    severity=SEVERITY_WARNING,
                    reason="A taxa de acerto esta abaixo do minimo esperado",
                    scope=SCOPE_GLOBAL,
                    confidence=70,
                    metric_name="win_rate",
                    metric_value=summary.win_rate,
                    threshold=self.low_win_rate,
                )
            )

        self._append_adaptive_recommendations(summary, recommendations)
        self._append_symbol_recommendations(summary, recommendations)

        if not recommendations:
            recommendations.append(
                LearningRecommendation(
                    action=ACTION_MAINTAIN_CURRENT_SETTINGS,
                    severity=SEVERITY_INFO,
                    reason="Nao foram encontrados sinais negativos relevantes",
                    scope=SCOPE_GLOBAL,
                    confidence=60,
                    metric_name="win_rate",
                    metric_value=summary.win_rate,
                    threshold=self.healthy_win_rate,
                )
            )

        return LearningRecommendationReport(
            status=REPORT_STATUS_READY,
            recommendations=recommendations,
            total_outcomes=summary.total_outcomes,
            can_use_for_execution=False,
            diagnostics={
                "advisor_version": LEARNING_RECOMMENDATION_VERSION,
                "can_use_for_execution": False,
                "note": "Recomendacoes ainda sao informativas; nao alteram execucao",
            },
        )

    def _append_adaptive_recommendations(
        self,
        summary: TradePlanLearningSummary,
        recommendations: list[LearningRecommendation],
    ) -> None:
        adjusted = summary.adjusted
        not_adjusted = summary.not_adjusted

        if adjusted.count < self.min_bucket_outcomes:
            recommendations.append(
                LearningRecommendation(
                    action=ACTION_KEEP_ADAPTIVE_OBSERVATION,
                    severity=SEVERITY_INFO,
                    reason="Ainda existem poucos trades com ajuste adaptativo",
                    scope=SCOPE_ADAPTIVE,
                    confidence=35,
                    metric_name="adjusted_count",
                    metric_value=Decimal(str(adjusted.count)),
                    threshold=Decimal(str(self.min_bucket_outcomes)),
                )
            )
            return

        if not_adjusted.count < self.min_bucket_outcomes:
            return

        pnl_delta = adjusted.average_net_pnl - not_adjusted.average_net_pnl

        if pnl_delta < -self.meaningful_pnl_delta:
            recommendations.append(
                LearningRecommendation(
                    action=ACTION_REVIEW_ADAPTIVE_MANAGEMENT,
                    severity=SEVERITY_WARNING,
                    reason="Trades com ajuste adaptativo estao performando pior",
                    scope=SCOPE_ADAPTIVE,
                    confidence=75,
                    metric_name="adaptive_average_pnl_delta",
                    metric_value=pnl_delta,
                    threshold=-self.meaningful_pnl_delta,
                    diagnostics={
                        "adjusted_average_net_pnl": str(adjusted.average_net_pnl),
                        "not_adjusted_average_net_pnl": str(not_adjusted.average_net_pnl),
                    },
                )
            )
            return

        if pnl_delta > self.meaningful_pnl_delta:
            recommendations.append(
                LearningRecommendation(
                    action=ACTION_KEEP_ADAPTIVE_OBSERVATION,
                    severity=SEVERITY_WATCH,
                    reason="Trades com ajuste adaptativo estao performando melhor",
                    scope=SCOPE_ADAPTIVE,
                    confidence=65,
                    metric_name="adaptive_average_pnl_delta",
                    metric_value=pnl_delta,
                    threshold=self.meaningful_pnl_delta,
                    diagnostics={
                        "adjusted_average_net_pnl": str(adjusted.average_net_pnl),
                        "not_adjusted_average_net_pnl": str(not_adjusted.average_net_pnl),
                    },
                )
            )

    def _append_symbol_recommendations(
        self,
        summary: TradePlanLearningSummary,
        recommendations: list[LearningRecommendation],
    ) -> None:
        for symbol, stats in summary.by_symbol.items():
            if stats.count < self.min_symbol_outcomes:
                continue

            if stats.total_net_pnl < 0 and stats.win_rate < self.low_win_rate:
                recommendations.append(
                    LearningRecommendation(
                        action=ACTION_REDUCE_SYMBOL_PRIORITY_REVIEW,
                        severity=SEVERITY_WARNING,
                        reason="Simbolo com PnL negativo e baixa taxa de acerto",
                        scope=SCOPE_SYMBOL,
                        symbol=symbol,
                        confidence=70,
                        metric_name="symbol_win_rate",
                        metric_value=stats.win_rate,
                        threshold=self.low_win_rate,
                        diagnostics=_bucket_diagnostics(stats),
                    )
                )
                continue

            if stats.total_net_pnl > 0 and stats.win_rate >= self.healthy_win_rate:
                recommendations.append(
                    LearningRecommendation(
                        action=ACTION_KEEP_SYMBOL_ACTIVE_REVIEW,
                        severity=SEVERITY_WATCH,
                        reason="Simbolo com desempenho positivo no historico recente",
                        scope=SCOPE_SYMBOL,
                        symbol=symbol,
                        confidence=55,
                        metric_name="symbol_win_rate",
                        metric_value=stats.win_rate,
                        threshold=self.healthy_win_rate,
                        diagnostics=_bucket_diagnostics(stats),
                    )
                )


def _bucket_diagnostics(stats: OutcomeBucketStats) -> dict[str, Any]:
    return {
        "count": stats.count,
        "wins": stats.wins,
        "losses": stats.losses,
        "breakeven": stats.breakeven,
        "unknown": stats.unknown,
        "linked": stats.linked,
        "total_net_pnl": str(stats.total_net_pnl),
        "average_net_pnl": str(stats.average_net_pnl),
        "win_rate": str(stats.win_rate),
    }
