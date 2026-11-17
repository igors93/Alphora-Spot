from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from trading.context import TradeContext
from trading.enums import OPEN_ACTIONS, TradeAction
from trading.plan import TradePlan

PROFILE_REFERENCE_FLOW = "PROFILE_REFERENCE"


@dataclass(frozen=True)
class TradePlanComparison:
    symbol: str
    action: str
    plan_id: str
    plan_status: str
    reference_flow: str
    reference_quantity: Decimal
    planned_quantity: Decimal
    quantity_delta: Decimal
    reference_notional: Decimal
    planned_notional: Decimal
    notional_delta: Decimal
    reference_stop_price: Decimal | None
    planned_stop_price: Decimal | None
    stop_price_delta: Decimal | None
    reference_take_profit_price: Decimal | None
    planned_take_profit_price: Decimal | None
    take_profit_price_delta: Decimal | None
    quantity_changed: bool
    notional_changed: bool
    exit_changed: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)


def compare_plan_to_profile_reference(
    *,
    plan: TradePlan,
    context: TradeContext,
) -> TradePlanComparison:
    reference_quantity = _reference_quantity(plan.action, context)
    reference_notional = (
        reference_quantity * context.market_price if context.market_price > 0 else Decimal("0")
    )
    reference_stop, reference_take, exit_comparable, exit_reason = _reference_exit(
        plan.action,
        context,
    )

    quantity_delta = plan.quantity - reference_quantity
    notional_delta = plan.notional - reference_notional
    stop_delta = _delta_or_none(plan.stop_price, reference_stop)
    take_delta = _delta_or_none(plan.take_profit_price, reference_take)

    exit_changed = bool(
        exit_comparable
        and (plan.stop_price != reference_stop or plan.take_profit_price != reference_take)
    )

    return TradePlanComparison(
        symbol=plan.symbol,
        action=plan.action,
        plan_id=plan.plan_id,
        plan_status=plan.status,
        reference_flow=PROFILE_REFERENCE_FLOW,
        reference_quantity=reference_quantity,
        planned_quantity=plan.quantity,
        quantity_delta=quantity_delta,
        reference_notional=reference_notional,
        planned_notional=plan.notional,
        notional_delta=notional_delta,
        reference_stop_price=reference_stop,
        planned_stop_price=plan.stop_price,
        stop_price_delta=stop_delta,
        reference_take_profit_price=reference_take,
        planned_take_profit_price=plan.take_profit_price,
        take_profit_price_delta=take_delta,
        quantity_changed=quantity_delta != 0,
        notional_changed=notional_delta != 0,
        exit_changed=exit_changed,
        diagnostics={
            "shadow_only": True,
            "execution_unchanged": True,
            "profile_reference": "profile_notional_and_fixed_pct_exit",
            "exit_comparable": exit_comparable,
            "exit_not_comparable_reason": exit_reason,
            "market_price": str(context.market_price),
            "position_notional_usdt": str(context.symbol_config.position_notional_usdt),
            "exit_model": _exit_model_value(context),
        },
    )


def _reference_quantity(action: str, context: TradeContext) -> Decimal:
    if action == TradeAction.CLOSE_POSITION.value:
        return context.position.quantity

    if action not in OPEN_ACTIONS:
        return Decimal("0")

    if context.market_price <= 0:
        return Decimal("0")

    notional = Decimal(str(context.symbol_config.position_notional_usdt))
    return context.exchange_rules.round_quantity(notional / context.market_price)


def _reference_exit(
    action: str,
    context: TradeContext,
) -> tuple[Decimal | None, Decimal | None, bool, str | None]:
    if action not in OPEN_ACTIONS:
        return None, None, False, "EXIT_NOT_REQUIRED_FOR_ACTION"

    exit_model = _exit_model_value(context)
    if exit_model != "FIXED_PCT":
        return None, None, False, f"{exit_model}_NOT_COMPARABLE_IN_SHADOW_REFERENCE"

    entry = context.market_price
    stop_pct = Decimal(str(context.symbol_config.stop_loss_pct))
    take_pct = Decimal(str(context.symbol_config.take_profit_pct))

    if action == TradeAction.OPEN_LONG.value:
        stop = context.exchange_rules.round_price_down(entry * (Decimal("1") - stop_pct))
        take = context.exchange_rules.round_price_up(entry * (Decimal("1") + take_pct))
    else:
        stop = context.exchange_rules.round_price_up(entry * (Decimal("1") + stop_pct))
        take = context.exchange_rules.round_price_down(entry * (Decimal("1") - take_pct))

    return stop, take, True, None


def _exit_model_value(context: TradeContext) -> str:
    raw = context.symbol_config.exit_model
    value = getattr(raw, "value", str(raw))
    return str(value).upper().strip()


def _delta_or_none(
    planned: Decimal | None,
    reference: Decimal | None,
) -> Decimal | None:
    if planned is None or reference is None:
        return None
    return planned - reference
