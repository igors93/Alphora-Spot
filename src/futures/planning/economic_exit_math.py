from dataclasses import dataclass
from decimal import Decimal

from futures.planning._math import BPS
from trading.enums import TradeAction


@dataclass(frozen=True)
class EconomicExitProjection:
    action: str
    entry_price: Decimal
    quantity: Decimal
    notional: Decimal
    gross_stop_distance: Decimal
    gross_take_distance_configured: Decimal
    required_net_gain: Decimal
    required_take_distance_by_min_net_gain: Decimal
    required_take_distance_by_reward_risk: Decimal
    economic_required_take_distance: Decimal
    required_take_distance_final: Decimal
    required_take_price: Decimal
    expected_net_gain: Decimal
    expected_net_gain_bps: Decimal | None
    expected_net_loss: Decimal
    reward_risk: Decimal | None
    round_trip_cost: Decimal
    min_net_gain_bps: Decimal
    min_net_reward_risk: Decimal
    configured_take_bps: Decimal | None
    economic_required_take_bps: Decimal | None
    required_take_bps: Decimal | None
    stop_bps: Decimal | None
    max_allowed_take_distance: Decimal | None = None
    max_allowed_take_bps: Decimal | None = None
    blocked: bool = False
    block_reason: str | None = None


def calculate_economic_exit(
    *,
    action: str,
    entry_price: Decimal,
    quantity: Decimal,
    gross_stop_distance: Decimal,
    gross_take_distance_configured: Decimal,
    round_trip_cost: Decimal,
    min_net_gain_bps: Decimal,
    min_net_reward_risk: Decimal,
    max_allowed_take_distance: Decimal | None = None,
    adjustment_enabled: bool = True,
) -> EconomicExitProjection:
    normalized_action = str(action or "").upper().strip()
    if normalized_action not in {
        TradeAction.OPEN_LONG.value,
        TradeAction.OPEN_SHORT.value,
    }:
        raise ValueError(f"action invalida para calculo economico: {action}")

    entry = Decimal(str(entry_price))
    qty = abs(Decimal(str(quantity)))
    stop_distance = abs(Decimal(str(gross_stop_distance)))
    configured_take_distance = abs(Decimal(str(gross_take_distance_configured)))
    cost = abs(Decimal(str(round_trip_cost)))
    min_gain_bps = Decimal(str(min_net_gain_bps))
    min_reward_risk = Decimal(str(min_net_reward_risk))

    if entry <= 0:
        raise ValueError("entry_price precisa ser maior que zero")
    if qty <= 0:
        raise ValueError("quantity precisa ser maior que zero")
    if min_gain_bps < 0:
        raise ValueError("min_net_gain_bps precisa ser maior ou igual a zero")
    if min_reward_risk < 0:
        raise ValueError("min_net_reward_risk precisa ser maior ou igual a zero")

    notional = abs(entry * qty)
    required_net_gain = notional * min_gain_bps / BPS
    gross_loss = stop_distance * qty
    expected_net_loss = gross_loss + cost

    required_take_distance_by_min_net_gain = (cost + required_net_gain) / qty
    required_take_distance_by_reward_risk = (cost + (expected_net_loss * min_reward_risk)) / qty
    economic_required_take_distance = max(
        required_take_distance_by_min_net_gain,
        required_take_distance_by_reward_risk,
    )
    required_take_distance_final = (
        max(configured_take_distance, economic_required_take_distance)
        if adjustment_enabled
        else configured_take_distance
    )
    required_take_price = _take_price(
        action=normalized_action,
        entry_price=entry,
        take_distance=required_take_distance_final,
    )
    expected_net_gain = required_take_distance_final * qty - cost
    reward_risk = expected_net_gain / expected_net_loss if expected_net_loss > 0 else None

    max_distance = (
        abs(Decimal(str(max_allowed_take_distance)))
        if max_allowed_take_distance is not None
        else None
    )
    blocked = (
        bool(adjustment_enabled)
        and max_distance is not None
        and max_distance > 0
        and configured_take_distance < economic_required_take_distance
        and economic_required_take_distance > max_distance
    )

    return EconomicExitProjection(
        action=normalized_action,
        entry_price=entry,
        quantity=qty,
        notional=notional,
        gross_stop_distance=stop_distance,
        gross_take_distance_configured=configured_take_distance,
        required_net_gain=required_net_gain,
        required_take_distance_by_min_net_gain=required_take_distance_by_min_net_gain,
        required_take_distance_by_reward_risk=required_take_distance_by_reward_risk,
        economic_required_take_distance=economic_required_take_distance,
        required_take_distance_final=required_take_distance_final,
        required_take_price=required_take_price,
        expected_net_gain=expected_net_gain,
        expected_net_gain_bps=_value_bps(expected_net_gain, notional),
        expected_net_loss=expected_net_loss,
        reward_risk=reward_risk,
        round_trip_cost=cost,
        min_net_gain_bps=min_gain_bps,
        min_net_reward_risk=min_reward_risk,
        configured_take_bps=_distance_bps(configured_take_distance, entry),
        economic_required_take_bps=_distance_bps(
            economic_required_take_distance,
            entry,
        ),
        required_take_bps=_distance_bps(required_take_distance_final, entry),
        stop_bps=_distance_bps(stop_distance, entry),
        max_allowed_take_distance=max_distance,
        max_allowed_take_bps=(
            _distance_bps(max_distance, entry) if max_distance is not None else None
        ),
        blocked=blocked,
        block_reason=("ECONOMIC_TAKE_REQUIRED_TOO_FAR" if blocked else None),
    )


def _take_price(
    *,
    action: str,
    entry_price: Decimal,
    take_distance: Decimal,
) -> Decimal:
    if action == TradeAction.OPEN_LONG.value:
        return entry_price + take_distance
    return entry_price - take_distance


def _value_bps(value: Decimal | None, notional: Decimal) -> Decimal | None:
    if value is None or notional <= 0:
        return None
    return value / notional * BPS


def _distance_bps(distance: Decimal | None, entry_price: Decimal) -> Decimal | None:
    if distance is None or entry_price <= 0:
        return None
    return distance / entry_price * BPS
