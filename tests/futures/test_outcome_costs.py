"""OPP-007: each outcome of a protected trade has its own cost and net R."""

from decimal import Decimal

import pytest

from futures.costs.model import CostModel
from futures.costs.outcome import estimate_outcome_costs

D = Decimal


def _model(**overrides) -> CostModel:
    settings = {
        "taker_fee_pct": D("0.0004"),
        "maker_fee_pct": D("0.0002"),
        "estimated_spread_bps": D("2"),
        "estimated_slippage_bps": D("3"),
        "estimated_funding_rate": D("0"),
        **overrides,
    }
    return CostModel(**settings)


def test_long_outcome_costs_match_the_hand_calculation():
    costs = estimate_outcome_costs(
        direction="LONG",
        entry_price=D("100"),
        stop_price=D("99"),
        take_price=D("101.5"),
        cost_model=_model(),
    )

    assert costs is not None
    # entry: taker 0.04 + half spread and slippage 0.04; take exit maker 0.0203;
    # stop exit taker 0.0396 + half spread and slippage 0.0396.
    assert costs.cost_take_per_unit == D("0.1003")
    assert costs.cost_stop_per_unit == D("0.1592")
    assert costs.r_win == D("1.3997")
    assert costs.r_loss == D("1.1592")
    assert costs.breakeven_probability == pytest.approx(D("0.45300"), abs=D("0.00001"))


def test_short_outcome_costs_mirror_the_long_ones():
    costs = estimate_outcome_costs(
        direction="SHORT",
        entry_price=D("100"),
        stop_price=D("101"),
        take_price=D("98.5"),
        cost_model=_model(),
    )

    assert costs is not None
    assert costs.cost_take_per_unit == D("0.0997")
    assert costs.cost_stop_per_unit == D("0.1608")
    assert costs.r_win == D("1.4003")
    assert costs.r_loss == D("1.1608")


def test_a_taker_take_exit_costs_more_than_a_maker_one():
    common = {
        "direction": "LONG",
        "entry_price": D("100"),
        "stop_price": D("99"),
        "take_price": D("101.5"),
        "cost_model": _model(),
    }

    maker = estimate_outcome_costs(**common)
    taker = estimate_outcome_costs(**common, take_exit_maker=False)

    assert maker is not None and taker is not None
    assert taker.cost_take_per_unit > maker.cost_take_per_unit
    assert taker.breakeven_probability > maker.breakeven_probability
    assert taker.cost_stop_per_unit == maker.cost_stop_per_unit


def test_positive_funding_costs_the_long_and_pays_the_short():
    model = _model(estimated_funding_rate=D("0.0001"))

    long = estimate_outcome_costs(
        direction="LONG",
        entry_price=D("100"),
        stop_price=D("99"),
        take_price=D("101.5"),
        cost_model=model,
        funding_events=2,
    )
    short = estimate_outcome_costs(
        direction="SHORT",
        entry_price=D("100"),
        stop_price=D("101"),
        take_price=D("98.5"),
        cost_model=model,
        funding_events=2,
    )

    assert long is not None and short is not None
    assert long.cost_take_per_unit == D("0.1003") + D("0.02")
    assert short.cost_take_per_unit == D("0.0997") - D("0.02")


def test_wider_stop_slippage_only_worsens_the_stop_outcome():
    common = {
        "direction": "LONG",
        "entry_price": D("100"),
        "stop_price": D("99"),
        "take_price": D("101.5"),
        "cost_model": _model(),
    }

    base = estimate_outcome_costs(**common)
    stressed = estimate_outcome_costs(**common, stop_slippage_bps=D("10"))

    assert base is not None and stressed is not None
    assert stressed.r_loss > base.r_loss
    assert stressed.r_win == base.r_win


def test_expected_r_is_zero_at_the_breakeven_probability():
    costs = estimate_outcome_costs(
        direction="LONG",
        entry_price=D("100"),
        stop_price=D("99"),
        take_price=D("101.5"),
        cost_model=_model(),
    )

    assert costs is not None
    p = costs.breakeven_probability
    assert costs.expected_r(p_take=p, p_stop=1 - p) == pytest.approx(D("0"), abs=D("1e-20"))


@pytest.mark.parametrize(
    ("direction", "stop", "take"),
    [("LONG", "101", "102"), ("LONG", "99", "98"), ("SHORT", "99", "98"), ("FLAT", "99", "101")],
)
def test_wrong_side_barriers_have_no_outcome_costs(direction, stop, take):
    assert (
        estimate_outcome_costs(
            direction=direction,
            entry_price=D("100"),
            stop_price=D(stop),
            take_price=D(take),
            cost_model=_model(),
        )
        is None
    )
