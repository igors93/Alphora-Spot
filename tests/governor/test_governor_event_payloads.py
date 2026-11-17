from decimal import Decimal

from governor.event_payloads import planned_exit_stop, planned_exit_take


def test_planned_exit_helpers_parse_decimal_prices():
    planned_exit = {
        "stop_price": "99.50",
        "take_profit_price": Decimal("105.25"),
    }

    assert planned_exit_stop(planned_exit) == Decimal("99.50")
    assert planned_exit_take(planned_exit) == Decimal("105.25")


def test_planned_exit_helpers_return_none_when_missing():
    assert planned_exit_stop(None) is None
    assert planned_exit_take(None) is None
    assert planned_exit_stop({}) is None
    assert planned_exit_take({}) is None
