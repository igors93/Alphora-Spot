from __future__ import annotations

import threading
from collections import deque
from decimal import Decimal


def normalize_market_symbol(symbol: str) -> str:
    return str(symbol or "").upper().strip()


class MarketPriceWindow:
    """Thread-safe owner of rolling price history shared by market data providers."""

    def __init__(self, *, maxlen: int, symbols: tuple[str, ...] = ()) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be at least 1")
        self.maxlen = int(maxlen)
        self._lock = threading.RLock()
        self._prices: dict[str, deque[Decimal]] = {
            normalize_market_symbol(symbol): deque(maxlen=self.maxlen)
            for symbol in symbols
            if normalize_market_symbol(symbol)
        }

    @property
    def prices_by_symbol(self) -> dict[str, deque[Decimal]]:
        """Compatibility view. Returns a shallow copy; individual deques are live references."""
        with self._lock:
            return dict(self._prices)

    def record(self, symbol: str, price: Decimal) -> tuple[Decimal, ...]:
        normalized = normalize_market_symbol(symbol)
        if not normalized:
            raise ValueError("symbol cannot be empty")
        normalized_price = Decimal(str(price))
        with self._lock:
            prices = self._prices.setdefault(normalized, deque(maxlen=self.maxlen))
            prices.append(normalized_price)
            return tuple(prices)

    def snapshot(self, symbol: str) -> tuple[Decimal, ...]:
        normalized = normalize_market_symbol(symbol)
        with self._lock:
            return tuple(self._prices.get(normalized, ()))

    def record_if_new(
        self,
        symbol: str,
        price: Decimal,
        *,
        previous_price: Decimal | None = None,
    ) -> tuple[Decimal, ...]:
        """Record a value unless it is the same observation already represented at the tail."""

        normalized = normalize_market_symbol(symbol)
        normalized_price = Decimal(str(price))
        with self._lock:
            prices = self._prices.setdefault(normalized, deque(maxlen=self.maxlen))
            if not prices or prices[-1] != normalized_price or previous_price != normalized_price:
                prices.append(normalized_price)
            return tuple(prices)
