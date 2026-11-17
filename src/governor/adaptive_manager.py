from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from domain.models import FuturesPosition, PositionDirection
from futures.state import RuntimeState

ADAPTIVE_MANAGER_VERSION = "1.0.0"

ACTION_NO_POSITION = "NO_POSITION"
ACTION_HOLD = "HOLD"
ACTION_MOVE_STOP_TO_BREAKEVEN_SHADOW = "MOVE_STOP_TO_BREAKEVEN_SHADOW"
ACTION_PROTECT_PROFIT_SHADOW = "PROTECT_PROFIT_SHADOW"
ACTION_REDUCE_RISK_SHADOW = "REDUCE_RISK_SHADOW"
ACTION_TRAILING_STOP_SHADOW = "TRAILING_STOP_SHADOW"
ACTION_PARTIAL_REDUCTION_SHADOW = "PARTIAL_REDUCTION_SHADOW"
ACTION_EXIT_POSITION_SHADOW = "EXIT_POSITION_SHADOW"

MANAGEMENT_MODE_HOLD = "HOLD"
MANAGEMENT_MODE_BREAKEVEN = "BREAKEVEN"
MANAGEMENT_MODE_PROTECT_PROFIT = "PROTECT_PROFIT"
MANAGEMENT_MODE_TRAILING_STOP = "TRAILING_STOP"
MANAGEMENT_MODE_REDUCE_RISK = "REDUCE_RISK"
MANAGEMENT_MODE_PARTIAL_REDUCTION = "PARTIAL_REDUCTION"
MANAGEMENT_MODE_EXIT_POSITION = "EXIT_POSITION"


