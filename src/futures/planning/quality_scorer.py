from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from futures.costs.model import CostEstimate
from futures.planning.exit_planner import ECONOMIC_EXIT_BLOCK_REASONS, ExitPlan
from trading.context import TradeContext
from trading.enums import OPEN_ACTIONS, TradeAction, TradePlanStatus
from trading.intent import TradeIntent

EXPECTED_NET_GAIN_NOT_POSITIVE_PENALTY = 30
HIGH_COST_BPS_PENALTY = 10
FUNDING_AGAINST_SIGNAL_PENALTY = 5
HIGH_VOLATILITY_PENALTY = 5
ECONOMIC_BLOCK_REASON = "QUALITY_SCORE_BELOW_MINIMUM"


@dataclass(frozen=True)
class QualityDecision:
    status: str
    quality_score: int
    size_multiplier: Decimal
    block_reason: str | None
    penalties: dict[str, Any] = field(default_factory=dict)
    bonuses: dict[str, Any] = field(default_factory=dict)
    hard_blocks: dict[str, Any] = field(default_factory=dict)
    economic_penalties: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


class QualityScorer:
    def score(
        self,
        *,
        intent: TradeIntent,
        context: TradeContext,
        cost: CostEstimate,
        exit_plan: ExitPlan,
    ) -> QualityDecision:
        hard_block = _hard_block_reason(intent, context, exit_plan)
        if hard_block is not None:
            return QualityDecision(
                status=TradePlanStatus.BLOCK_SAFETY.value,
                quality_score=0,
                size_multiplier=Decimal("0"),
                block_reason=hard_block,
                hard_blocks={hard_block: True},
                diagnostics={
                    "block_category": "SAFETY",
                    "hard_block": hard_block,
                    "hard_blocks": {hard_block: True},
                    "economic_penalties": {},
                },
            )

        economic_block = _economic_block_reason(exit_plan)
        if economic_block is not None:
            economic_penalties = {
                economic_block: {
                    "exit_diagnostics": exit_plan.diagnostics,
                }
            }
            return QualityDecision(
                status=TradePlanStatus.BLOCK_ECONOMIC.value,
                quality_score=0,
                size_multiplier=Decimal("0"),
                block_reason=economic_block,
                penalties=economic_penalties,
                hard_blocks={},
                economic_penalties=economic_penalties,
                diagnostics={
                    "block_category": "ECONOMIC",
                    "hard_blocks": {},
                    "economic_penalties": economic_penalties,
                    "economic_exit_blocked": True,
                    "economic_exit_block_reason": economic_block,
                },
            )

        penalties: dict[str, Any] = {}
        economic_penalties = {}
        score = intent.confidence
        cost_bps = _cost_bps(context, cost)

        if intent.action in OPEN_ACTIONS and exit_plan.expected_net_gain is not None:
            if exit_plan.expected_net_gain <= 0:
                _add_economic_penalty(
                    penalties,
                    economic_penalties,
                    key="expected_net_gain_not_positive",
                    value={
                        "expected_net_gain": str(exit_plan.expected_net_gain),
                        "round_trip_cost": str(cost.round_trip_cost),
                        "penalty_points": EXPECTED_NET_GAIN_NOT_POSITIVE_PENALTY,
                    },
                )
                score -= EXPECTED_NET_GAIN_NOT_POSITIVE_PENALTY

        if intent.action in OPEN_ACTIONS and cost_bps > Decimal("15"):
            _add_economic_penalty(
                penalties,
                economic_penalties,
                key="high_cost_bps",
                value={
                    "cost_bps": str(cost_bps),
                    "penalty_points": HIGH_COST_BPS_PENALTY,
                },
            )
            score -= HIGH_COST_BPS_PENALTY

        if intent.action in OPEN_ACTIONS and _funding_against_signal(
            intent.action, context.funding_rate
        ):
            _add_economic_penalty(
                penalties,
                economic_penalties,
                key="funding_against_signal",
                value={
                    "funding_rate": str(context.funding_rate),
                    "penalty_points": FUNDING_AGAINST_SIGNAL_PENALTY,
                },
            )
            score -= FUNDING_AGAINST_SIGNAL_PENALTY

        if (
            intent.action in OPEN_ACTIONS
            and context.volatility is not None
            and context.volatility > Decimal("0.05")
        ):
            _add_economic_penalty(
                penalties,
                economic_penalties,
                key="high_volatility",
                value={
                    "volatility": str(context.volatility),
                    "penalty_points": HIGH_VOLATILITY_PENALTY,
                },
            )
            score -= HIGH_VOLATILITY_PENALTY

        quality_score = max(0, min(100, int(score)))
        status, multiplier = _status_for_quality(quality_score)
        block_reason = (
            ECONOMIC_BLOCK_REASON if status == TradePlanStatus.BLOCK_ECONOMIC.value else None
        )

        return QualityDecision(
            status=status,
            quality_score=quality_score,
            size_multiplier=multiplier,
            block_reason=block_reason,
            penalties=penalties,
            hard_blocks={},
            economic_penalties=economic_penalties,
            diagnostics={
                "block_category": "ECONOMIC" if block_reason else None,
                "cost_bps": str(cost_bps),
                "hard_blocks": {},
                "economic_penalties": economic_penalties,
                "economic_penalties_only_reduce_score": True,
            },
        )


