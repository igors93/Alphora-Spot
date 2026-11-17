from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from domain.models import FuturesPosition, PositionDirection, TradeAction
from domain.symbol_rules import SymbolRules
from futures.exit_models import FixedPctExitModel
from futures.risk_manager import FuturesRiskManager

PROTECTION_SOURCE_STATIC = "STATIC_PROTECTION_MODEL"
PROTECTION_SOURCE_TRADE_PLAN = "TRADE_PLAN"


@dataclass(frozen=True)
class ExpectedProtection:
    close_side: str
    stop_price: Decimal
    take_profit_price: Decimal
    working_type: str
    model: str = "FIXED_PCT"
    warning: str = ""
    source: str = PROTECTION_SOURCE_STATIC
    trade_plan_id: str | None = None


@dataclass(frozen=True)
class PostFillProtectionValidation:
    allowed: bool
    reason: str
    expected: ExpectedProtection | None
    adjusted: bool
    risk_increase_pct: Decimal
    take_extension_pct: Decimal
    diagnostics: dict


@dataclass(frozen=True)
class ProtectionAuditResult:
    ok: bool
    stop_order: dict | None
    take_order: dict | None
    missing: list[str]
    invalid: list[str]
    expected: ExpectedProtection


def build_expected_protection_for_entry(
    *,
    action: TradeAction,
    entry_price: Decimal,
    risk_manager: FuturesRiskManager,
    rules: SymbolRules,
    working_type: str,
    exit_model=None,
    candles=None,
    planned_stop_price: Decimal | None = None,
    planned_take_profit_price: Decimal | None = None,
    trade_plan_id: str | None = None,
) -> ExpectedProtection:
    """Build the exact protection payload expected after an entry fill.

    This pure preview is shared by the pre-execution guard and the post-fill engine so
    both layers evaluate the same rounding, exit model and working type.
    """

    action = TradeAction(action)
    entry_price = Decimal(str(entry_price))
    if entry_price <= 0:
        raise ValueError("entry_price precisa ser maior que zero")

    if action == TradeAction.OPEN_LONG:
        close_side = "SELL"
    elif action == TradeAction.OPEN_SHORT:
        close_side = "BUY"
    else:
        raise ValueError(f"Acao invalida para protecao: {action}")

    if planned_stop_price is not None or planned_take_profit_price is not None:
        if planned_stop_price is None or planned_take_profit_price is None:
            raise ValueError("planned_stop_price e planned_take_profit_price devem vir juntos")

        stop_price, take_profit_price = normalize_planned_exit_prices(
            action=action,
            entry_price=entry_price,
            rules=rules,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
        )
        return ExpectedProtection(
            close_side=close_side,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            working_type=working_type,
            model="TRADE_PLAN",
            source=PROTECTION_SOURCE_TRADE_PLAN,
            trade_plan_id=trade_plan_id,
        )

    model = exit_model or FixedPctExitModel()
    exit_prices = model.calculate(
        action=action,
        entry_price=entry_price,
        rules=rules,
        stop_loss_pct=risk_manager.stop_loss_pct,
        take_profit_pct=risk_manager.take_profit_pct,
        candles=candles,
    )

    return ExpectedProtection(
        close_side=close_side,
        stop_price=exit_prices.stop_price,
        take_profit_price=exit_prices.take_profit_price,
        working_type=working_type,
        model=exit_prices.model,
        warning=exit_prices.warning,
    )