@dataclass(frozen=True)
class AdaptiveManagementEvaluation:
    symbol: str
    action: str
    reason: str
    confidence: int
    current_price: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    pnl_pct: Decimal
    suggested_stop_price: Decimal | None = None
    suggested_take_profit_price: Decimal | None = None
    trailing_stop_price: Decimal | None = None
    suggested_reduction_fraction: Decimal | None = None
    suggested_exit_fraction: Decimal | None = None
    should_reduce_position: bool = False
    should_exit_position: bool = False
    deterioration_score: Decimal | None = None
    management_mode: str = MANAGEMENT_MODE_HOLD
    trade_plan_id: str | None = None
    source: str = "SHADOW"
    manager_version: str = ADAPTIVE_MANAGER_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class AdaptivePositionManager:
    def __init__(
        self,
        *,
        breakeven_trigger_pct: Decimal = Decimal("0.003"),
        protect_profit_progress: Decimal = Decimal("0.75"),
        risk_warning_pct: Decimal = Decimal("0.006"),
        trailing_activation_pct: Decimal = Decimal("0.006"),
        trailing_distance_pct: Decimal = Decimal("0.003"),
        partial_reduction_warning_pct: Decimal = Decimal("0.008"),
        severe_deterioration_pct: Decimal = Decimal("0.012"),
        partial_reduction_fraction: Decimal = Decimal("0.5"),
    ):
        self.breakeven_trigger_pct = breakeven_trigger_pct
        self.protect_profit_progress = protect_profit_progress
        self.risk_warning_pct = risk_warning_pct
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_distance_pct = trailing_distance_pct
        self.partial_reduction_warning_pct = partial_reduction_warning_pct
        self.severe_deterioration_pct = severe_deterioration_pct
        self.partial_reduction_fraction = partial_reduction_fraction

    def evaluate(
        self,
        *,
        position: FuturesPosition,
        current_price: Decimal,
        runtime_state: RuntimeState | None = None,
    ) -> AdaptiveManagementEvaluation:
        current_price = Decimal(str(current_price))

        if not position.is_open:
            return AdaptiveManagementEvaluation(
                symbol=position.symbol,
                action=ACTION_NO_POSITION,
                reason="POSITION_NOT_OPEN",
                confidence=0,
                current_price=current_price,
                entry_price=position.entry_price,
                unrealized_pnl=position.unrealized_pnl,
                pnl_pct=Decimal("0"),
                management_mode=MANAGEMENT_MODE_HOLD,
                diagnostics={"manager_active": True},
            )

        if position.entry_price <= 0 or current_price <= 0:
            return AdaptiveManagementEvaluation(
                symbol=position.symbol,
                action=ACTION_HOLD,
                reason="INVALID_PRICE_CONTEXT",
                confidence=0,
                current_price=current_price,
                entry_price=position.entry_price,
                unrealized_pnl=position.unrealized_pnl,
                pnl_pct=Decimal("0"),
                trade_plan_id=_runtime_trade_plan_id(runtime_state),
                management_mode=MANAGEMENT_MODE_HOLD,
                diagnostics={
                    "manager_active": True,
                    "invalid_price_context": True,
                },
            )

        pnl_pct = _pnl_pct(position, current_price)
        planned_stop = _runtime_planned_stop(runtime_state)
        planned_take = _runtime_planned_take(runtime_state)
        progress_to_take = _progress_to_take(position, current_price, planned_take)
        distance_to_stop = _distance_to_stop_pct(position, current_price, planned_stop)
        deterioration_score = _deterioration_score(
            pnl_pct=pnl_pct,
            severe_deterioration_pct=self.severe_deterioration_pct,
        )

        diagnostics = {
            "manager_active": True,
            "position_direction": position.direction.value,
            "protection_source": (
                runtime_state.protection_source if runtime_state is not None else None
            ),
            "trade_plan_id": _runtime_trade_plan_id(runtime_state),
            "planned_stop_price": str(planned_stop) if planned_stop is not None else None,
            "planned_take_profit_price": (str(planned_take) if planned_take is not None else None),
            "progress_to_take": (str(progress_to_take) if progress_to_take is not None else None),
            "distance_to_stop_pct": (
                str(distance_to_stop) if distance_to_stop is not None else None
            ),
            "pnl_pct": str(pnl_pct),
            "deterioration_score": (
                str(deterioration_score) if deterioration_score is not None else None
            ),
            "breakeven_trigger_pct": str(self.breakeven_trigger_pct),
            "protect_profit_progress": str(self.protect_profit_progress),
            "risk_warning_pct": str(self.risk_warning_pct),
            "trailing_activation_pct": str(self.trailing_activation_pct),
            "trailing_distance_pct": str(self.trailing_distance_pct),
            "partial_reduction_warning_pct": str(self.partial_reduction_warning_pct),
            "severe_deterioration_pct": str(self.severe_deterioration_pct),
            "partial_reduction_fraction": str(self.partial_reduction_fraction),
            "shadow_only": True,
        }

        if planned_stop is None or planned_take is None:
            return AdaptiveManagementEvaluation(
                symbol=position.symbol,
                action=ACTION_HOLD,
                reason="PLANNED_PROTECTION_NOT_AVAILABLE",
                confidence=35,
                current_price=current_price,
                entry_price=position.entry_price,
                unrealized_pnl=position.unrealized_pnl,
                pnl_pct=pnl_pct,
                trade_plan_id=_runtime_trade_plan_id(runtime_state),
                deterioration_score=deterioration_score,
                management_mode=MANAGEMENT_MODE_HOLD,
                diagnostics=diagnostics,
            )

        if pnl_pct <= -self.severe_deterioration_pct:
            return AdaptiveManagementEvaluation(
                symbol=position.symbol,
                action=ACTION_EXIT_POSITION_SHADOW,
                reason="POSITION_SEVERE_DETERIORATION_SHADOW",
                confidence=90,
                current_price=current_price,
                entry_price=position.entry_price,
                unrealized_pnl=position.unrealized_pnl,
                pnl_pct=pnl_pct,
                suggested_stop_price=planned_stop,
                suggested_take_profit_price=planned_take,
                suggested_reduction_fraction=Decimal("1"),
                suggested_exit_fraction=Decimal("1"),
                should_reduce_position=True,
                should_exit_position=True,
                deterioration_score=deterioration_score,
                management_mode=MANAGEMENT_MODE_EXIT_POSITION,
                trade_plan_id=_runtime_trade_plan_id(runtime_state),
                diagnostics=diagnostics,
            )

        if pnl_pct <= -self.partial_reduction_warning_pct:
            return AdaptiveManagementEvaluation(
                symbol=position.symbol,
                action=ACTION_PARTIAL_REDUCTION_SHADOW,
                reason="POSITION_DETERIORATING_PARTIAL_REDUCTION_SHADOW",
                confidence=82,
                current_price=current_price,
                entry_price=position.entry_price,
                unrealized_pnl=position.unrealized_pnl,
                pnl_pct=pnl_pct,
                suggested_stop_price=planned_stop,
                suggested_take_profit_price=planned_take,
                suggested_reduction_fraction=self.partial_reduction_fraction,
                should_reduce_position=True,
                should_exit_position=False,
                deterioration_score=deterioration_score,
                management_mode=MANAGEMENT_MODE_PARTIAL_REDUCTION,
                trade_plan_id=_runtime_trade_plan_id(runtime_state),
                diagnostics=diagnostics,
            )

        if pnl_pct <= -self.risk_warning_pct:
            return AdaptiveManagementEvaluation(
                symbol=position.symbol,
                action=ACTION_REDUCE_RISK_SHADOW,
                reason="POSITION_MOVING_AGAINST_PLAN",
                confidence=70,
                current_price=current_price,
                entry_price=position.entry_price,
                unrealized_pnl=position.unrealized_pnl,
                pnl_pct=pnl_pct,
                suggested_stop_price=planned_stop,
                suggested_take_profit_price=planned_take,
                deterioration_score=deterioration_score,
                management_mode=MANAGEMENT_MODE_REDUCE_RISK,
                trade_plan_id=_runtime_trade_plan_id(runtime_state),
                diagnostics=diagnostics,
            )

        if progress_to_take is not None and progress_to_take >= self.protect_profit_progress:
            return AdaptiveManagementEvaluation(
                symbol=position.symbol,
                action=ACTION_PROTECT_PROFIT_SHADOW,
                reason="PRICE_NEAR_TAKE_PROFIT",
                confidence=80,
                current_price=current_price,
                entry_price=position.entry_price,
                unrealized_pnl=position.unrealized_pnl,
                pnl_pct=pnl_pct,
                suggested_stop_price=_half_profit_stop(position, current_price),
                suggested_take_profit_price=planned_take,
                deterioration_score=deterioration_score,
                management_mode=MANAGEMENT_MODE_PROTECT_PROFIT,
                trade_plan_id=_runtime_trade_plan_id(runtime_state),
                diagnostics=diagnostics,
            )

        trailing_stop = _trailing_stop(
            position=position,
            current_price=current_price,
            trailing_distance_pct=self.trailing_distance_pct,
        )
        if (
            pnl_pct >= self.trailing_activation_pct
            and trailing_stop is not None
            and _suggested_stop_improves(
                position=position,
                current_price=current_price,
                current_stop_price=planned_stop,
                suggested_stop_price=trailing_stop,
            )
        ):
            return AdaptiveManagementEvaluation(
                symbol=position.symbol,
                action=ACTION_TRAILING_STOP_SHADOW,
                reason="TRAILING_STOP_CANDIDATE_SHADOW",
                confidence=78,
                current_price=current_price,
                entry_price=position.entry_price,
                unrealized_pnl=position.unrealized_pnl,
                pnl_pct=pnl_pct,
                suggested_stop_price=trailing_stop,
                suggested_take_profit_price=planned_take,
                trailing_stop_price=trailing_stop,
                deterioration_score=deterioration_score,
                management_mode=MANAGEMENT_MODE_TRAILING_STOP,
                trade_plan_id=_runtime_trade_plan_id(runtime_state),
                diagnostics=diagnostics,
            )

        if pnl_pct >= self.breakeven_trigger_pct and _stop_before_breakeven(
            position,
            planned_stop,
        ):
            return AdaptiveManagementEvaluation(
                symbol=position.symbol,
                action=ACTION_MOVE_STOP_TO_BREAKEVEN_SHADOW,
                reason="BREAKEVEN_TRIGGER_REACHED",
                confidence=75,
                current_price=current_price,
                entry_price=position.entry_price,
                unrealized_pnl=position.unrealized_pnl,
                pnl_pct=pnl_pct,
                suggested_stop_price=position.entry_price,
                suggested_take_profit_price=planned_take,
                deterioration_score=deterioration_score,
                management_mode=MANAGEMENT_MODE_BREAKEVEN,
                trade_plan_id=_runtime_trade_plan_id(runtime_state),
                diagnostics=diagnostics,
            )

        return AdaptiveManagementEvaluation(
            symbol=position.symbol,
            action=ACTION_HOLD,
            reason="NO_ADAPTIVE_CHANGE_NEEDED",
            confidence=60,
            current_price=current_price,
            entry_price=position.entry_price,
            unrealized_pnl=position.unrealized_pnl,
            pnl_pct=pnl_pct,
            suggested_stop_price=planned_stop,
            suggested_take_profit_price=planned_take,
            deterioration_score=deterioration_score,
            management_mode=MANAGEMENT_MODE_HOLD,
            trade_plan_id=_runtime_trade_plan_id(runtime_state),
            diagnostics=diagnostics,
        )


