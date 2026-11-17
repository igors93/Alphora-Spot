from decimal import Decimal

from backtesting.domain.realism import BacktestValidationSettings, FundingRatePoint
from backtesting.engine.statistics import build_statistical_validation
from domain.models import PositionDirection
from domain.trade_result import TradeResult
from futures.performance.cost_model import CostModel


def _trade(index: int, pnl: str) -> TradeResult:
    value = Decimal(pnl)
    return TradeResult(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        entry_time=f"2024-01-{index:02d}T00:00:00Z",
        exit_time=f"2024-01-{index:02d}T01:00:00Z",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        quantity=Decimal("1"),
        notional=Decimal("100"),
        gross_pnl=value,
        fees=Decimal("0"),
        slippage_cost=Decimal("0"),
        spread_cost=Decimal("0"),
        funding_cost=Decimal("0"),
        net_pnl=value,
        return_pct=value / Decimal("100"),
        exit_reason="test",
        strategy_reason_entry="test",
        strategy_reason_exit="test",
    )


def test_statistical_validation_is_deterministic_and_has_confidence_gates():
    trades = [_trade(index, "2" if index % 4 else "-1") for index in range(1, 21)]
    settings = BacktestValidationSettings(bootstrap_samples=200, random_seed=7)

    first = build_statistical_validation(
        trades,
        initial_balance=Decimal("1000"),
        settings=settings,
    )
    second = build_statistical_validation(
        trades,
        initial_balance=Decimal("1000"),
        settings=settings,
    )

    assert first == second
    assert set(first["expectancy_ci"]) == {"lower", "median", "upper"}
    assert first["walk_forward"]["folds_with_trades"] == 5
    assert first["execution_stress"]["scenarios"]


def test_historical_funding_is_discrete_and_directional():
    model = CostModel.from_numbers(
        maker_fee_pct=0,
        taker_fee_pct=0,
        slippage_bps=0,
        spread_bps=0,
        funding_enabled=True,
    )
    points = (
        FundingRatePoint("2024-01-01T08:00:00Z", Decimal("0.001")),
        FundingRatePoint("2024-01-01T16:00:00Z", Decimal("-0.002")),
    )

    long_cost = model.calculate_funding_cost(
        Decimal("100"),
        direction="LONG",
        entry_time="2024-01-01T01:00:00Z",
        exit_time="2024-01-01T17:00:00Z",
        rate_points=points,
    )
    short_cost = model.calculate_funding_cost(
        Decimal("100"),
        direction="SHORT",
        entry_time="2024-01-01T01:00:00Z",
        exit_time="2024-01-01T17:00:00Z",
        rate_points=points,
    )

    assert long_cost == Decimal("-0.100")
    assert short_cost == Decimal("0.100")
