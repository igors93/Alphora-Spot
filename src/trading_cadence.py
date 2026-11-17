"""Trading cadence policy: Alphora is a day-trade / swing-trade robot.

High-frequency trading (positions held for seconds or milliseconds, order
bursts, tick scalping) is explicitly out of scope. The limits below are hard
floors/ceilings enforced by config validation, the risk guard, the strategy
coordinator and the timer-driven test strategies. Configuration can make the
robot slower or more selective, never faster than these limits.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

# Governor polling cadence. Faster polling only adds API pressure: every
# supported strategy decides on closed candles.
MIN_LOOP_INTERVAL_SECONDS = 1.0

# Per-symbol minimum spacing between two new entries.
MIN_SECONDS_BETWEEN_ENTRIES_FLOOR = 60.0
DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES = 300.0

# Upper bound of new entries per symbol per UTC day. A configured value of
# ``0`` means "use this ceiling", never "unlimited".
MAX_DAILY_TRADES_CEILING = 48

# Minimum interval for timer-driven test strategies (execution probes).
MIN_TEST_SIGNAL_INTERVAL_SECONDS = 60.0

# Smallest primary signal timeframe allowed with real money
# (day trade: 5m-1h, swing trade: 1h-1d); domain.timeframe applies it.
MIN_REAL_SIGNAL_TIMEFRAME = "5m"

# Strategies of this category emit random/scripted signals to exercise the
# execution plumbing; they must never trade real money.
TEST_STRATEGY_CATEGORY = "FIXED_TEST"


def effective_max_daily_trades(configured: Any) -> int:
    """Return the enforced daily entry limit for a configured value."""

    try:
        value = int(configured or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return MAX_DAILY_TRADES_CEILING
    return min(value, MAX_DAILY_TRADES_CEILING)


def effective_min_seconds_between_entries(configured: Any) -> float:
    """Return the enforced per-symbol spacing between entries, in seconds."""

    try:
        value = float(configured) if configured is not None else DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES
    except (TypeError, ValueError):
        value = DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES
    if not math.isfinite(value):
        value = DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES
    return max(value, MIN_SECONDS_BETWEEN_ENTRIES_FLOOR)


def entry_cadence_block(
    *,
    opened_trades_today: int,
    seconds_since_entry: Any,
    max_daily_trades: Any,
    min_seconds_between_entries: Any,
) -> str | None:
    """The same anti-HFT gate for live risk checks and historical decisions."""
    if int(opened_trades_today) >= effective_max_daily_trades(max_daily_trades):
        return "max_daily_trades"
    elapsed = Decimal(str(seconds_since_entry))
    minimum = Decimal(str(effective_min_seconds_between_entries(min_seconds_between_entries)))
    if elapsed >= 0 and elapsed < minimum:
        return "min_seconds_between_entries"
    return None


def is_test_strategy(strategy: Any) -> bool:
    """Return whether a strategy (class or instance) is a scripted test strategy."""

    category = getattr(strategy, "strategy_category", "")
    return str(category or "").strip().upper() == TEST_STRATEGY_CATEGORY


__all__ = [
    "DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES",
    "MAX_DAILY_TRADES_CEILING",
    "MIN_LOOP_INTERVAL_SECONDS",
    "MIN_REAL_SIGNAL_TIMEFRAME",
    "MIN_SECONDS_BETWEEN_ENTRIES_FLOOR",
    "MIN_TEST_SIGNAL_INTERVAL_SECONDS",
    "TEST_STRATEGY_CATEGORY",
    "effective_max_daily_trades",
    "effective_min_seconds_between_entries",
    "entry_cadence_block",
    "is_test_strategy",
]