def _trailing_stop(
    *,
    position: FuturesPosition,
    current_price: Decimal,
    trailing_distance_pct: Decimal,
) -> Decimal | None:
    if current_price <= 0:
        return None

    if position.direction == PositionDirection.LONG:
        return current_price * (Decimal("1") - trailing_distance_pct)

    if position.direction == PositionDirection.SHORT:
        return current_price * (Decimal("1") + trailing_distance_pct)

    return None


def _suggested_stop_improves(
    *,
    position: FuturesPosition,
    current_price: Decimal,
    current_stop_price: Decimal,
    suggested_stop_price: Decimal,
) -> bool:
    if current_price <= 0:
        return False

    if position.direction == PositionDirection.LONG:
        if suggested_stop_price <= current_stop_price:
            return False
        if suggested_stop_price >= current_price:
            return False
        return True

    if position.direction == PositionDirection.SHORT:
        if suggested_stop_price >= current_stop_price:
            return False
        if suggested_stop_price <= current_price:
            return False
        return True

    return False


def _deterioration_score(
    *,
    pnl_pct: Decimal,
    severe_deterioration_pct: Decimal,
) -> Decimal:
    if pnl_pct >= 0:
        return Decimal("0")

    if severe_deterioration_pct <= 0:
        return Decimal("0")

    return _clamp(
        abs(pnl_pct) / severe_deterioration_pct,
        Decimal("0"),
        Decimal("1"),
    )


