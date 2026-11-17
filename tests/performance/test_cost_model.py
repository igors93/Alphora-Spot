from decimal import Decimal

from futures.performance.cost_model import CostModel


def test_cost_model_calculates_taker_entry_and_exit_fees():
    model = CostModel.from_numbers(taker_fee_pct="0.001", slippage_bps=0, spread_bps=0)

    costs = model.calculate_total_cost(
        entry_notional=Decimal("100"),
        exit_notional=Decimal("110"),
    )

    assert costs.entry_fee == Decimal("0.100")
    assert costs.exit_fee == Decimal("0.110")
    assert costs.fees == Decimal("0.210")


def test_cost_model_calculates_slippage_and_spread():
    model = CostModel.from_numbers(
        taker_fee_pct=0,
        slippage_bps="2",
        spread_bps="1",
    )

    costs = model.calculate_total_cost(
        entry_notional=Decimal("100"),
        exit_notional=Decimal("100"),
    )

    assert costs.slippage_cost == Decimal("0.0400")
    assert costs.spread_cost == Decimal("0.0100")
    assert model.spread_bps_semantics == "FULL_BID_ASK_SPREAD"
    assert model.slippage_bps_semantics == "PER_MARKET_FILL"


def test_cost_model_calculates_net_pnl_after_costs():
    model = CostModel.from_numbers(
        taker_fee_pct="0.001",
        slippage_bps=0,
        spread_bps=0,
        funding_enabled=True,
        default_funding_rate="0.0001",
    )

    net_pnl, costs = model.calculate_net_pnl(
        gross_pnl=Decimal("10"),
        entry_notional=Decimal("100"),
        exit_notional=Decimal("110"),
        holding_hours=Decimal("8"),
    )

    assert costs.fees == Decimal("0.210")
    assert costs.funding_cost == Decimal("0.0105")
    assert net_pnl == Decimal("9.7795")
