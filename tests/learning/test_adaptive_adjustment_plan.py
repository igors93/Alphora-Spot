from decimal import Decimal

from domain.models import FuturesPosition, PositionDirection
from futures.state import RuntimeState
from governor.adaptive_adjustment_plan import (
    PLAN_ACTION_EXIT_POSITION_REVIEW,
    PLAN_ACTION_MOVE_STOP_TO_BREAKEVEN,
    PLAN_ACTION_NONE,
    PLAN_ACTION_PARTIAL_REDUCTION_REVIEW,
    PLAN_ACTION_PROTECT_PROFIT,
    PLAN_ACTION_TRAILING_STOP,
    PLAN_STATUS_BLOCKED_MISSING_RUNTIME_STATE,
    PLAN_STATUS_BLOCKED_TARGET_NOT_IMPROVING,
    PLAN_STATUS_HOLD,
    PLAN_STATUS_SHADOW_READY,
    PLAN_STATUS_SHADOW_REVIEW_REQUIRED,
    AdaptiveAdjustmentPlanner,
)
from governor.adaptive_manager import (
    ACTION_EXIT_POSITION_SHADOW,
    ACTION_MOVE_STOP_TO_BREAKEVEN_SHADOW,
    ACTION_PARTIAL_REDUCTION_SHADOW,
    ACTION_TRAILING_STOP_SHADOW,
    AdaptiveManagementEvaluation,
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


def test_adjustment_plan_converts_breakeven_evaluation_to_shadow_plan():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("100.50"),
        runtime_state=_runtime_state(),
    )

    plan = AdaptiveAdjustmentPlanner().build_shadow_plan(
        evaluation=evaluation,
        runtime_state=_runtime_state(),
    )

    assert plan.action == PLAN_ACTION_MOVE_STOP_TO_BREAKEVEN
    assert plan.status == PLAN_STATUS_SHADOW_READY
    assert plan.target_stop_price == Decimal("100")
    assert plan.target_take_profit_price == Decimal("104")
    assert plan.trade_plan_id == "tp_test"
    assert plan.requires_order_change is True
    assert plan.execution_enabled is False
    assert plan.shadow_only is True
    assert plan.plan_id.startswith("adj_")


def test_adjustment_plan_converts_profit_protection_to_shadow_plan():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("103.20"),
        runtime_state=_runtime_state(),
    )

    plan = AdaptiveAdjustmentPlanner().build_shadow_plan(
        evaluation=evaluation,
        runtime_state=_runtime_state(),
    )

    assert plan.action == PLAN_ACTION_PROTECT_PROFIT
    assert plan.status == PLAN_STATUS_SHADOW_READY
    assert plan.target_stop_price > Decimal("100")
    assert plan.target_take_profit_price == Decimal("104")
    assert plan.requires_order_change is True
    assert plan.execution_enabled is False


def test_adjustment_plan_holds_without_runtime_state():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("100.50"),
        runtime_state=None,
    )

    plan = AdaptiveAdjustmentPlanner().build_shadow_plan(
        evaluation=evaluation,
        runtime_state=None,
    )

    assert plan.action == PLAN_ACTION_NONE
    assert plan.status == PLAN_STATUS_BLOCKED_MISSING_RUNTIME_STATE
    assert plan.requires_order_change is False
    assert plan.execution_enabled is False
    assert plan.shadow_only is True


def test_adjustment_plan_holds_when_evaluation_holds_with_runtime_state():
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("100.10"),
        runtime_state=_runtime_state(),
    )

    plan = AdaptiveAdjustmentPlanner().build_shadow_plan(
        evaluation=evaluation,
        runtime_state=_runtime_state(),
    )

    assert plan.action == PLAN_ACTION_NONE
    assert plan.status == PLAN_STATUS_HOLD
    assert plan.requires_order_change is False
    assert plan.execution_enabled is False


