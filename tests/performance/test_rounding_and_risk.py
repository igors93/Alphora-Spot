from decimal import Decimal

import pytest

from domain.models import TradeAction
from domain.symbol_rules import round_price_down, round_price_up
from futures.exchange_rules import extract_symbol_rules
from futures.risk_manager import FuturesRiskManager


def test_price_rounding_up_and_down():
    assert round_price_down(Decimal("100.049"), Decimal("0.10")) == Decimal("100.00")
    assert round_price_up(Decimal("100.001"), Decimal("0.10")) == Decimal("100.10")
    assert round_price_up(Decimal("100.000"), Decimal("0.10")) == Decimal("100.00")


def test_risk_manager_uses_directional_rounding(symbol_rules):
    risk_manager = FuturesRiskManager(50, 0.003, 0.004)
    entry = Decimal("100.05")

    assert risk_manager.calculate_stop_price(
        TradeAction.OPEN_LONG,
        entry,
        symbol_rules,
    ) == Decimal("99.70")
    assert risk_manager.calculate_take_profit_price(
        TradeAction.OPEN_LONG,
        entry,
        symbol_rules,
    ) == Decimal("100.50")
    assert risk_manager.calculate_stop_price(
        TradeAction.OPEN_SHORT,
        entry,
        symbol_rules,
    ) == Decimal("100.40")
    assert risk_manager.calculate_take_profit_price(
        TradeAction.OPEN_SHORT,
        entry,
        symbol_rules,
    ) == Decimal("99.60")


def test_quantity_rounding_and_min_notional(symbol_rules):
    risk_manager = FuturesRiskManager(50, 0.01, 0.02)

    assert risk_manager.calculate_quantity(Decimal("100"), symbol_rules) == Decimal("0.500")

    with pytest.raises(ValueError):
        FuturesRiskManager(1, 0.01, 0.02).calculate_quantity(
            Decimal("100"),
            symbol_rules,
        )


def test_extract_symbol_rules_supports_min_notional_variants():
    rules = extract_symbol_rules(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                    ],
                }
            ]
        },
        "BTCUSDT",
    )

    assert rules.min_notional == Decimal("5")
