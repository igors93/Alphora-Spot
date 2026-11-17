from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool
    source: str = "unknown"

    @property
    def timestamp(self) -> str:
        """ISO UTC string of open_time — backward-compat with backtesting code."""
        return datetime.fromtimestamp(self.open_time_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    @property
    def open_time_seconds(self) -> float:
        return self.open_time_ms / 1000.0

    @property
    def close_time_seconds(self) -> float:
        return self.close_time_ms / 1000.0
