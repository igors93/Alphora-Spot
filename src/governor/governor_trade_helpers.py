import re
from decimal import Decimal, InvalidOperation
from typing import Any

from config.symbol_context import SymbolTradingConfig
from domain.models import FuturesPosition, PositionDirection
from governor.event_payloads import planned_exit_stop, planned_exit_take
from governor.models import SymbolRuntimeStatus
from scanner.signal_score import SignalPriority

EXECUTION_PLAN_ALLOWED_STATUSES = {
    "ALLOW_MIN_SIZE",
    "ALLOW_REDUCED",
    "ALLOW_NORMAL",
    "ALLOW_AGGRESSIVE",
}
EXECUTION_OPEN_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}
INTELLIGENT_FINAL_STATUS_READY_FOR_EXECUTION = "READY_FOR_EXECUTION"
INTELLIGENT_FINAL_STATUS_BLOCKED_BY_GOVERNOR = "BLOCKED_BY_GOVERNOR"
INTELLIGENT_FINAL_STATUS_BLOCKED_BY_LEARNING_PAPER = "BLOCKED_BY_LEARNING_PAPER"
INTELLIGENT_FINAL_STATUS_BLOCKED_BY_LEARNING_TESTNET = "BLOCKED_BY_LEARNING_TESTNET"
INTELLIGENT_FINAL_STATUS_WAITING_BY_EXECUTION_PLANNER = "WAITING_BY_EXECUTION_PLANNER"