def build_expected_protection(
    *,
    position: FuturesPosition,
    risk_manager: FuturesRiskManager,
    rules: SymbolRules,
    working_type: str,
    exit_model=None,
    candles=None,
    planned_stop_price: Decimal | None = None,
    planned_take_profit_price: Decimal | None = None,
    trade_plan_id: str | None = None,
) -> ExpectedProtection:
    if position.direction == PositionDirection.LONG:
        action = TradeAction.OPEN_LONG
    elif position.direction == PositionDirection.SHORT:
        action = TradeAction.OPEN_SHORT
    else:
        raise ValueError(f"Direcao invalida para protecao: {position.direction}")

    return build_expected_protection_for_entry(
        action=action,
        entry_price=position.entry_price,
        risk_manager=risk_manager,
        rules=rules,
        working_type=working_type,
        exit_model=exit_model,
        candles=candles,
        planned_stop_price=planned_stop_price,
        planned_take_profit_price=planned_take_profit_price,
        trade_plan_id=trade_plan_id,
    )


def revalidate_expected_protection_after_fill(
    *,
    action: TradeAction,
    planned_entry_price: Decimal,
    actual_entry_price: Decimal,
    trigger_price: Decimal,
    risk_manager: FuturesRiskManager,
    rules: SymbolRules,
    working_type: str,
    exit_model=None,
    prevalidated: ExpectedProtection | None = None,
    max_risk_increase_pct: Decimal = Decimal("10"),
    trigger_buffer_bps: Decimal = Decimal("3"),
) -> PostFillProtectionValidation:
    """Recalculate and validate the exact protection after the entry fill.

    The entry command may be filled away from its planning reference.  This helper
    preserves the pre-dispatch protection geometry, recalculates it from the actual
    average fill, and only applies a bounded trigger-side adjustment.  The returned
    ``ExpectedProtection`` is the exact payload the engine must submit.
    """

    action = TradeAction(action)
    planned_entry = Decimal(str(planned_entry_price))
    actual_entry = Decimal(str(actual_entry_price))
    trigger = Decimal(str(trigger_price))
    max_increase = max(Decimal("0"), Decimal(str(max_risk_increase_pct)))
    buffer_bps = max(Decimal("0"), Decimal(str(trigger_buffer_bps)))
    if planned_entry <= 0 or actual_entry <= 0 or trigger <= 0:
        return PostFillProtectionValidation(
            allowed=False,
            reason="POST_FILL_PROTECTION_INVALID_PRICE",
            expected=None,
            adjusted=False,
            risk_increase_pct=Decimal("0"),
            take_extension_pct=Decimal("0"),
            diagnostics={
                "planned_entry_price": planned_entry,
                "actual_entry_price": actual_entry,
                "trigger_price": trigger,
            },
        )

    baseline = _rebase_expected_protection(
        action=action,
        planned_entry_price=planned_entry,
        actual_entry_price=actual_entry,
        risk_manager=risk_manager,
        rules=rules,
        working_type=working_type,
        exit_model=exit_model,
        prevalidated=prevalidated,
    )
    position = _position_for_protection_validation(action, actual_entry)
    breached = breached_protection_triggers(
        position=position,
        expected=baseline,
        current_price=trigger,
    )
    if not breached:
        return PostFillProtectionValidation(
            allowed=True,
            reason="POST_FILL_PROTECTION_VALID",
            expected=baseline,
            adjusted=False,
            risk_increase_pct=Decimal("0"),
            take_extension_pct=Decimal("0"),
            diagnostics=_post_fill_diagnostics(
                planned_entry=planned_entry,
                actual_entry=actual_entry,
                trigger=trigger,
                baseline=baseline,
                final=baseline,
                breached=breached,
                buffer_bps=buffer_bps,
            ),
        )

    buffer_distance = max(
        rules.price_tick,
        abs(trigger) * buffer_bps / Decimal("10000"),
    )
    stop = baseline.stop_price
    take = baseline.take_profit_price
    if action is TradeAction.OPEN_LONG:
        if "STOP_MARKET" in breached:
            stop = rules.round_price_down(trigger - buffer_distance)
        if "TAKE_PROFIT_MARKET" in breached:
            take = rules.round_price_up(trigger + buffer_distance)
    else:
        if "STOP_MARKET" in breached:
            stop = rules.round_price_up(trigger + buffer_distance)
        if "TAKE_PROFIT_MARKET" in breached:
            take = rules.round_price_down(trigger - buffer_distance)

    try:
        stop, take = normalize_planned_exit_prices(
            action=action,
            entry_price=actual_entry,
            rules=rules,
            planned_stop_price=stop,
            planned_take_profit_price=take,
        )
    except ValueError as error:
        return PostFillProtectionValidation(
            allowed=False,
            reason="POST_FILL_PROTECTION_INVALID_AFTER_ADJUSTMENT",
            expected=None,
            adjusted=True,
            risk_increase_pct=Decimal("0"),
            take_extension_pct=Decimal("0"),
            diagnostics={
                "error": str(error),
                "breached_triggers": breached,
                "planned_entry_price": planned_entry,
                "actual_entry_price": actual_entry,
                "trigger_price": trigger,
            },
        )

    final = ExpectedProtection(
        close_side=baseline.close_side,
        stop_price=stop,
        take_profit_price=take,
        working_type=baseline.working_type,
        model=baseline.model,
        warning=baseline.warning,
        source=baseline.source,
        trade_plan_id=baseline.trade_plan_id,
    )
    baseline_risk = abs(actual_entry - baseline.stop_price)
    final_risk = abs(actual_entry - final.stop_price)
    baseline_take = abs(baseline.take_profit_price - actual_entry)
    final_take = abs(final.take_profit_price - actual_entry)
    risk_increase = _percentage_increase(baseline_risk, final_risk)
    take_extension = _percentage_increase(baseline_take, final_take)
    final_breached = breached_protection_triggers(
        position=position,
        expected=final,
        current_price=trigger,
    )
    allowed = (
        not final_breached and risk_increase <= max_increase and take_extension <= max_increase
    )
    reason = (
        "POST_FILL_PROTECTION_ADJUSTED"
        if allowed
        else "POST_FILL_PROTECTION_ADJUSTMENT_EXCEEDS_LIMIT"
    )
    return PostFillProtectionValidation(
        allowed=allowed,
        reason=reason,
        expected=final if allowed else None,
        adjusted=True,
        risk_increase_pct=risk_increase,
        take_extension_pct=take_extension,
        diagnostics={
            **_post_fill_diagnostics(
                planned_entry=planned_entry,
                actual_entry=actual_entry,
                trigger=trigger,
                baseline=baseline,
                final=final,
                breached=breached,
                buffer_bps=buffer_bps,
            ),
            "remaining_breached_triggers": final_breached,
            "risk_increase_pct": risk_increase,
            "take_extension_pct": take_extension,
            "max_adjustment_pct": max_increase,
        },
    )


