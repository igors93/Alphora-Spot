from decimal import Decimal
from typing import Any

from futures.costs.model import CostEstimate, CostModel
from futures.costs.outcome import BPS, estimate_outcome_costs
from futures.planning._math import decimal_or_none
from futures.planning.exit_planner import (
    ECONOMIC_EXIT_BLOCK_REASONS,
    ExitPlan,
    ExitPlanner,
)
from futures.planning.position_sizer import PositionSizer
from futures.planning.quality_scorer import QualityScorer
from trading.context import TradeContext
from trading.diagnostics import merge_diagnostics
from trading.enums import TradePlanStatus
from trading.intent import TradeIntent
from trading.plan import TRADE_PLAN_SOURCE_SHADOW, TRADE_PLAN_VERSION, TradePlan


class TradePlanBuilder:
    def __init__(
        self,
        *,
        cost_model: CostModel | None = None,
        exit_planner: ExitPlanner | None = None,
        quality_scorer: QualityScorer | None = None,
        position_sizer: PositionSizer | None = None,
        plan_version: str = TRADE_PLAN_VERSION,
        source: str = TRADE_PLAN_SOURCE_SHADOW,
        policy_version: str | None = None,
    ):
        self.cost_model = cost_model or CostModel()
        self.exit_planner = exit_planner or ExitPlanner()
        self.quality_scorer = quality_scorer or QualityScorer()
        self.position_sizer = position_sizer or PositionSizer()
        self.plan_version = plan_version
        self.source = source
        self.policy_version = policy_version

    @classmethod
    def from_config(cls, config) -> "TradePlanBuilder":
        return cls(
            cost_model=CostModel.from_config(config),
            exit_planner=ExitPlanner.from_config(config),
        )

    def _outcome_costs(
        self,
        intent: TradeIntent,
        context: TradeContext,
        exit_plan: ExitPlan,
    ) -> dict[str, Any]:
        """Per-outcome costs and the breakeven probability p* (OPP-007); diagnostic only."""

        direction = {"OPEN_LONG": "LONG", "OPEN_SHORT": "SHORT"}.get(str(intent.action), "")
        if not direction or exit_plan.stop_price is None or exit_plan.take_profit_price is None:
            return {}
        costs = estimate_outcome_costs(
            direction=direction,
            entry_price=context.market_price,
            stop_price=exit_plan.stop_price,
            take_price=exit_plan.take_profit_price,
            cost_model=self.cost_model,
            spread_bps=abs(context.spread_pct) * BPS if context.spread_pct is not None else None,
            funding_rate=context.funding_rate,
        )
        return costs.to_dict() if costs is not None else {}

    def build(self, *, intent: TradeIntent, context: TradeContext) -> TradePlan:
        base_quantity = _base_quantity(intent, context)
        initial_cost = self.cost_model.estimate(
            context=context,
            quantity=base_quantity,
            execution_style="MARKET",
        )
        initial_exit_plan = self.exit_planner.plan(
            intent=intent,
            context=context,
            quantity=base_quantity,
            cost=initial_cost,
        )
        quality = self.quality_scorer.score(
            intent=intent,
            context=context,
            cost=initial_cost,
            exit_plan=initial_exit_plan,
        )
        sized = self.position_sizer.size(
            intent=intent,
            context=context,
            quality=quality,
        )
        final_cost = self.cost_model.estimate(
            context=context,
            quantity=sized.quantity,
            execution_style="MARKET",
        )
        final_exit_plan = self.exit_planner.plan(
            intent=intent,
            context=context,
            quantity=sized.quantity,
            cost=final_cost,
        )
        if quality.block_reason in ECONOMIC_EXIT_BLOCK_REASONS:
            final_exit_plan = initial_exit_plan

        status = quality.status
        block_reason = quality.block_reason or sized.block_reason
        if sized.block_reason and status not in {
            TradePlanStatus.BLOCK_SAFETY.value,
            TradePlanStatus.BLOCK_ECONOMIC.value,
        }:
            status = TradePlanStatus.BLOCK_ECONOMIC.value

        base_diagnostics: dict[str, Any] = {
            "shadow_mode_safe": True,
            "execution_unchanged": True,
            "plan_version": self.plan_version,
            "plan_source": self.source,
            "initial_quantity": str(base_quantity),
            "final_quantity": str(sized.quantity),
            "exit_recalculated_after_sizing": True,
        }
        if self.policy_version is not None:
            base_diagnostics["policy_version"] = self.policy_version
        diagnostics = merge_diagnostics(
            base_diagnostics,
            {"cost": final_cost.diagnostics},
            {"cost_summary": _cost_summary(final_cost)},
            {"exit": final_exit_plan.diagnostics},
            {"quality": quality.diagnostics},
            {"sizing": sized.diagnostics},
            {"initial_plan": _initial_plan_diagnostics(initial_cost, initial_exit_plan)},
            {"penalties": quality.penalties},
            {"bonuses": quality.bonuses},
            {"outcome_costs": self._outcome_costs(intent, context, final_exit_plan)},
        )
        return TradePlan(
            symbol=context.symbol,
            action=intent.action,
            status=status,
            strategy_name=intent.strategy_name,
            signal_confidence=intent.confidence,
            quality_score=quality.quality_score,
            entry_reference_price=context.market_price,
            quantity=sized.quantity,
            notional=sized.notional,
            stop_price=final_exit_plan.stop_price,
            take_profit_price=final_exit_plan.take_profit_price,
            estimated_entry_fee=final_cost.entry_fee,
            estimated_exit_fee=final_cost.exit_fee,
            estimated_spread_cost=final_cost.spread_cost,
            estimated_slippage_cost=final_cost.slippage_cost,
            estimated_funding_cost=final_cost.funding_cost,
            estimated_round_trip_cost=final_cost.round_trip_cost,
            expected_net_gain=final_exit_plan.expected_net_gain,
            expected_net_loss=final_exit_plan.expected_net_loss,
            reward_risk=final_exit_plan.reward_risk,
            size_multiplier=sized.size_multiplier,
            execution_style="MARKET",
            management_style="PROTECTION_POLICY_ONLY",
            reason=intent.reason,
            block_reason=block_reason,
            plan_version=self.plan_version,
            source=self.source,
            diagnostics=diagnostics,
        )


