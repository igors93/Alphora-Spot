from dataclasses import asdict, replace

from trading.learning_advisory import LearningAdvisoryEvaluation
from trading.learning_plan_guard import LearningPlanGuardEvaluation
from trading.plan import TradePlan

LEARNING_ADVISORY_DIAGNOSTIC_KEY = "learning_advisory"
LEARNING_GUARD_DIAGNOSTIC_KEY = "learning_guard"


def attach_learning_advisory_to_trade_plan(
    *,
    plan: TradePlan,
    advisory: LearningAdvisoryEvaluation | None,
) -> TradePlan:
    if advisory is None:
        return plan

    diagnostics = dict(plan.diagnostics or {})
    diagnostics[LEARNING_ADVISORY_DIAGNOSTIC_KEY] = asdict(advisory)

    return replace(
        plan,
        diagnostics=diagnostics,
    )


def attach_learning_guard_to_trade_plan(
    *,
    plan: TradePlan,
    guard: LearningPlanGuardEvaluation | None,
) -> TradePlan:
    if guard is None:
        return plan

    diagnostics = dict(plan.diagnostics or {})
    diagnostics[LEARNING_GUARD_DIAGNOSTIC_KEY] = asdict(guard)

    return replace(
        plan,
        diagnostics=diagnostics,
    )
