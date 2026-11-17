from decimal import Decimal

from futures.planned_exit import (
    STRATEGY_METADATA_EXIT_SOURCE,
    extract_strategy_planned_exit,
    validate_strategy_planned_exit,
)


def test_extract_strategy_planned_exit_parses_valid_metadata():
    candidate = extract_strategy_planned_exit(_metadata())

    assert candidate.valid is True
    assert candidate.source == STRATEGY_METADATA_EXIT_SOURCE
    assert candidate.stop_price == Decimal("99.4")
    assert candidate.take_profit_price == Decimal("101.2")
    assert candidate.reward_risk_liquido == Decimal("1.6")


def test_extract_strategy_planned_exit_rejects_missing_prices():
    candidate = extract_strategy_planned_exit({"risk_abs": "1", "reward_abs": "2"})

    assert candidate.valid is False
    assert candidate.rejection_reason.startswith("MISSING_STRATEGY_PLANNED_EXIT_FIELD")


def test_validate_strategy_planned_exit_long_accepts_directional_prices():
    candidate = validate_strategy_planned_exit(
        "OPEN_LONG",
        Decimal("100"),
        extract_strategy_planned_exit(_metadata(stop="99.44", take="101.11")),
        price_tick=Decimal("0.10"),
    )

    assert candidate.valid is True
    assert candidate.stop_price == Decimal("99.40")
    assert candidate.take_profit_price == Decimal("101.20")


def test_validate_strategy_planned_exit_long_rejects_inverted_stop_or_take():
    stop_candidate = validate_strategy_planned_exit(
        "LONG",
        Decimal("100"),
        extract_strategy_planned_exit(_metadata(stop="100.01", take="101.2")),
    )
    take_candidate = validate_strategy_planned_exit(
        "LONG",
        Decimal("100"),
        extract_strategy_planned_exit(_metadata(stop="99", take="99.9")),
    )

    assert stop_candidate.valid is False
    assert stop_candidate.rejection_reason == "LONG_STOP_NOT_BELOW_ENTRY"
    assert take_candidate.valid is False
    assert take_candidate.rejection_reason == "LONG_TAKE_NOT_ABOVE_ENTRY"


def test_validate_strategy_planned_exit_short_accepts_directional_prices():
    candidate = validate_strategy_planned_exit(
        "OPEN_SHORT",
        Decimal("100"),
        extract_strategy_planned_exit(_metadata(stop="100.11", take="98.94")),
        price_tick=Decimal("0.10"),
    )

    assert candidate.valid is True
    assert candidate.stop_price == Decimal("100.20")
    assert candidate.take_profit_price == Decimal("98.90")


def test_validate_strategy_planned_exit_short_rejects_inverted_stop_or_take():
    stop_candidate = validate_strategy_planned_exit(
        "SHORT",
        Decimal("100"),
        extract_strategy_planned_exit(_metadata(stop="99.9", take="98")),
    )
    take_candidate = validate_strategy_planned_exit(
        "SHORT",
        Decimal("100"),
        extract_strategy_planned_exit(_metadata(stop="101", take="100.1")),
    )

    assert stop_candidate.valid is False
    assert stop_candidate.rejection_reason == "SHORT_STOP_NOT_ABOVE_ENTRY"
    assert take_candidate.valid is False
    assert take_candidate.rejection_reason == "SHORT_TAKE_NOT_BELOW_ENTRY"


def test_strategy_planned_exit_accepts_decimal_string_and_float_values():
    metadata = _metadata(
        stop=Decimal("99.4"),
        take=101.2,
        risk_abs=0.6,
        reward_abs="1.2",
        reward_risk_liquido=Decimal("1.6"),
    )

    candidate = validate_strategy_planned_exit(
        "OPEN_LONG",
        Decimal("100"),
        extract_strategy_planned_exit(metadata),
    )

    assert candidate.valid is True
    assert candidate.stop_price == Decimal("99.4")
    assert candidate.take_profit_price == Decimal("101.2")


def _metadata(
    *,
    stop="99.4",
    take="101.2",
    risk_abs="0.6",
    reward_abs="1.2",
    reward_risk_liquido="1.6",
) -> dict:
    return {
        "recommended_stop_price": stop,
        "recommended_take_profit_price": take,
        "risk_abs": risk_abs,
        "reward_abs": reward_abs,
        "estimated_cost_abs": "0.1",
        "net_reward_abs": "1.1",
        "net_risk_abs": "0.7",
        "reward_risk_liquido": reward_risk_liquido,
        "raw_reward_risk": "2",
        "cost_to_reward_ratio": "0.0833",
        "risk_pct": "0.6",
        "reward_pct": "1.2",
    }
