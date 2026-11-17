from collections.abc import Sequence
from decimal import Decimal

from indicators.common import to_decimal


def relative_strength_index(prices: Sequence[Decimal], period: int) -> Decimal | None:
    if period < 1:
        raise ValueError("period precisa ser maior que zero")
    if len(prices) < period + 1:
        return None

    gains = Decimal("0")
    losses = Decimal("0")
    start = len(prices) - period
    for index in range(start, len(prices)):
        current = to_decimal(prices[index])
        previous = to_decimal(prices[index - 1])
        change = current - previous
        if change > 0:
            gains += change
        elif change < 0:
            losses += abs(change)

    average_gain = gains / Decimal(period)
    average_loss = losses / Decimal(period)

    if average_loss == 0:
        if average_gain == 0:
            return Decimal("50")
        return Decimal("100")

    relative_strength = average_gain / average_loss
    rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + relative_strength))
    return rsi.quantize(Decimal("0.01"))