def _ai_signal_audit_fields(
    score,
    *,
    final_action: str | None = None,
    block_reason: str | None = None,
) -> dict[str, Any]:
    metadata = getattr(score, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        return {}

    ai_keys = {
        "model_source",
        "model_path",
        "model_id",
        "model_type",
        "long_score",
        "short_score",
        "p_long_event",
        "p_short_event",
        "p_neutral_event",
        "expected_move_bps",
        "expected_value_bps",
        "confidence",
        "fallback_used",
        "fallback_reason",
        "action_candidate",
        "direction_candidate",
    }
    if metadata.get("source") not in {"AI", "AI_PROBABILISTIC"} and not ai_keys.intersection(
        metadata
    ):
        return {}

    payload = {
        "ai_source": str(metadata.get("source", "")),
        "model_type": str(metadata.get("model_type", "")),
        "model_id": str(metadata.get("model_id", "")),
        "model_source": str(metadata.get("model_source", "")),
        "model_path": str(metadata.get("model_path", "")),
        "long_score": metadata.get("long_score"),
        "short_score": metadata.get("short_score"),
        "p_long_event": metadata.get("p_long_event"),
        "p_short_event": metadata.get("p_short_event"),
        "p_neutral_event": metadata.get("p_neutral_event"),
        "expected_move_bps": metadata.get("expected_move_bps"),
        "expected_value_bps": metadata.get("expected_value_bps"),
        "confidence": metadata.get("confidence"),
        "fallback_used": metadata.get("fallback_used"),
        "fallback_reason": str(metadata.get("fallback_reason", "")),
        "action_candidate": str(metadata.get("action_candidate", "")),
        "direction_candidate": str(metadata.get("direction_candidate", "")),
    }
    if final_action is not None:
        payload["governor_final_action"] = str(final_action)
    if block_reason is not None:
        payload["governor_block_reason"] = str(block_reason)
    return payload


def _status_from_score(priority: SignalPriority, is_open: bool) -> SymbolRuntimeStatus:
    if is_open:
        return SymbolRuntimeStatus.IN_POSITION
    if priority is SignalPriority.READY:
        return SymbolRuntimeStatus.READY
    if priority is SignalPriority.HOT:
        return SymbolRuntimeStatus.HOT_CHECK
    if priority is SignalPriority.WARM:
        return SymbolRuntimeStatus.WARM
    return SymbolRuntimeStatus.COLD


def _is_no_action_result(reason: str) -> bool:
    return reason in {
        "STRATEGY_HOLD",
        "CLOSE_WITHOUT_POSITION",
        "UNSUPPORTED_STRATEGY_ACTION",
    }


def _economic_adjusted_exit_for_execution(trade_pipeline_result, *, action: str):
    if trade_pipeline_result is None:
        return None

    plan = trade_pipeline_result.plan
    if action not in EXECUTION_OPEN_ACTIONS:
        return None
    if plan.action != action:
        return None
    if plan.status not in EXECUTION_PLAN_ALLOWED_STATUSES:
        return None
    if plan.stop_price is None or plan.take_profit_price is None:
        return None

    diagnostics = plan.diagnostics if isinstance(plan.diagnostics, dict) else {}
    exit_diagnostics = diagnostics.get("exit", {})
    if not isinstance(exit_diagnostics, dict):
        return None
    if exit_diagnostics.get("economic_exit_adjusted") is not True:
        return None

    return {
        "stop_price": plan.stop_price,
        "take_profit_price": plan.take_profit_price,
    }


def _merge_planned_exit_candidate(
    current_planned_exit: Any,
    *,
    stop_price: Any,
    take_profit_price: Any,
) -> dict[str, Any] | None:
    current_stop = planned_exit_stop(current_planned_exit)
    current_take = planned_exit_take(current_planned_exit)
    final_stop = Decimal(str(stop_price)) if stop_price is not None else current_stop
    final_take = Decimal(str(take_profit_price)) if take_profit_price is not None else current_take
    if final_stop is None and final_take is None:
        return None
    payload = dict(current_planned_exit) if isinstance(current_planned_exit, dict) else {}
    payload["stop_price"] = final_stop
    payload["take_profit_price"] = final_take
    return payload


def _runtime_state_position(symbol: str, runtime_state) -> FuturesPosition:
    if runtime_state is None:
        return FuturesPosition(
            symbol=symbol,
            direction=PositionDirection.NONE,
            quantity=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        )
    return FuturesPosition(
        symbol=symbol,
        direction=getattr(runtime_state, "direction", PositionDirection.NONE),
        quantity=Decimal(str(getattr(runtime_state, "quantity", "0") or "0")),
        entry_price=Decimal(str(getattr(runtime_state, "entry_price", "0") or "0")),
        unrealized_pnl=Decimal("0"),
        opened_at=getattr(runtime_state, "opened_at", None),
    )


def _robot_orders_for_symbol(
    symbol_config: SymbolTradingConfig,
    *,
    normal_orders: list[dict[str, Any]],
    algo_orders: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prefix = str(symbol_config.bot_order_prefix or "ALPHORA").upper().strip()
    normalized_prefix = re.sub(r"[^A-Z0-9_]+", "_", prefix).strip("_") or "ALPHORA"
    order_prefix = f"{normalized_prefix}_"
    robot_normal = [
        order
        for order in normal_orders
        if _normal_order_client_id(order).upper().startswith(order_prefix)
    ]
    robot_algo = [
        order
        for order in algo_orders
        if _algo_order_client_id(order).upper().startswith(order_prefix)
    ]
    return robot_normal, robot_algo


def _daily_consecutive_losses(account_snapshot: dict[str, Any] | None) -> int:
    if not isinstance(account_snapshot, dict):
        return 0
    for key in ("consecutive_losses", "daily_consecutive_losses"):
        value = account_snapshot.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
    return 0


def _normal_order_client_id(order: dict[str, Any]) -> str:
    return str(order.get("clientOrderId") or order.get("origClientOrderId") or "")


def _algo_order_client_id(order: dict[str, Any]) -> str:
    return str(
        order.get("clientAlgoId") or order.get("clientAlgoID") or order.get("clientOrderId") or ""
    )


def _decimal_or_none(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _price_distance_bps(price, entry_price) -> Decimal | None:
    price = _decimal_or_none(price)
    entry_price = _decimal_or_none(entry_price)
    if price is None or entry_price is None or entry_price <= 0:
        return None
    return abs(price - entry_price) / entry_price * Decimal("10000")


def _exit_diagnostics_from_plan(plan) -> dict[str, Any]:
    if plan is None or not isinstance(getattr(plan, "diagnostics", None), dict):
        return {}
    diagnostics = plan.diagnostics
    exit_diagnostics = diagnostics.get("exit_diagnostics")
    if isinstance(exit_diagnostics, dict):
        return exit_diagnostics
    exit_diagnostics = diagnostics.get("exit")
    if isinstance(exit_diagnostics, dict):
        return exit_diagnostics
    return {}


def _intelligent_activation_context(activation_decision) -> dict[str, Any]:
    if activation_decision is None:
        return {}
    return {
        "status": getattr(activation_decision, "status", None),
        "activation_action": getattr(activation_decision, "action", None),
        "reason": getattr(activation_decision, "reason", None),
        "decision_id": getattr(activation_decision, "decision_id", None),
        "plan_id": getattr(activation_decision, "plan_id", None),
        "trade_plan_id": getattr(activation_decision, "trade_plan_id", None),
        "exit_source": getattr(activation_decision, "exit_source", None),
        "used_adaptive_exit": getattr(activation_decision, "used_adaptive_exit", None),
        "adaptive_exit_applied": getattr(
            activation_decision,
            "adaptive_exit_applied",
            None,
        ),
        "recommendation_mode": getattr(
            activation_decision,
            "recommendation_mode",
            None,
        ),
        "protection_quality": getattr(
            activation_decision,
            "protection_quality",
            None,
        ),
        "diagnostics": getattr(activation_decision, "diagnostics", {}),
    }


def _seconds_since_open(position, now: float) -> float | None:
    opened_at = getattr(position, "opened_at", None)
    is_open = bool(getattr(position, "is_open", False))
    if not is_open or opened_at is None:
        return None
    try:
        return max(0.0, float(now) - float(opened_at))
    except (TypeError, ValueError):
        return None


def _close_source_from_intent(intent) -> str:
    metadata = getattr(intent, "metadata", {}) or {}
    if hasattr(metadata, "get"):
        explicit_source = metadata.get("close_source") or metadata.get("source")
        if explicit_source:
            return str(explicit_source).upper().strip()

    strategy_name = str(getattr(intent, "strategy_name", "") or "").strip()
    if strategy_name and strategy_name != "unknown_strategy":
        return "STRATEGY"
    reason = str(getattr(intent, "reason", "") or "").upper()
    if "ADAPTIVE" in reason:
        return "ADAPTIVE_MANAGER"
    if "RECOVER" in reason or "RECOVERY" in reason:
        return "RECOVERY"
    if "RISK" in reason or "RISCO" in reason:
        return "GOVERNOR_RISK"
    if "LEARNING" in reason:
        return "LEARNING"
    return "UNKNOWN"


def _component_fallback_reason_from_plan(assisted_plan, component: str) -> str:
    details = getattr(assisted_plan, "details", {}) or {}
    if not isinstance(details, dict):
        return ""

    component_reasons = details.get("component_fallback_reasons", {}) or {}
    if not isinstance(component_reasons, dict):
        return ""

    return str(component_reasons.get(component, "") or "").upper().strip()


def _metadata_float(metadata: dict[str, Any], key: str) -> float | None:
    value = metadata.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_new_entry_action(action: str | None) -> bool:
    normalized = str(action or "").upper().strip()
    if not normalized:
        return False

    if normalized in {
        "BUY",
        "SELL",
        "LONG",
        "SHORT",
        "OPEN_LONG",
        "OPEN_SHORT",
        "ENTER_LONG",
        "ENTER_SHORT",
    }:
        return True

    return normalized.startswith("OPEN_") or normalized.startswith("ENTER_")


def _notional_from_planned_quantity(planned_quantity: Any, intent: Any) -> Decimal | None:
    """Return notional derived from the final planned quantity, or None to use the fallback."""
    if planned_quantity is None:
        return None
    try:
        price = Decimal(str(getattr(intent, "market_price", None) or 0))
        qty = Decimal(str(planned_quantity))
        if price > 0 and qty > 0:
            return abs(qty * price)
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return None
    return None
