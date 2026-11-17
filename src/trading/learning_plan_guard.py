from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.learning_advisory import LearningAdvisoryEvaluation
from trading.learning_recommendations import SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_WATCH
from trading.plan import TradePlan

LEARNING_PLAN_GUARD_VERSION = "1.0.0"

LEARNING_GUARD_STATUS_NO_ADVISORY = "NO_ADVISORY"
LEARNING_GUARD_STATUS_ALLOW = "ALLOW"
LEARNING_GUARD_STATUS_REVIEW = "REVIEW"
LEARNING_GUARD_STATUS_WOULD_REDUCE_SIZE = "WOULD_REDUCE_SIZE"
LEARNING_GUARD_STATUS_WOULD_BLOCK = "WOULD_BLOCK"

LEARNING_GUARD_ACTION_ALLOW = "ALLOW_TRADE_SHADOW"
LEARNING_GUARD_ACTION_REVIEW = "REVIEW_TRADE_SHADOW"
LEARNING_GUARD_ACTION_REDUCE_SIZE = "REDUCE_SIZE_SHADOW"
LEARNING_GUARD_ACTION_BLOCK = "BLOCK_TRADE_SHADOW"


def _new_guard_id() -> str:
    return f"lg_{uuid4().hex}"


@dataclass(frozen=True)
class LearningPlanGuardEvaluation:
    symbol: str
    plan_id: str
    plan_action: str
    status: str
    suggested_action: str
    reason: str
    severity: str = ""
    advisory_status: str = ""
    advisory_id: str | None = None
    would_block_trade: bool = False
    would_reduce_size: bool = False
    suggested_size_multiplier: Decimal = Decimal("1")
    can_use_for_execution: bool = False
    shadow_only: bool = True
    guard_id: str = field(default_factory=_new_guard_id)
    version: str = LEARNING_PLAN_GUARD_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningPlanGuard:
    def evaluate(
        self,
        *,
        plan: TradePlan,
        advisory: LearningAdvisoryEvaluation | None,
    ) -> LearningPlanGuardEvaluation:
        if advisory is None:
            return LearningPlanGuardEvaluation(
                symbol=plan.symbol,
                plan_id=plan.plan_id,
                plan_action=plan.action,
                status=LEARNING_GUARD_STATUS_NO_ADVISORY,
                suggested_action=LEARNING_GUARD_ACTION_ALLOW,
                reason="LEARNING_ADVISORY_NOT_AVAILABLE",
                severity=SEVERITY_INFO,
                diagnostics={
                    "shadow_only": True,
                    "can_use_for_execution": False,
                },
            )

        if advisory.would_block_trade and advisory.severity == SEVERITY_WARNING:
            return LearningPlanGuardEvaluation(
                symbol=plan.symbol,
                plan_id=plan.plan_id,
                plan_action=plan.action,
                status=LEARNING_GUARD_STATUS_WOULD_BLOCK,
                suggested_action=LEARNING_GUARD_ACTION_BLOCK,
                reason="LEARNING_ADVISORY_WOULD_BLOCK_TRADE",
                severity=advisory.severity,
                advisory_status=advisory.status,
                advisory_id=advisory.advisory_id,
                would_block_trade=True,
                suggested_size_multiplier=Decimal("0"),
                diagnostics=_diagnostics_from_advisory(advisory),
            )

        if advisory.would_reduce_priority:
            return LearningPlanGuardEvaluation(
                symbol=plan.symbol,
                plan_id=plan.plan_id,
                plan_action=plan.action,
                status=LEARNING_GUARD_STATUS_WOULD_REDUCE_SIZE,
                suggested_action=LEARNING_GUARD_ACTION_REDUCE_SIZE,
                reason="LEARNING_ADVISORY_WOULD_REDUCE_SIZE",
                severity=advisory.severity,
                advisory_status=advisory.status,
                advisory_id=advisory.advisory_id,
                would_reduce_size=True,
                suggested_size_multiplier=Decimal("0.5"),
                diagnostics=_diagnostics_from_advisory(advisory),
            )

        if advisory.severity in {SEVERITY_WARNING, SEVERITY_WATCH}:
            return LearningPlanGuardEvaluation(
                symbol=plan.symbol,
                plan_id=plan.plan_id,
                plan_action=plan.action,
                status=LEARNING_GUARD_STATUS_REVIEW,
                suggested_action=LEARNING_GUARD_ACTION_REVIEW,
                reason="LEARNING_ADVISORY_REQUIRES_REVIEW",
                severity=advisory.severity,
                advisory_status=advisory.status,
                advisory_id=advisory.advisory_id,
                diagnostics=_diagnostics_from_advisory(advisory),
            )

        return LearningPlanGuardEvaluation(
            symbol=plan.symbol,
            plan_id=plan.plan_id,
            plan_action=plan.action,
            status=LEARNING_GUARD_STATUS_ALLOW,
            suggested_action=LEARNING_GUARD_ACTION_ALLOW,
            reason="LEARNING_ADVISORY_DOES_NOT_RESTRICT_PLAN",
            severity=advisory.severity,
            advisory_status=advisory.status,
            advisory_id=advisory.advisory_id,
            diagnostics=_diagnostics_from_advisory(advisory),
        )


def _diagnostics_from_advisory(advisory: LearningAdvisoryEvaluation) -> dict[str, Any]:
    return {
        "shadow_only": True,
        "can_use_for_execution": False,
        "advisory_status": advisory.status,
        "advisory_severity": advisory.severity,
        "advisory_reason": advisory.reason,
        "advisory_would_block_trade": advisory.would_block_trade,
        "advisory_would_reduce_priority": advisory.would_reduce_priority,
        "relevant_recommendations_count": advisory.relevant_recommendations_count,
        "total_outcomes": advisory.total_outcomes,
    }
