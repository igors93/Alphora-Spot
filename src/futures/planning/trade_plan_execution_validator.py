from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from futures.planning._math import BPS, decimal_or_none
from futures.planning.economic_gate import EconomicGateDecision, EconomicTradeGate
from trading.enums import OPEN_ACTIONS
from trading.plan import TradePlan


@dataclass(frozen=True)
class TradePlanExecutionValidation:
    decision: EconomicGateDecision
    effective_plan: TradePlan | None
    original_quantity: Decimal | None
    final_quantity: Decimal | None
    original_stop_price: Decimal | None
    final_stop_price: Decimal | None
    original_take_profit_price: Decimal | None
    final_take_profit_price: Decimal | None
    diagnostics: dict[str, Any]


class TradePlanExecutionValidator:
    """Validates the exact trade payload that can reach execution."""

    def __init__(self, gate: EconomicTradeGate):
        self.gate = gate

    def evaluate(
        self,
        *,
        plan: TradePlan | None,
        action: str,
        planned_quantity: Decimal | None = None,
        planned_exit: dict[str, Decimal] | None = None,
    ) -> TradePlanExecutionValidation:
        effective_plan = build_effective_trade_plan(
            plan=plan,
            action=action,
            planned_quantity=planned_quantity,
            planned_exit=planned_exit,
        )
        decision = self.gate.evaluate(effective_plan, action)
        original_quantity = plan.quantity if plan is not None else None
        original_stop = plan.stop_price if plan is not None else None
        original_take = plan.take_profit_price if plan is not None else None
        final_quantity = effective_plan.quantity if effective_plan is not None else planned_quantity
        final_stop = effective_plan.stop_price if effective_plan is not None else None
        final_take = effective_plan.take_profit_price if effective_plan is not None else None
        diagnostics = {
            "validator_version": "1.0.0",
            "effective_plan_available": effective_plan is not None,
            "overrides": {
                "planned_quantity_applied": planned_quantity is not None,
                "planned_exit_applied": planned_exit is not None,
            },
            "original": {
                "quantity": decimal_or_none(original_quantity),
                "stop_price": decimal_or_none(original_stop),
                "take_profit_price": decimal_or_none(original_take),
            },
            "final": {
                "quantity": decimal_or_none(final_quantity),
                "stop_price": decimal_or_none(final_stop),
                "take_profit_price": decimal_or_none(final_take),
            },
        }
        return TradePlanExecutionValidation(
            decision=decision,
            effective_plan=effective_plan,
            original_quantity=original_quantity,
            final_quantity=final_quantity,
            original_stop_price=original_stop,
            final_stop_price=final_stop,
            original_take_profit_price=original_take,
            final_take_profit_price=final_take,
            diagnostics=diagnostics,
        )


def build_effective_trade_plan(
    *,
    plan: TradePlan | None,
    action: str,
    planned_quantity: Decimal | None = None,
    planned_exit: dict[str, Decimal] | None = None,
) -> TradePlan | None:
    if plan is None:
        return None

    normalized_action = str(action or "").upper().strip()
    if normalized_action not in OPEN_ACTIONS:
        return plan

    final_quantity = abs(
        Decimal(str(planned_quantity)) if planned_quantity is not None else plan.quantity
    )
    final_stop = (
        Decimal(str(planned_exit["stop_price"]))
        if planned_exit is not None and planned_exit.get("stop_price") is not None
        else plan.stop_price
    )
    final_take = (
        Decimal(str(planned_exit["take_profit_price"]))
        if planned_exit is not None and planned_exit.get("take_profit_price") is not None
        else plan.take_profit_price
    )

    cost_scale = (
        final_quantity / abs(plan.quantity)
        if plan.quantity is not None and abs(plan.quantity) > 0
        else Decimal("0")
    )
    entry_fee = plan.estimated_entry_fee * cost_scale
    exit_fee = plan.estimated_exit_fee * cost_scale
    spread_cost = plan.estimated_spread_cost * cost_scale
    slippage_cost = plan.estimated_slippage_cost * cost_scale
    funding_cost = plan.estimated_funding_cost * cost_scale
    round_trip_cost = entry_fee + exit_fee + spread_cost + slippage_cost + funding_cost
    notional = abs(final_quantity * plan.entry_reference_price)
    expected_net_gain = _expected_net_gain(
        entry=plan.entry_reference_price,
        take=final_take,
        quantity=final_quantity,
        round_trip_cost=round_trip_cost,
    )
    expected_net_loss = _expected_net_loss(
        entry=plan.entry_reference_price,
        stop=final_stop,
        quantity=final_quantity,
        round_trip_cost=round_trip_cost,
    )
    reward_risk = (
        expected_net_gain / expected_net_loss
        if expected_net_gain is not None and expected_net_loss is not None and expected_net_loss > 0
        else None
    )
    diagnostics = _effective_diagnostics(
        plan=plan,
        final_quantity=final_quantity,
        final_stop=final_stop,
        final_take=final_take,
        cost_scale=cost_scale,
        round_trip_cost=round_trip_cost,
        notional=notional,
    )

    return replace(
        plan,
        quantity=final_quantity,
        notional=notional,
        stop_price=final_stop,
        take_profit_price=final_take,
        estimated_entry_fee=entry_fee,
        estimated_exit_fee=exit_fee,
        estimated_spread_cost=spread_cost,
        estimated_slippage_cost=slippage_cost,
        estimated_funding_cost=funding_cost,
        estimated_round_trip_cost=round_trip_cost,
        expected_net_gain=expected_net_gain,
        expected_net_loss=expected_net_loss,
        reward_risk=reward_risk,
        diagnostics=diagnostics,
    )