def _rebase_expected_protection(
    *,
    action: TradeAction,
    planned_entry_price: Decimal,
    actual_entry_price: Decimal,
    risk_manager: FuturesRiskManager,
    rules: SymbolRules,
    working_type: str,
    exit_model,
    prevalidated: ExpectedProtection | None,
) -> ExpectedProtection:
    if prevalidated is None:
        return build_expected_protection_for_entry(
            action=action,
            entry_price=actual_entry_price,
            risk_manager=risk_manager,
            rules=rules,
            working_type=working_type,
            exit_model=exit_model,
        )

    stop_ratio = abs(prevalidated.stop_price - planned_entry_price) / planned_entry_price
    take_ratio = abs(prevalidated.take_profit_price - planned_entry_price) / planned_entry_price
    if action is TradeAction.OPEN_LONG:
        stop = actual_entry_price * (Decimal("1") - stop_ratio)
        take = actual_entry_price * (Decimal("1") + take_ratio)
    else:
        stop = actual_entry_price * (Decimal("1") + stop_ratio)
        take = actual_entry_price * (Decimal("1") - take_ratio)
    stop, take = normalize_planned_exit_prices(
        action=action,
        entry_price=actual_entry_price,
        rules=rules,
        planned_stop_price=stop,
        planned_take_profit_price=take,
    )
    return ExpectedProtection(
        close_side=prevalidated.close_side,
        stop_price=stop,
        take_profit_price=take,
        working_type=prevalidated.working_type,
        model=prevalidated.model,
        warning=prevalidated.warning,
        source=prevalidated.source,
        trade_plan_id=prevalidated.trade_plan_id,
    )


