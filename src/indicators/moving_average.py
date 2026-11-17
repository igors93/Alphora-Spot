from collections.abc import Iterable, Sequence
from decimal import Decimal

from indicators.common import to_decimal


def simple_moving_average(values: Sequence[Decimal], window: int) -> Decimal | None:
    if window <= 0:
        raise ValueError("window precisa ser maior que zero")
    if len(values) < window:
        return None

    total = Decimal("0")
    start = len(values) - window
    for index in range(start, len(values)):
        total += to_decimal(values[index])
    return total / Decimal(window)


def exponential_moving_average(values: Iterable[Decimal], window: int) -> Decimal | None:
    """Standard EMA: seeded with the SMA of the first ``window`` values, then
    smoothed over every remaining value of the series.

    Seeding with a single raw value and smoothing only the last ``window``
    values leaves ~13% of the result anchored to that one seed price, which
    distorts trend filters and EMA slopes.
    """
    if window <= 0:
        raise ValueError("window precisa ser maior que zero")

    sample = [to_decimal(value) for value in values]
    if len(sample) < window:
        return None

    multiplier = Decimal("2") / Decimal(window + 1)
    ema = sum(sample[:window], Decimal("0")) / Decimal(window)
    for value in sample[window:]:
        ema = (value - ema) * multiplier + ema
    return ema


def exponential_moving_average_series(
    values: Iterable[Decimal], window: int
) -> list[Decimal | None]:
    """EMA at every index of ``values``, seeded exactly like ``exponential_moving_average``.

    Indexes before the seed window are ``None``; the last element equals
    ``exponential_moving_average(values, window)``. Lets a strategy compare each
    candle with the EMA of its own time in a single pass.
    """
    if window <= 0:
        raise ValueError("window precisa ser maior que zero")

    sample = [to_decimal(value) for value in values]
    result: list[Decimal | None] = [None] * len(sample)
    if len(sample) < window:
        return result

    multiplier = Decimal("2") / Decimal(window + 1)
    ema = sum(sample[:window], Decimal("0")) / Decimal(window)
    result[window - 1] = ema
    for index in range(window, len(sample)):
        ema = (sample[index] - ema) * multiplier + ema
        result[index] = ema
    return result