def _hard_block_reason(
    intent: TradeIntent,
    context: TradeContext,
    exit_plan: ExitPlan,
) -> str | None:
    if context.portfolio_snapshot.get("account_info_failed"):
        return "ACCOUNT_INFO_FAILED"
    if context.stale:
        return "STALE_DATA"
    if intent.action in OPEN_ACTIONS and context.position.is_open:
        return "POSITION_ALREADY_OPEN"
    if intent.action == TradeAction.CLOSE_POSITION.value and not context.position.is_open:
        return "CLOSE_WITHOUT_POSITION"
    if intent.action in OPEN_ACTIONS:
        if _economic_block_reason(exit_plan) is not None:
            return None
        if exit_plan.stop_price is None:
            return "STOP_NOT_PLANNED"
        if exit_plan.take_profit_price is None:
            return "TAKE_PROFIT_NOT_PLANNED"
    return None


def _economic_block_reason(exit_plan: ExitPlan) -> str | None:
    if exit_plan.block_reason in ECONOMIC_EXIT_BLOCK_REASONS:
        return exit_plan.block_reason

    diagnostics = exit_plan.diagnostics if isinstance(exit_plan.diagnostics, dict) else {}
    block_reason = diagnostics.get("block_reason")
    if block_reason in ECONOMIC_EXIT_BLOCK_REASONS:
        return str(block_reason)

    if diagnostics.get("economic_exit_blocked_unrealistic_take"):
        return "ECONOMIC_EXIT_NOT_REALISTIC"

    return None


def _cost_bps(context: TradeContext, cost: CostEstimate) -> Decimal:
    if context.market_price <= 0:
        return Decimal("0")

    return cost.minimum_profitable_move / abs(context.market_price) * Decimal("10000")


def _funding_against_signal(action: str, funding_rate: Decimal | None) -> bool:
    """Return True when the funding rate is unfavorable for the intended direction.

    Binance Futures sign convention: positive rate means longs pay shorts;
    negative rate means shorts pay longs.
    """
    if funding_rate is None:
        return False
    if action == TradeAction.OPEN_LONG.value:
        return funding_rate > 0
    if action == TradeAction.OPEN_SHORT.value:
        return funding_rate < 0
    return False


def _add_economic_penalty(
    penalties: dict[str, Any],
    economic_penalties: dict[str, Any],
    *,
    key: str,
    value: dict[str, Any],
) -> None:
    penalties[key] = value
    economic_penalties[key] = value


def _status_for_quality(score: int) -> tuple[str, Decimal]:
    if score >= 90:
        return TradePlanStatus.ALLOW_AGGRESSIVE.value, Decimal("1")
    if score >= 70:
        return TradePlanStatus.ALLOW_NORMAL.value, Decimal("1")
    if score >= 55:
        return TradePlanStatus.ALLOW_REDUCED.value, Decimal("0.70")
    if score >= 45:
        return TradePlanStatus.ALLOW_MIN_SIZE.value, Decimal("0.40")
    return TradePlanStatus.BLOCK_ECONOMIC.value, Decimal("0")
