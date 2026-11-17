from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from trading.learning_recommendations import (
    ACTION_REDUCE_SYMBOL_PRIORITY_REVIEW,
    ACTION_REVIEW_ADAPTIVE_MANAGEMENT,
    ACTION_REVIEW_GLOBAL_RISK,
    ACTION_REVIEW_TRADE_FILTERS,
    SCOPE_ADAPTIVE,
    SCOPE_GLOBAL,
    SCOPE_SYMBOL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    SEVERITY_WATCH,
)

LEARNING_ADVISORY_VERSION = "1.0.0"

ADVISORY_STATUS_NO_REPORT = "NO_REPORT"
ADVISORY_STATUS_NO_RELEVANT_RECOMMENDATION = "NO_RELEVANT_RECOMMENDATION"
ADVISORY_STATUS_INFO = "INFO"
ADVISORY_STATUS_WATCH = "WATCH"
ADVISORY_STATUS_WARNING = "WARNING"

SHADOW_BLOCK_ACTIONS = {
    ACTION_REDUCE_SYMBOL_PRIORITY_REVIEW,
    ACTION_REVIEW_ADAPTIVE_MANAGEMENT,
    ACTION_REVIEW_GLOBAL_RISK,
    ACTION_REVIEW_TRADE_FILTERS,
}

_SEVERITY_RANK = {
    SEVERITY_INFO: 1,
    SEVERITY_WATCH: 2,
    SEVERITY_WARNING: 3,
}


def _new_advisory_id() -> str:
    return f"adv_{uuid4().hex}"


@dataclass(frozen=True)
class LearningAdvisoryEvaluation:
    symbol: str
    action: str
    status: str
    severity: str
    reason: str
    recommendations_count: int
    relevant_recommendations_count: int
    report_status: str = ""
    total_outcomes: int = 0
    would_block_trade: bool = False
    would_reduce_priority: bool = False
    can_use_for_execution: bool = False
    shadow_only: bool = True
    relevant_recommendations: list[dict[str, Any]] = field(default_factory=list)
    advisory_id: str = field(default_factory=_new_advisory_id)
    version: str = LEARNING_ADVISORY_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningAdvisoryEngine:
    def evaluate(
        self,
        *,
        symbol: str,
        action: str,
        report_payload: dict[str, Any] | None,
    ) -> LearningAdvisoryEvaluation:
        if report_payload is None:
            return LearningAdvisoryEvaluation(
                symbol=symbol,
                action=action,
                status=ADVISORY_STATUS_NO_REPORT,
                severity=SEVERITY_INFO,
                reason="LEARNING_REPORT_NOT_AVAILABLE",
                recommendations_count=0,
                relevant_recommendations_count=0,
                diagnostics={
                    "shadow_only": True,
                    "can_use_for_execution": False,
                },
            )

        recommendations = _extract_recommendations(report_payload)
        relevant = [
            recommendation
            for recommendation in recommendations
            if _recommendation_applies_to_symbol(recommendation, symbol)
        ]

        if not relevant:
            return LearningAdvisoryEvaluation(
                symbol=symbol,
                action=action,
                status=ADVISORY_STATUS_NO_RELEVANT_RECOMMENDATION,
                severity=SEVERITY_INFO,
                reason="NO_RELEVANT_LEARNING_RECOMMENDATION",
                recommendations_count=len(recommendations),
                relevant_recommendations_count=0,
                report_status=str(report_payload.get("status", "")),
                total_outcomes=_safe_int(report_payload.get("total_outcomes")),
                diagnostics={
                    "shadow_only": True,
                    "can_use_for_execution": False,
                    "report_status": report_payload.get("status", ""),
                },
            )

        strongest = _strongest_recommendation(relevant)
        severity = str(strongest.get("severity") or SEVERITY_INFO)
        status = _status_from_severity(severity)
        would_block = any(
            recommendation.get("action") in SHADOW_BLOCK_ACTIONS for recommendation in relevant
        )
        would_reduce_priority = any(
            recommendation.get("action") == ACTION_REDUCE_SYMBOL_PRIORITY_REVIEW
            for recommendation in relevant
        )

        return LearningAdvisoryEvaluation(
            symbol=symbol,
            action=action,
            status=status,
            severity=severity,
            reason=_reason_from_status(status),
            recommendations_count=len(recommendations),
            relevant_recommendations_count=len(relevant),
            report_status=str(report_payload.get("status", "")),
            total_outcomes=_safe_int(report_payload.get("total_outcomes")),
            would_block_trade=would_block,
            would_reduce_priority=would_reduce_priority,
            can_use_for_execution=False,
            shadow_only=True,
            relevant_recommendations=relevant,
            diagnostics={
                "shadow_only": True,
                "can_use_for_execution": False,
                "report_status": report_payload.get("status", ""),
                "strongest_recommendation": strongest,
            },
        )


def _extract_recommendations(report_payload: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations = report_payload.get("recommendations")
    if isinstance(recommendations, list):
        return [item for item in recommendations if isinstance(item, dict)]

    report = report_payload.get("report")
    if isinstance(report, dict):
        nested = report.get("recommendations")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]

    return []


def _recommendation_applies_to_symbol(
    recommendation: dict[str, Any],
    symbol: str,
) -> bool:
    scope = recommendation.get("scope")

    if scope == SCOPE_GLOBAL:
        return True

    if scope == SCOPE_ADAPTIVE:
        return True

    if scope == SCOPE_SYMBOL:
        return str(recommendation.get("symbol") or "").upper() == symbol.upper()

    return False


def _strongest_recommendation(
    recommendations: list[dict[str, Any]],
) -> dict[str, Any]:
    return max(
        recommendations,
        key=lambda item: _SEVERITY_RANK.get(str(item.get("severity")), 0),
    )


def _status_from_severity(severity: str) -> str:
    if severity == SEVERITY_WARNING:
        return ADVISORY_STATUS_WARNING

    if severity == SEVERITY_WATCH:
        return ADVISORY_STATUS_WATCH

    return ADVISORY_STATUS_INFO


def _reason_from_status(status: str) -> str:
    if status == ADVISORY_STATUS_WARNING:
        return "LEARNING_RECOMMENDATIONS_REQUIRE_ATTENTION"

    if status == ADVISORY_STATUS_WATCH:
        return "LEARNING_RECOMMENDATIONS_SHOULD_BE_MONITORED"

    return "LEARNING_RECOMMENDATIONS_INFORMATIONAL"


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