def _expected_net_gain(
    *,
    entry: Decimal,
    take: Decimal | None,
    quantity: Decimal,
    round_trip_cost: Decimal,
) -> Decimal | None:
    if take is None:
        return None
    return abs(take - entry) * quantity - round_trip_cost


def _expected_net_loss(
    *,
    entry: Decimal,
    stop: Decimal | None,
    quantity: Decimal,
    round_trip_cost: Decimal,
) -> Decimal | None:
    if stop is None:
        return None
    return abs(entry - stop) * quantity + round_trip_cost


def _effective_diagnostics(
    *,
    plan: TradePlan,
    final_quantity: Decimal,
    final_stop: Decimal | None,
    final_take: Decimal | None,
    cost_scale: Decimal,
    round_trip_cost: Decimal,
    notional: Decimal,
) -> dict[str, Any]:
    diagnostics = dict(plan.diagnostics) if isinstance(plan.diagnostics, dict) else {}
    minimum_profitable_move = (
        round_trip_cost / final_quantity if final_quantity > 0 else Decimal("0")
    )
    round_trip_cost_bps = round_trip_cost / notional * BPS if notional > 0 else Decimal("0")
    cost_summary = dict(diagnostics.get("cost_summary", {}))
    cost_summary.update(
        {
            "notional": str(notional),
            "entry_fee": str(plan.estimated_entry_fee * cost_scale),
            "exit_fee": str(plan.estimated_exit_fee * cost_scale),
            "spread_cost": str(plan.estimated_spread_cost * cost_scale),
            "slippage_cost": str(plan.estimated_slippage_cost * cost_scale),
            "funding_cost": str(plan.estimated_funding_cost * cost_scale),
            "round_trip_cost": str(round_trip_cost),
            "round_trip_cost_bps": str(round_trip_cost_bps),
            "minimum_profitable_move": str(minimum_profitable_move),
        }
    )
    cost = dict(diagnostics.get("cost", {}))
    components = dict(cost.get("components", {}))
    components.update(
        {
            "entry_fee": str(plan.estimated_entry_fee * cost_scale),
            "exit_fee": str(plan.estimated_exit_fee * cost_scale),
            "spread_cost": str(plan.estimated_spread_cost * cost_scale),
            "slippage_cost": str(plan.estimated_slippage_cost * cost_scale),
            "funding_cost": str(plan.estimated_funding_cost * cost_scale),
        }
    )
    cost.update(
        {
            "notional": str(notional),
            "round_trip_cost": str(round_trip_cost),
            "round_trip_cost_bps": str(round_trip_cost_bps),
            "minimum_profitable_move": str(minimum_profitable_move),
            "components": components,
        }
    )
    diagnostics.update(
        {
            "cost_summary": cost_summary,
            "cost": cost,
            "execution_effective_plan": {
                "original_quantity": str(plan.quantity),
                "final_quantity": str(final_quantity),
                "original_stop_price": decimal_or_none(plan.stop_price),
                "final_stop_price": decimal_or_none(final_stop),
                "original_take_profit_price": decimal_or_none(plan.take_profit_price),
                "final_take_profit_price": decimal_or_none(final_take),
                "cost_scaled_from_trade_plan": True,
                "cost_scale_ratio": str(cost_scale),
            },
        }
    )
    return diagnostics
