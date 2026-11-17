from decimal import Decimal

from trading.intelligent_execution_planner import (
    INTELLIGENT_EXECUTION_STATUS_DISABLED,
    INTELLIGENT_EXECUTION_STATUS_MARKET_DATA_STALE,
    INTELLIGENT_EXECUTION_STATUS_NOT_ACTIVE_LEVEL,
    INTELLIGENT_EXECUTION_STATUS_PRICE_DRIFT_TOO_HIGH,
    INTELLIGENT_EXECUTION_STATUS_READY,
    INTELLIGENT_EXECUTION_STYLE_MARKET_NOW,
    INTELLIGENT_EXECUTION_STYLE_WAIT,
    IntelligentExecutionPlanner,
)


def _build_plan(**overrides):
    params = {
        "symbol": "BTCUSDT",
        "action": "OPEN_LONG",
        "architecture_mode": "INTELLIGENT_FLOW",
        "intelligent_activation_level": "PAPER_ACTIVE",
        "execution_mode": "PAPER_LIVE",
        "execution_planner_enabled": True,
        "intent_market_price": Decimal("100"),
        "mark_price": Decimal("100"),
        "market_data_age_seconds": Decimal("0"),
        "market_data_stale": False,
        "planned_quantity": Decimal("0.5"),
        "planned_exit": {
            "stop_price": Decimal("99"),
            "take_profit_price": Decimal("102"),
        },
        "trade_plan_id": "tp_test",
    }
    params.update(overrides)
    return IntelligentExecutionPlanner().build_plan(**params)


def test_intelligent_execution_planner_stays_disabled_when_flag_off():
    plan = _build_plan(execution_planner_enabled=False)

    assert plan.status == INTELLIGENT_EXECUTION_STATUS_DISABLED
    assert plan.should_execute is True
    assert plan.execution_will_run is True
    assert plan.can_use_for_execution is False


def test_intelligent_execution_planner_releases_market_now_in_paper_active():
    plan = _build_plan()

    assert plan.status == INTELLIGENT_EXECUTION_STATUS_READY
    assert plan.execution_style == INTELLIGENT_EXECUTION_STYLE_MARKET_NOW
    assert plan.should_execute is True
    assert plan.execution_will_run is True
    assert plan.applied is True
    assert plan.paper_only is True
    assert plan.testnet_only is False
    assert plan.can_activate_testnet is False
    assert plan.can_activate_real is False
    assert plan.planned_quantity_present is True
    assert plan.planned_exit_present is True


def test_intelligent_execution_planner_waits_for_stale_market_data():
    plan = _build_plan(
        market_data_age_seconds=Decimal("9"),
        market_data_stale=True,
    )

    assert plan.status == INTELLIGENT_EXECUTION_STATUS_MARKET_DATA_STALE
    assert plan.execution_style == INTELLIGENT_EXECUTION_STYLE_WAIT
    assert plan.should_execute is False
    assert plan.execution_will_run is False
    assert plan.applied is True
    assert plan.can_use_for_execution is True


def test_intelligent_execution_planner_waits_for_price_stability():
    plan = _build_plan(
        intent_market_price=Decimal("101"),
        mark_price=Decimal("100"),
    )

    assert plan.status == INTELLIGENT_EXECUTION_STATUS_PRICE_DRIFT_TOO_HIGH
    assert plan.execution_style == INTELLIGENT_EXECUTION_STYLE_WAIT
    assert plan.should_execute is False
    assert plan.execution_will_run is False
    assert plan.price_drift_bps == Decimal("100.00")


def test_intelligent_execution_planner_applies_in_testnet_block():
    plan = _build_plan(
        intelligent_activation_level="TESTNET_BLOCK",
        execution_mode="TESTNET_LIVE",
    )

    assert plan.status == INTELLIGENT_EXECUTION_STATUS_READY
    assert plan.should_execute is True
    assert plan.paper_only is False
    assert plan.testnet_only is True
    assert plan.can_activate_testnet is True
    assert plan.can_activate_real is False


def test_intelligent_execution_planner_does_not_activate_in_testnet_reduce():
    plan = _build_plan(
        intelligent_activation_level="TESTNET_REDUCE",
        execution_mode="TESTNET_LIVE",
    )

    assert plan.status == INTELLIGENT_EXECUTION_STATUS_NOT_ACTIVE_LEVEL
    assert plan.should_execute is True
    assert plan.applied is False
    assert plan.can_use_for_execution is False
