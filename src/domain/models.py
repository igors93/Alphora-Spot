import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from domain.candle import Candle


class TradeAction(Enum):
    HOLD = "HOLD"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_POSITION = "CLOSE_POSITION"


class PositionDirection(Enum):
    NONE = "NONE"
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    price: Decimal
    prices: list[Decimal]
    indicators: dict[str, Any] = field(default_factory=dict)
    # Candle fields — None when candle hub is not active (backward-compat safe)
    candles: "dict[str, tuple[Candle, ...]] | None" = None
    candle_diagnostics: "dict[str, Any] | None" = None
    primary_timeframe: str | None = None
    spread_bps: Decimal | None = None
    data_age_seconds: float = 0.0
    stale: bool = False


@dataclass(frozen=True)
class FuturesPosition:
    symbol: str
    direction: PositionDirection
    quantity: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.direction != PositionDirection.NONE and self.quantity > 0

    @property
    def holding_seconds(self) -> float | None:
        if not self.is_open or self.opened_at is None:
            return None

        return time.time() - self.opened_at


@dataclass(frozen=True)
class TradeDecision:
    action: TradeAction
    reason: str

    @staticmethod
    def hold(reason: str) -> "TradeDecision":
        return TradeDecision(action=TradeAction.HOLD, reason=reason)

    @staticmethod
    def open_long(reason: str) -> "TradeDecision":
        return TradeDecision(action=TradeAction.OPEN_LONG, reason=reason)

    @staticmethod
    def open_short(reason: str) -> "TradeDecision":
        return TradeDecision(action=TradeAction.OPEN_SHORT, reason=reason)

    @staticmethod
    def close(reason: str) -> "TradeDecision":
        return TradeDecision(action=TradeAction.CLOSE_POSITION, reason=reason)