def _position_for_protection_validation(
    action: TradeAction,
    entry_price: Decimal,
) -> FuturesPosition:
    direction = (
        PositionDirection.LONG if action is TradeAction.OPEN_LONG else PositionDirection.SHORT
    )
    return FuturesPosition(
        symbol="PROTECTION_VALIDATION",
        direction=direction,
        quantity=Decimal("1"),
        entry_price=entry_price,
        unrealized_pnl=Decimal("0"),
    )


def _percentage_increase(baseline: Decimal, final: Decimal) -> Decimal:
    if baseline <= 0:
        return Decimal("0") if final <= 0 else Decimal("1000000")
    if final <= baseline:
        return Decimal("0")
    return (final - baseline) / baseline * Decimal("100")


def _post_fill_diagnostics(
    *,
    planned_entry: Decimal,
    actual_entry: Decimal,
    trigger: Decimal,
    baseline: ExpectedProtection,
    final: ExpectedProtection,
    breached: list[str],
    buffer_bps: Decimal,
) -> dict:
    return {
        "planned_entry_price": planned_entry,
        "actual_entry_price": actual_entry,
        "trigger_price": trigger,
        "fill_drift_bps": abs(actual_entry - planned_entry) / planned_entry * Decimal("10000"),
        "working_type": final.working_type,
        "baseline_stop_price": baseline.stop_price,
        "baseline_take_profit_price": baseline.take_profit_price,
        "final_stop_price": final.stop_price,
        "final_take_profit_price": final.take_profit_price,
        "breached_triggers": list(breached),
        "trigger_buffer_bps": buffer_bps,
        "source": final.source,
        "trade_plan_id": final.trade_plan_id,
    }


def audit_protections(
    *,
    position: FuturesPosition,
    algo_orders: list[dict],
    risk_manager: FuturesRiskManager,
    rules: SymbolRules,
    working_type: str,
    symbol: str,
    robot_id_prefix: str,
    exit_model=None,
    candles=None,
    planned_stop_price: Decimal | None = None,
    planned_take_profit_price: Decimal | None = None,
    trade_plan_id: str | None = None,
) -> ProtectionAuditResult:
    expected = build_expected_protection(
        position=position,
        risk_manager=risk_manager,
        rules=rules,
        working_type=working_type,
        exit_model=exit_model,
        candles=candles,
        planned_stop_price=planned_stop_price,
        planned_take_profit_price=planned_take_profit_price,
        trade_plan_id=trade_plan_id,
    )

    stop_order = None
    take_order = None
    invalid: list[str] = []

    for order in algo_orders:
        order_type = _order_type(order)

        if not _client_id(order).startswith(robot_id_prefix):
            continue

        order_errors = _validate_common_order(order, expected, symbol)

        if order_type == "STOP_MARKET":
            order_errors.extend(_validate_trigger_price(order, expected.stop_price, "STOP_MARKET"))
            if order_errors:
                invalid.extend(order_errors)
            else:
                stop_order = order

        elif order_type == "TAKE_PROFIT_MARKET":
            order_errors.extend(
                _validate_trigger_price(
                    order,
                    expected.take_profit_price,
                    "TAKE_PROFIT_MARKET",
                )
            )
            if order_errors:
                invalid.extend(order_errors)
            else:
                take_order = order

    missing = []
    if stop_order is None:
        missing.append("STOP_MARKET")

    if take_order is None:
        missing.append("TAKE_PROFIT_MARKET")

    return ProtectionAuditResult(
        ok=not missing and not invalid,
        stop_order=stop_order,
        take_order=take_order,
        missing=missing,
        invalid=invalid,
        expected=expected,
    )


