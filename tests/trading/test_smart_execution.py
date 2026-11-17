from decimal import Decimal

from config.smart_settings import SmartExecutionSettings
from governor.smart_execution import SmartExecutionEngine, SmartExecutionInput
from operational_module_modes import OperationalModuleMode


def _input(**overrides):
    payload = {
        "symbol": "BTCUSDT",
        "side": "OPEN_LONG",
        "entry_price": Decimal("100"),
        "current_price": Decimal("100"),
        "mark_price": Decimal("100"),
        "estimated_spread_bps": Decimal("1"),
        "estimated_slippage_bps": Decimal("2"),
        "limit_entry_enabled": False,
        "limit_entry_max_price_drift_bps": Decimal("5"),
        "limit_entry_fallback_to_market": False,
        "mode": OperationalModuleMode.OFF,
    }
    payload.update(overrides)
    return SmartExecutionInput(**payload)


def test_smart_execution_off_uses_current_market_execution():
    decision = SmartExecutionEngine().evaluate(_input(mode=OperationalModuleMode.OFF))

    assert decision.entry_order_type == "MARKET"
    assert decision.affected_result is False
    assert decision.blocked is False


def test_smart_execution_observe_calculates_without_changing_execution():
    decision = SmartExecutionEngine().evaluate(
        _input(mode=OperationalModuleMode.OBSERVE, estimated_spread_bps=Decimal("30"))
    )

    assert decision.entry_order_type == "MARKET"
    assert decision.blocked is False
    assert decision.affected_result is False
    assert decision.reason == "SMART_EXECUTION_SPREAD_TOO_HIGH"


def test_smart_execution_shadow_calculates_without_changing_execution():
    decision = SmartExecutionEngine().evaluate(
        _input(mode=OperationalModuleMode.SHADOW, limit_entry_enabled=True)
    )

    assert decision.entry_order_type == "MARKET"
    assert decision.diagnostics["recommended_entry_order_type"] == "LIMIT"
    assert decision.affected_result is False


def test_smart_execution_active_blocks_high_spread():
    decision = SmartExecutionEngine().evaluate(
        _input(
            mode=OperationalModuleMode.ACTIVE,
            estimated_spread_bps=Decimal("30"),
            limit_entry_enabled=True,
        )
    )

    assert decision.blocked is True
    assert decision.reason == "SMART_EXECUTION_SPREAD_TOO_HIGH"
    assert decision.diagnostics["limit_fill_status"] == "LIMIT_REJECTED_BY_SPREAD"


def test_smart_execution_settings_make_spread_threshold_configurable():
    decision = SmartExecutionEngine(
        settings=SmartExecutionSettings(max_spread_bps=Decimal("40"))
    ).evaluate(_input(mode=OperationalModuleMode.ACTIVE, estimated_spread_bps=Decimal("30")))

    assert decision.blocked is False
    assert decision.reason == "SMART_EXECUTION_MARKET_SELECTED"


def test_smart_execution_active_blocks_price_drift():
    decision = SmartExecutionEngine().evaluate(
        _input(
            mode=OperationalModuleMode.ACTIVE,
            current_price=Decimal("102"),
            mark_price=Decimal("100"),
            limit_entry_enabled=True,
            limit_entry_max_price_drift_bps=Decimal("5"),
        )
    )

    assert decision.blocked is True
    assert decision.reason == "SMART_EXECUTION_PRICE_DRIFT_TOO_HIGH"
    assert decision.diagnostics["limit_fill_status"] == "LIMIT_REJECTED_BY_DRIFT"


def test_smart_execution_active_blocks_stale_market_data():
    decision = SmartExecutionEngine().evaluate(
        _input(
            mode=OperationalModuleMode.ACTIVE,
            market_data_age_seconds=Decimal("4"),
        )
    )

    assert decision.blocked is True
    assert decision.reason == "SMART_EXECUTION_MARKET_DATA_STALE"


def test_smart_execution_active_uses_limit_when_enabled():
    decision = SmartExecutionEngine().evaluate(
        _input(
            mode=OperationalModuleMode.ACTIVE,
            limit_entry_enabled=True,
            bid=Decimal("99.99"),
            ask=Decimal("100.01"),
        )
    )

    assert decision.entry_order_type == "LIMIT"
    assert decision.limit_price == Decimal("99.99")
    assert decision.affected_result is True


def test_smart_execution_fallback_to_market_only_when_allowed():
    blocked = SmartExecutionEngine().evaluate(
        _input(
            mode=OperationalModuleMode.ACTIVE,
            limit_entry_enabled=True,
            current_price=None,
            limit_entry_fallback_to_market=False,
        )
    )
    fallback = SmartExecutionEngine().evaluate(
        _input(
            mode=OperationalModuleMode.ACTIVE,
            limit_entry_enabled=True,
            current_price=Decimal("100"),
            limit_entry_fallback_to_market=True,
        )
    )

    assert blocked.blocked is True
    assert fallback.entry_order_type == "LIMIT"
    assert fallback.blocked is False


def test_smart_execution_invalid_data_blocks_only_in_active():
    observe = SmartExecutionEngine().evaluate(
        _input(mode=OperationalModuleMode.OBSERVE, current_price=None)
    )
    active = SmartExecutionEngine().evaluate(
        _input(mode=OperationalModuleMode.ACTIVE, current_price=None)
    )

    assert observe.blocked is False
    assert active.blocked is True