def _runtime_trade_plan_id(runtime_state: RuntimeState | None) -> str | None:
    return runtime_state.trade_plan_id if runtime_state is not None else None


def _runtime_planned_stop(runtime_state: RuntimeState | None) -> Decimal | None:
    return runtime_state.planned_stop_price if runtime_state is not None else None


def _runtime_planned_take(runtime_state: RuntimeState | None) -> Decimal | None:
    return runtime_state.planned_take_profit_price if runtime_state is not None else None


def _pnl_pct(position: FuturesPosition, current_price: Decimal) -> Decimal:
    if position.direction == PositionDirection.LONG:
        return (current_price - position.entry_price) / position.entry_price

    if position.direction == PositionDirection.SHORT:
        return (position.entry_price - current_price) / position.entry_price

    return Decimal("0")


def _progress_to_take(
    position: FuturesPosition,
    current_price: Decimal,
    take_profit_price: Decimal | None,
) -> Decimal | None:
    if take_profit_price is None:
        return None

    if position.direction == PositionDirection.LONG:
        total = take_profit_price - position.entry_price
        current = current_price - position.entry_price
    elif position.direction == PositionDirection.SHORT:
        total = position.entry_price - take_profit_price
        current = position.entry_price - current_price
    else:
        return None

    if total <= 0:
        return None

    return _clamp(current / total, Decimal("0"), Decimal("1"))


def _distance_to_stop_pct(
    position: FuturesPosition,
    current_price: Decimal,
    stop_price: Decimal | None,
) -> Decimal | None:
    if stop_price is None or current_price <= 0:
        return None

    if position.direction == PositionDirection.LONG:
        return (current_price - stop_price) / current_price

    if position.direction == PositionDirection.SHORT:
        return (stop_price - current_price) / current_price

    return None


def _stop_before_breakeven(
    position: FuturesPosition,
    stop_price: Decimal,
) -> bool:
    if position.direction == PositionDirection.LONG:
        return stop_price < position.entry_price

    if position.direction == PositionDirection.SHORT:
        return stop_price > position.entry_price

    return False


def _half_profit_stop(
    position: FuturesPosition,
    current_price: Decimal,
) -> Decimal:
    if position.direction == PositionDirection.LONG:
        return position.entry_price + ((current_price - position.entry_price) / Decimal("2"))

    if position.direction == PositionDirection.SHORT:
        return position.entry_price - ((position.entry_price - current_price) / Decimal("2"))

    return position.entry_price


def _clamp(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    return max(minimum, min(maximum, value))
