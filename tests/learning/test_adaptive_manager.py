from decimal import Decimal

from domain.models import FuturesPosition, PositionDirection
from futures.state import RuntimeState
from governor.adaptive_manager import (
    ACTION_EXIT_POSITION_SHADOW,
    ACTION_HOLD,
    ACTION_MOVE_STOP_TO_BREAKEVEN_SHADOW,
    ACTION_PARTIAL_REDUCTION_SHADOW,
    ACTION_PROTECT_PROFIT_SHADOW,
    ACTION_REDUCE_RISK_SHADOW,
    ACTION_TRAILING_STOP_SHADOW,
    MANAGEMENT_MODE_EXIT_POSITION,
    MANAGEMENT_MODE_PARTIAL_REDUCTION,
    MANAGEMENT_MODE_TRAILING_STOP,
    AdaptivePositionManager,
)


def _long_position() -> FuturesPosition:
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )


def _runtime_state() -> RuntimeState:
    return RuntimeState(
        active_trade_id="trade",
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.1"),
        protection_source="TRADE_PLAN",
        trade_plan_id="tp_test",
        planned_stop_price=Decimal("99"),
        planned_take_profit_price=Decimal("104"),
    )


def _runtime_state_with_stop(
    *,
    stop: Decimal,
    take: Decimal = Decimal("104"),
) -> RuntimeState:
    return RuntimeState(
        active_trade_id="trade",
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.1"),
        protection_source="TRADE_PLAN",
        trade_plan_id="tp_test",
        planned_stop_price=stop,
        planned_take_profit_price=take,
    )


def test_adaptive_manager_suggests_breakeven_after_profit_trigger():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("100.50"),
        runtime_state=_runtime_state(),
    )

    assert evaluation.action == ACTION_MOVE_STOP_TO_BREAKEVEN_SHADOW
    assert evaluation.reason == "BREAKEVEN_TRIGGER_REACHED"
    assert evaluation.suggested_stop_price == Decimal("100")
    assert evaluation.trade_plan_id == "tp_test"
    assert evaluation.diagnostics["shadow_only"] is True


def test_adaptive_manager_suggests_profit_protection_near_take():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("103.20"),
        runtime_state=_runtime_state(),
    )

    assert evaluation.action == ACTION_PROTECT_PROFIT_SHADOW
    assert evaluation.reason == "PRICE_NEAR_TAKE_PROFIT"
    assert evaluation.suggested_stop_price > Decimal("100")
    assert evaluation.suggested_take_profit_price == Decimal("104")


def test_adaptive_manager_suggests_reduce_risk_when_position_moves_against_plan():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("99.30"),
        runtime_state=_runtime_state(),
    )

    assert evaluation.action == ACTION_REDUCE_RISK_SHADOW
    assert evaluation.reason == "POSITION_MOVING_AGAINST_PLAN"
    assert evaluation.suggested_stop_price == Decimal("99")


def test_adaptive_manager_holds_when_runtime_protection_is_missing():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("100.50"),
        runtime_state=None,
    )

    assert evaluation.action == ACTION_HOLD
    assert evaluation.reason == "PLANNED_PROTECTION_NOT_AVAILABLE"
    assert evaluation.diagnostics["planned_stop_price"] is None


def test_adaptive_manager_suggests_trailing_stop_when_profit_expands():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("101.50"),
        runtime_state=_runtime_state_with_stop(stop=Decimal("100.20")),
    )

    assert evaluation.action == ACTION_TRAILING_STOP_SHADOW
    assert evaluation.reason == "TRAILING_STOP_CANDIDATE_SHADOW"
    assert evaluation.suggested_stop_price > Decimal("100.20")
    assert evaluation.suggested_stop_price < Decimal("101.50")
    assert evaluation.trailing_stop_price == evaluation.suggested_stop_price
    assert evaluation.management_mode == MANAGEMENT_MODE_TRAILING_STOP
    assert evaluation.should_reduce_position is False
    assert evaluation.should_exit_position is False


def test_adaptive_manager_suggests_partial_reduction_when_trade_deteriorates():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("99.10"),
        runtime_state=_runtime_state(),
    )

    assert evaluation.action == ACTION_PARTIAL_REDUCTION_SHADOW
    assert evaluation.reason == "POSITION_DETERIORATING_PARTIAL_REDUCTION_SHADOW"
    assert evaluation.suggested_reduction_fraction == Decimal("0.5")
    assert evaluation.should_reduce_position is True
    assert evaluation.should_exit_position is False
    assert evaluation.management_mode == MANAGEMENT_MODE_PARTIAL_REDUCTION
    assert evaluation.deterioration_score is not None


def test_adaptive_manager_suggests_exit_when_trade_deteriorates_severely():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("98.70"),
        runtime_state=_runtime_state(),
    )

    assert evaluation.action == ACTION_EXIT_POSITION_SHADOW
    assert evaluation.reason == "POSITION_SEVERE_DETERIORATION_SHADOW"
    assert evaluation.suggested_reduction_fraction == Decimal("1")
    assert evaluation.suggested_exit_fraction == Decimal("1")
    assert evaluation.should_reduce_position is True
    assert evaluation.should_exit_position is True
    assert evaluation.management_mode == MANAGEMENT_MODE_EXIT_POSITION
    assert evaluation.deterioration_score == Decimal("1")
