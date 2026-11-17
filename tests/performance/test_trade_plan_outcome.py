from decimal import Decimal

from domain.models import PositionDirection
from domain.trade_result import TradeResult
from trading.outcome import (
    OUTCOME_LOSS,
    OUTCOME_WIN,
    PLAN_LINKED,
    build_trade_plan_outcome_from_live_payload,
    build_trade_plan_outcome_from_trade_result,
)


def test_trade_plan_outcome_records_winning_paper_trade():
    trade = TradeResult(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        entry_time="1",
        exit_time="2",
        entry_price=Decimal("100"),
        exit_price=Decimal("104"),
        quantity=Decimal("0.1"),
        notional=Decimal("10"),
        gross_pnl=Decimal("0.4"),
        fees=Decimal("0.01"),
        slippage_cost=Decimal("0.01"),
        spread_cost=Decimal("0.01"),
        funding_cost=Decimal("0"),
        net_pnl=Decimal("0.37"),
        return_pct=Decimal("0.037"),
        exit_reason="governor_close",
        strategy_reason_entry="entry",
        strategy_reason_exit="exit",
    )

    outcome = build_trade_plan_outcome_from_trade_result(
        trade_result=trade,
        trade_plan_id="tp_test",
        planned_stop_price=Decimal("98"),
        planned_take_profit_price=Decimal("104"),
        adaptive_adjustments_applied=1,
        last_adaptive_adjustment_plan_id="adj_test",
    )

    assert outcome.trade_plan_id == "tp_test"
    assert outcome.outcome_status == OUTCOME_WIN
    assert outcome.plan_link_status == PLAN_LINKED
    assert outcome.net_pnl == Decimal("0.37")
    assert outcome.adaptive_adjustments_applied == 1
    assert outcome.last_adaptive_adjustment_plan_id == "adj_test"


def test_trade_plan_outcome_records_losing_paper_trade():
    trade = TradeResult(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        entry_time="1",
        exit_time="2",
        entry_price=Decimal("100"),
        exit_price=Decimal("98"),
        quantity=Decimal("0.1"),
        notional=Decimal("10"),
        gross_pnl=Decimal("-0.2"),
        fees=Decimal("0.01"),
        slippage_cost=Decimal("0.01"),
        spread_cost=Decimal("0.01"),
        funding_cost=Decimal("0"),
        net_pnl=Decimal("-0.23"),
        return_pct=Decimal("-0.023"),
        exit_reason="governor_close",
        strategy_reason_entry="entry",
        strategy_reason_exit="exit",
    )

    outcome = build_trade_plan_outcome_from_trade_result(
        trade_result=trade,
        trade_plan_id="tp_test",
    )

    assert outcome.outcome_status == OUTCOME_LOSS
    assert outcome.trade_plan_id == "tp_test"


def test_live_outcome_records_income_history_cost_components():
    outcome = build_trade_plan_outcome_from_live_payload(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        trade_plan_id="tp_live",
        active_trade_id="trade_live",
        planned_stop_price=Decimal("98"),
        planned_take_profit_price=Decimal("104"),
        exit_reason="strategy close",
        pnl_result={
            "close_price_estimate": Decimal("101"),
            "gross_pnl_estimate": Decimal("1"),
            "realized_pnl": Decimal("1"),
            "commission": Decimal("-0.08"),
            "funding_fee": Decimal("-0.02"),
            "net_pnl": Decimal("0.90"),
            "pnl_status": "confirmed_income_history",
            "pnl_source": "binance_income_history",
        },
    )

    assert outcome.net_pnl == Decimal("0.90")
    assert outcome.fees == Decimal("0.08")
    assert outcome.funding_cost == Decimal("0.02")
    assert outcome.plan_link_status == PLAN_LINKED
