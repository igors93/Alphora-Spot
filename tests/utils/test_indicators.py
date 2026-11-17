from decimal import Decimal

from indicators.atr import PriceCandle, average_true_range, true_range
from indicators.common import tail_window, to_decimal
from indicators.moving_average import exponential_moving_average, simple_moving_average
from indicators.rsi import relative_strength_index
from indicators.volatility import (
    average_absolute_return,
    average_range_pct,
    percentage_change,
)


def test_common_decimal_and_tail_window():
    assert to_decimal("1.25") == Decimal("1.25")
    assert tail_window(range(10), 3) == [7, 8, 9]


def test_sma_and_ema_with_decimal():
    values = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]

    assert simple_moving_average(values, 3) == Decimal("3")
    assert simple_moving_average(values, 10) is None
    # SMA seed of [1, 2, 3] = 2, then (4 - 2) * 0.5 + 2 = 3
    assert exponential_moving_average(values, 3) == Decimal("3")
    assert exponential_moving_average(values[:3], 3) == Decimal("2")


def test_atr_with_decimal_candles():
    candles = [
        PriceCandle(Decimal("101"), Decimal("99"), Decimal("100")),
        PriceCandle(Decimal("103"), Decimal("100"), Decimal("102")),
        PriceCandle(Decimal("104"), Decimal("101"), Decimal("103")),
    ]

    assert true_range(
        high=Decimal("103"), low=Decimal("100"), previous_close=Decimal("100")
    ) == Decimal("3")
    assert average_true_range(candles, 2) == Decimal("3")
    assert average_true_range(candles, 10) is None


def test_rsi_handles_history_and_flat_prices():
    assert relative_strength_index([Decimal("1"), Decimal("1")], 1) == Decimal("50")
    assert relative_strength_index([Decimal("1")], 1) is None
    assert relative_strength_index([Decimal("1"), Decimal("2")], 1) == Decimal("100")


def test_volatility_helpers():
    prices = [Decimal("100"), Decimal("110"), Decimal("99")]
    highs = [Decimal("110"), Decimal("120")]
    lows = [Decimal("100"), Decimal("100")]

    assert percentage_change(Decimal("100"), Decimal("110")) == Decimal("0.1")
    assert average_absolute_return(prices, 2) == Decimal("0.1")
    assert average_range_pct(highs, lows, 2) == Decimal("0.15")
