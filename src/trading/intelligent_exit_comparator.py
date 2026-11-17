from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from trading.intelligent_exit_builder import IntelligentExitPlan
from trading.plan import TradePlan

INTELLIGENT_EXIT_COMPARISON_VERSION = "0.1.0"

INTELLIGENT_EXIT_COMPARISON_STATUS_COMPARED = "COMPARED"
INTELLIGENT_EXIT_COMPARISON_STATUS_NO_EXIT_PLAN = "NO_INTELLIGENT_EXIT_PLAN"
INTELLIGENT_EXIT_COMPARISON_STATUS_NO_TRADE_PLAN = "NO_TRADE_PLAN"


def _new_comparison_id() -> str:
    return f"iexitcmp_{uuid4().hex}"


@dataclass(frozen=True)
class IntelligentExitComparison:
    symbol: str
    action: str
    status: str
    reason: str
    trade_plan_id: str | None = None
    intelligent_exit_plan_id: str | None = None
    stop_changed: bool = False
    take_profit_changed: bool = False
    risk_reward_changed: bool = False
    protection_complete: bool = False
    execution_unchanged: bool = True
    can_use_for_execution: bool = False
    shadow_only: bool = True
    differences: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    comparison_id: str = field(default_factory=_new_comparison_id)
    version: str = INTELLIGENT_EXIT_COMPARISON_VERSION


class IntelligentExitComparator:
    """Compara a protecao inteligente com a protecao do TradePlan.

    Fase 5B:
    - apenas observa diferenças
    - nao altera stop
    - nao altera take profit
    - nao altera execucao
    """

    def compare(
        self,
        *,
        trade_plan: TradePlan | None,
        intelligent_exit_plan: IntelligentExitPlan | None,
        action: str,
        planned_exit: dict[str, Decimal] | None,
    ) -> IntelligentExitComparison:
        if intelligent_exit_plan is None:
            return IntelligentExitComparison(
                symbol=trade_plan.symbol if trade_plan is not None else "",
                action=action,
                status=INTELLIGENT_EXIT_COMPARISON_STATUS_NO_EXIT_PLAN,
                reason="INTELLIGENT_EXIT_PLAN_NOT_AVAILABLE",
                trade_plan_id=trade_plan.plan_id if trade_plan is not None else None,
                diagnostics={
                    "shadow_only": True,
                    "can_use_for_execution": False,
                    "execution_unchanged": True,
                },
            )

        if trade_plan is None:
            return IntelligentExitComparison(
                symbol=intelligent_exit_plan.symbol,
                action=action,
                status=INTELLIGENT_EXIT_COMPARISON_STATUS_NO_TRADE_PLAN,
                reason="TRADE_PLAN_NOT_AVAILABLE",
                intelligent_exit_plan_id=intelligent_exit_plan.plan_id,
                protection_complete=intelligent_exit_plan.protection_complete,
                shadow_only=intelligent_exit_plan.shadow_only,
                can_use_for_execution=intelligent_exit_plan.can_use_for_execution,
                diagnostics={
                    "shadow_only": intelligent_exit_plan.shadow_only,
                    "can_use_for_execution": intelligent_exit_plan.can_use_for_execution,
                    "execution_unchanged": True,
                },
            )

        execution_stop = None
        execution_take = None
        if planned_exit is not None:
            execution_stop = planned_exit.get("stop_price")
            execution_take = planned_exit.get("take_profit_price")

        stop_changed = _changed(
            intelligent_exit_plan.recommended_stop_price,
            trade_plan.stop_price,
        )
        take_profit_changed = _changed(
            intelligent_exit_plan.recommended_take_profit_price,
            trade_plan.take_profit_price,
        )
        risk_reward_changed = _changed(
            intelligent_exit_plan.risk_reward,
            trade_plan.reward_risk,
        )

        differences = {
            "stop": {
                "trade_plan": _decimal_or_none(trade_plan.stop_price),
                "recommended": _decimal_or_none(intelligent_exit_plan.recommended_stop_price),
                "execution_candidate": _decimal_or_none(execution_stop),
                "changed": stop_changed,
            },
            "take_profit": {
                "trade_plan": _decimal_or_none(trade_plan.take_profit_price),
                "recommended": _decimal_or_none(
                    intelligent_exit_plan.recommended_take_profit_price
                ),
                "execution_candidate": _decimal_or_none(execution_take),
                "changed": take_profit_changed,
            },
            "risk_reward": {
                "trade_plan": _decimal_or_none(trade_plan.reward_risk),
                "recommended": _decimal_or_none(intelligent_exit_plan.risk_reward),
                "changed": risk_reward_changed,
            },
            "distance": {
                "stop_distance_pct": _decimal_or_none(intelligent_exit_plan.stop_distance_pct),
                "take_profit_distance_pct": _decimal_or_none(
                    intelligent_exit_plan.take_profit_distance_pct
                ),
            },
        }

        return IntelligentExitComparison(
            symbol=trade_plan.symbol,
            action=action,
            status=INTELLIGENT_EXIT_COMPARISON_STATUS_COMPARED,
            reason="INTELLIGENT_EXIT_SHADOW_COMPARED",
            trade_plan_id=trade_plan.plan_id,
            intelligent_exit_plan_id=intelligent_exit_plan.plan_id,
            stop_changed=stop_changed,
            take_profit_changed=take_profit_changed,
            risk_reward_changed=risk_reward_changed,
            protection_complete=intelligent_exit_plan.protection_complete,
            execution_unchanged=not intelligent_exit_plan.can_use_for_execution,
            can_use_for_execution=intelligent_exit_plan.can_use_for_execution,
            shadow_only=intelligent_exit_plan.shadow_only,
            differences=differences,
            diagnostics={
                "shadow_only": intelligent_exit_plan.shadow_only,
                "can_use_for_execution": intelligent_exit_plan.can_use_for_execution,
                "execution_unchanged": not intelligent_exit_plan.can_use_for_execution,
                "comparison_scope": "stop_take_profit_risk_reward",
                "stop_valid": intelligent_exit_plan.stop_valid,
                "take_profit_valid": intelligent_exit_plan.take_profit_valid,
                "protection_complete": intelligent_exit_plan.protection_complete,
                "safety_note": (
                    "Fase 5B: comparacao observacional da protecao inteligente. "
                    "Nao altera stop, take profit ou execucao."
                ),
            },
        )


def _changed(recommended: Decimal | None, current: Decimal | None) -> bool:
    if recommended is None and current is None:
        return False
    if recommended is None or current is None:
        return True
    return recommended != current


def _decimal_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)