def _base_quantity(intent: TradeIntent, context: TradeContext) -> Decimal:
    if intent.action == "CLOSE_POSITION":
        return context.position.quantity
    notional = Decimal(str(context.symbol_config.position_notional_usdt))
    if context.market_price <= 0:
        return Decimal("0")
    return context.exchange_rules.round_quantity(notional / context.market_price)


def _initial_plan_diagnostics(
    initial_cost: CostEstimate,
    initial_exit_plan: ExitPlan,
) -> dict:
    return {
        "cost": initial_cost.diagnostics,
        "cost_summary": _cost_summary(initial_cost),
        "exit": initial_exit_plan.diagnostics,
        "expected_net_gain": decimal_or_none(initial_exit_plan.expected_net_gain),
        "expected_net_loss": decimal_or_none(initial_exit_plan.expected_net_loss),
        "reward_risk": decimal_or_none(initial_exit_plan.reward_risk),
    }


def _cost_summary(cost: CostEstimate) -> dict:
    return {
        "cost_model_version": cost.cost_model_version,
        "notional": str(cost.notional),
        "entry_fee": str(cost.entry_fee),
        "exit_fee": str(cost.exit_fee),
        "spread_cost": str(cost.spread_cost),
        "slippage_cost": str(cost.slippage_cost),
        "funding_cost": str(cost.funding_cost),
        "round_trip_cost": str(cost.round_trip_cost),
        "round_trip_cost_bps": str(cost.round_trip_cost_bps),
        "minimum_profitable_move": str(cost.minimum_profitable_move),
    }
