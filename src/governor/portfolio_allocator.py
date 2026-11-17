from dataclasses import dataclass, field
from typing import Any

from trading.enums import TradePlanStatus
from trading.plan import TradePlan


@dataclass(frozen=True)
class PortfolioAllocation:
    selected_plans: list[TradePlan]
    rejected_plans: list[TradePlan]
    diagnostics: dict[str, Any] = field(default_factory=dict)


class PortfolioAllocator:
    def allocate(
        self,
        plans: list[TradePlan],
        *,
        max_selected: int = 1,
    ) -> PortfolioAllocation:
        allowed = [
            plan
            for plan in plans
            if plan.status
            not in {
                TradePlanStatus.BLOCK_SAFETY.value,
                TradePlanStatus.BLOCK_ECONOMIC.value,
            }
        ]
        ranked = sorted(
            allowed,
            key=lambda plan: (plan.quality_score, plan.notional, plan.symbol),
            reverse=True,
        )
        selected = ranked[: max(0, max_selected)]
        selected_ids = {id(plan) for plan in selected}
        rejected = [plan for plan in plans if id(plan) not in selected_ids]
        return PortfolioAllocation(
            selected_plans=selected,
            rejected_plans=rejected,
            diagnostics={
                "candidate_count": len(plans),
                "selected_count": len(selected),
                "rejected_count": len(rejected),
                "mode": "quality_score_ranking",
            },
        )
