import inspect
from decimal import Decimal

from futures.planning.economic_exit_math import calculate_economic_exit


def test_economic_exit_math_calculates_take_minimum_by_net_gain_bps():
    result = calculate_economic_exit(
        action="OPEN_LONG",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        gross_stop_distance=Decimal("0.10"),
        gross_take_distance_configured=Decimal("0.10"),
        round_trip_cost=Decimal("0.10"),
        min_net_gain_bps=Decimal("20"),
        min_net_reward_risk=Decimal("0"),
    )

    assert result.required_net_gain == Decimal("0.2")
    assert result.required_take_distance_by_min_net_gain == Decimal("0.30")
    assert result.required_take_distance_final == Decimal("0.30")
    assert result.expected_net_gain == Decimal("0.20")


def test_economic_exit_math_calculates_take_minimum_by_reward_risk():
    result = calculate_economic_exit(
        action="OPEN_LONG",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        gross_stop_distance=Decimal("1"),
        gross_take_distance_configured=Decimal("0.40"),
        round_trip_cost=Decimal("0.10"),
        min_net_gain_bps=Decimal("0"),
        min_net_reward_risk=Decimal("1.5"),
    )

    assert result.expected_net_loss == Decimal("1.10")
    assert result.required_take_distance_by_reward_risk == Decimal("1.750")
    assert result.required_take_distance_final == Decimal("1.750")
    assert result.reward_risk == Decimal("1.5")


def test_economic_exit_math_uses_larger_of_configured_and_economic_take():
    result = calculate_economic_exit(
        action="OPEN_LONG",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        gross_stop_distance=Decimal("0.20"),
        gross_take_distance_configured=Decimal("2"),
        round_trip_cost=Decimal("0.10"),
        min_net_gain_bps=Decimal("5"),
        min_net_reward_risk=Decimal("1.5"),
        max_allowed_take_distance=Decimal("1.20"),
    )

    assert result.required_take_distance_final == Decimal("2")
    assert result.blocked is False
    assert result.required_take_price == Decimal("102")


def test_economic_exit_math_blocks_when_economic_take_adjustment_is_too_far():
    result = calculate_economic_exit(
        action="OPEN_LONG",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        gross_stop_distance=Decimal("1"),
        gross_take_distance_configured=Decimal("0.40"),
        round_trip_cost=Decimal("0.10"),
        min_net_gain_bps=Decimal("0"),
        min_net_reward_risk=Decimal("1.5"),
        max_allowed_take_distance=Decimal("1.20"),
    )

    assert result.blocked is True
    assert result.block_reason == "ECONOMIC_TAKE_REQUIRED_TOO_FAR"
    assert result.economic_required_take_bps == Decimal("175.00")
    assert result.max_allowed_take_bps == Decimal("120.00")


def test_economic_exit_math_open_long_take_price_is_above_entry():
    result = calculate_economic_exit(
        action="OPEN_LONG",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        gross_stop_distance=Decimal("0.30"),
        gross_take_distance_configured=Decimal("0.80"),
        round_trip_cost=Decimal("0.10"),
        min_net_gain_bps=Decimal("5"),
        min_net_reward_risk=Decimal("1.5"),
    )

    assert result.required_take_price > result.entry_price


def test_economic_exit_math_open_short_take_price_is_below_entry():
    result = calculate_economic_exit(
        action="OPEN_SHORT",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        gross_stop_distance=Decimal("0.30"),
        gross_take_distance_configured=Decimal("0.80"),
        round_trip_cost=Decimal("0.10"),
        min_net_gain_bps=Decimal("5"),
        min_net_reward_risk=Decimal("1.5"),
    )

    assert result.required_take_price < result.entry_price


def test_economic_exit_math_keeps_core_calculation_decimal_only():
    result = calculate_economic_exit(
        action="OPEN_LONG",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        gross_stop_distance=Decimal("0.30"),
        gross_take_distance_configured=Decimal("0.80"),
        round_trip_cost=Decimal("0.10"),
        min_net_gain_bps=Decimal("5"),
        min_net_reward_risk=Decimal("1.5"),
    )

    assert isinstance(result.expected_net_gain, Decimal)
    assert isinstance(result.required_take_distance_final, Decimal)
    assert "float(" not in inspect.getsource(calculate_economic_exit)
