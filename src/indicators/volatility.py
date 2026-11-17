from collections.abc import Sequence
from decimal import Decimal

from indicators.common import to_decimal


def percentage_change(previous: Decimal, current: Decimal) -> Decimal:
    previous = to_decimal(previous)
    current = to_decimal(current)
    if previous == 0:
        return Decimal("0")
    return (current - previous) / previous


def average_absolute_return(prices: Sequence[Decimal], window: int) -> Decimal | None:
    if window <= 0:
        raise ValueError("window precisa ser maior que zero")
    if len(prices) < window + 1:
        return None

    start = len(prices) - window
    total = Decimal("0")
    for index in range(start, len(prices)):
        total += abs(percentage_change(prices[index - 1], prices[index]))
    return total / Decimal(window)


def average_range_pct(
    highs: Sequence[Decimal], lows: Sequence[Decimal], window: int
) -> Decimal | None:
    if window <= 0:
        raise ValueError("window precisa ser maior que zero")
    if len(highs) != len(lows):
        raise ValueError("highs e lows precisam ter o mesmo tamanho")
    if len(highs) < window:
        return None

    total = Decimal("0")
    start = len(highs) - window
    for index in range(start, len(highs)):
        high = to_decimal(highs[index])
        low = to_decimal(lows[index])
        if low == 0:
            continue
        total += (high - low) / low
    return total / Decimal(window)