def breached_protection_triggers(
    *,
    position: FuturesPosition,
    expected: ExpectedProtection,
    current_price: Decimal,
) -> list[str]:
    breached = []

    if position.direction == PositionDirection.LONG:
        if current_price <= expected.stop_price:
            breached.append("STOP_MARKET")
        if current_price >= expected.take_profit_price:
            breached.append("TAKE_PROFIT_MARKET")

    elif position.direction == PositionDirection.SHORT:
        if current_price >= expected.stop_price:
            breached.append("STOP_MARKET")
        if current_price <= expected.take_profit_price:
            breached.append("TAKE_PROFIT_MARKET")

    return breached


def normalize_planned_exit_prices(
    *,
    action: TradeAction,
    entry_price: Decimal,
    rules: SymbolRules,
    planned_stop_price: Decimal,
    planned_take_profit_price: Decimal,
) -> tuple[Decimal, Decimal]:
    stop = Decimal(str(planned_stop_price))
    take = Decimal(str(planned_take_profit_price))

    if entry_price <= 0:
        raise ValueError("entry_price precisa ser maior que zero")

    if action == TradeAction.OPEN_LONG:
        stop = rules.round_price_down(stop)
        take = rules.round_price_up(take)
        if stop >= entry_price:
            raise ValueError("planned stop invalido para LONG: precisa ficar abaixo da entrada")
        if take <= entry_price:
            raise ValueError("planned take invalido para LONG: precisa ficar acima da entrada")
        return stop, take

    if action == TradeAction.OPEN_SHORT:
        stop = rules.round_price_up(stop)
        take = rules.round_price_down(take)
        if stop <= entry_price:
            raise ValueError("planned stop invalido para SHORT: precisa ficar acima da entrada")
        if take >= entry_price:
            raise ValueError("planned take invalido para SHORT: precisa ficar abaixo da entrada")
        return stop, take

    raise ValueError(f"Acao invalida para planned exit: {action}")


def _validate_common_order(
    order: dict,
    expected: ExpectedProtection,
    symbol: str,
) -> list[str]:
    errors = []

    if str(order.get("symbol", "")) != symbol:
        errors.append(f"symbol invalido: {order.get('symbol')}")

    if str(order.get("side", "")) != expected.close_side:
        errors.append(f"side invalido: {order.get('side')}")

    if order.get("spotOco"):
        if int(order.get("orderListId", -1)) < 0:
            errors.append("orderListId Spot OCO invalido")
    else:
        if not _is_close_position(order):
            errors.append("closePosition invalido")

        if str(order.get("workingType", "")) != expected.working_type:
            errors.append(f"workingType invalido: {order.get('workingType')}")

    status = str(order.get("algoStatus") or order.get("status") or "NEW")
    if status not in {"NEW", "PARTIALLY_FILLED"}:
        errors.append(f"status invalido: {status}")

    return errors


def _validate_trigger_price(
    order: dict,
    expected_price: Decimal,
    order_type: str,
) -> list[str]:
    trigger_price = _decimal_from_order(order, "triggerPrice")
    if trigger_price is None:
        return [f"{order_type} sem triggerPrice"]

    if trigger_price != expected_price:
        return [
            f"{order_type} triggerPrice invalido: esperado={expected_price}, recebido={trigger_price}"
        ]

    return []


def _order_type(order: dict) -> str:
    return str(order.get("orderType") or order.get("type") or order.get("origType") or "")


def _client_id(order: dict) -> str:
    return str(
        order.get("clientAlgoId") or order.get("clientAlgoID") or order.get("clientOrderId") or ""
    )


def _is_close_position(order: dict) -> bool:
    value = order.get("closePosition")
    if isinstance(value, bool):
        return value

    return str(value).lower() == "true"


def _decimal_from_order(order: dict, key: str) -> Decimal | None:
    value = order.get(key)
    if value is None or value == "":
        return None

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