def test_adjustment_plan_blocks_target_stop_that_does_not_improve_protection():
    state = RuntimeState(
        active_trade_id="trade",
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.1"),
        protection_source="TRADE_PLAN",
        trade_plan_id="tp_test",
        planned_stop_price=Decimal("100.20"),
        planned_take_profit_price=Decimal("104"),
    )
    evaluation = AdaptiveManagementEvaluation(
        symbol="BTCUSDT",
        action=ACTION_MOVE_STOP_TO_BREAKEVEN_SHADOW,
        reason="BREAKEVEN_TRIGGER_REACHED",
        confidence=75,
        current_price=Decimal("100.50"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        pnl_pct=Decimal("0.005"),
        suggested_stop_price=Decimal("100"),
        suggested_take_profit_price=Decimal("104"),
        trade_plan_id="tp_test",
    )

    plan = AdaptiveAdjustmentPlanner().build_shadow_plan(
        evaluation=evaluation,
        runtime_state=state,
        execution_enabled=True,
    )

    assert plan.status == PLAN_STATUS_BLOCKED_TARGET_NOT_IMPROVING
    assert plan.requires_order_change is False
    assert plan.execution_enabled is False


def test_adjustment_plan_converts_trailing_stop_to_shadow_plan():
    state = RuntimeState(
        active_trade_id="trade",
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.1"),
        protection_source="TRADE_PLAN",
        trade_plan_id="tp_test",
        planned_stop_price=Decimal("100.20"),
        planned_take_profit_price=Decimal("104"),
    )
    evaluation = AdaptivePositionManager().evaluate(
        position=_long_position(),
        current_price=Decimal("101.50"),
        runtime_state=state,
    )

    plan = AdaptiveAdjustmentPlanner().build_shadow_plan(
        evaluation=evaluation,
        runtime_state=state,
    )

    assert evaluation.action == ACTION_TRAILING_STOP_SHADOW
    assert plan.action == PLAN_ACTION_TRAILING_STOP
    assert plan.status == PLAN_STATUS_SHADOW_READY
    assert plan.target_stop_price > Decimal("100.20")
    assert plan.trailing_stop_price == plan.target_stop_price
    assert plan.requires_order_change is True
    assert plan.requires_manual_review is False


def test_adjustment_plan_converts_partial_reduction_to_review_plan():
    evaluation = AdaptiveManagementEvaluation(
        symbol="BTCUSDT",
        action=ACTION_PARTIAL_REDUCTION_SHADOW,
        reason="POSITION_DETERIORATING_PARTIAL_REDUCTION_SHADOW",
        confidence=82,
        current_price=Decimal("99.10"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        pnl_pct=Decimal("-0.009"),
        suggested_stop_price=Decimal("99"),
        suggested_take_profit_price=Decimal("104"),
        suggested_reduction_fraction=Decimal("0.5"),
        should_reduce_position=True,
        should_exit_position=False,
        deterioration_score=Decimal("0.75"),
        management_mode="PARTIAL_REDUCTION",
        trade_plan_id="tp_test",
    )

    plan = AdaptiveAdjustmentPlanner().build_shadow_plan(
        evaluation=evaluation,
        runtime_state=_runtime_state(),
    )

    assert plan.action == PLAN_ACTION_PARTIAL_REDUCTION_REVIEW
    assert plan.status == PLAN_STATUS_SHADOW_REVIEW_REQUIRED
    assert plan.requires_order_change is False
    assert plan.requires_manual_review is True
    assert plan.target_reduction_fraction == Decimal("0.5")
    assert plan.should_reduce_position is True
    assert plan.should_exit_position is False


def test_adjustment_plan_converts_exit_position_to_review_plan():
    evaluation = AdaptiveManagementEvaluation(
        symbol="BTCUSDT",
        action=ACTION_EXIT_POSITION_SHADOW,
        reason="POSITION_SEVERE_DETERIORATION_SHADOW",
        confidence=90,
        current_price=Decimal("98.70"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        pnl_pct=Decimal("-0.013"),
        suggested_stop_price=Decimal("99"),
        suggested_take_profit_price=Decimal("104"),
        suggested_reduction_fraction=Decimal("1"),
        suggested_exit_fraction=Decimal("1"),
        should_reduce_position=True,
        should_exit_position=True,
        deterioration_score=Decimal("1"),
        management_mode="EXIT_POSITION",
        trade_plan_id="tp_test",
    )

    plan = AdaptiveAdjustmentPlanner().build_shadow_plan(
        evaluation=evaluation,
        runtime_state=_runtime_state(),
    )

    assert plan.action == PLAN_ACTION_EXIT_POSITION_REVIEW
    assert plan.status == PLAN_STATUS_SHADOW_REVIEW_REQUIRED
    assert plan.requires_order_change is False
    assert plan.requires_manual_review is True
    assert plan.target_reduction_fraction == Decimal("1")
    assert plan.target_exit_fraction == Decimal("1")
    assert plan.should_reduce_position is True
    assert plan.should_exit_position is True
