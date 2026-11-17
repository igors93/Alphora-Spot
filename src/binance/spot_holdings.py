import json
import os
import threading
from decimal import Decimal
from pathlib import Path


class SpotHoldingsStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self.positions: dict[str, Decimal] = {}
        self.costs: dict[str, Decimal] = {}
        self.order_fills: dict[str, Decimal] = {}
        self.order_quotes: dict[str, Decimal] = {}
        self.fees: dict[str, dict[str, str]] = {}
        self.executions: list[dict[str, str]] = []
        if self.path and self.path.exists():
            with self.path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
            self.positions = {
                symbol: Decimal(str(quantity))
                for symbol, quantity in payload.get("positions", {}).items()
            }
            self.order_fills = {
                order_id: Decimal(str(quantity))
                for order_id, quantity in payload.get("order_fills", {}).items()
            }
            self.costs = {
                symbol: Decimal(str(value))
                for symbol, value in payload.get("costs", {}).items()
            }
            self.order_quotes = {
                order_id: Decimal(str(value))
                for order_id, value in payload.get("order_quotes", {}).items()
            }
            self.fees = dict(payload.get("fees", {}))
            self.executions = list(payload.get("executions", []))

    def quantity(self, symbol: str) -> Decimal:
        with self._lock:
            return self.positions.get(symbol, Decimal("0"))

    def average_cost(self, symbol: str) -> Decimal:
        with self._lock:
            quantity = self.positions.get(symbol, Decimal("0"))
            cost = self.costs.get(symbol, Decimal("0"))
            return cost / quantity if quantity > 0 and cost > 0 else Decimal("0")

    def record_order_fill(
        self, *, symbol: str, side: str, order_id: str, executed_quantity: Decimal,
        quote_quantity: Decimal = Decimal("0"),
    ) -> Decimal:
        with self._lock:
            previous = self.order_fills.get(order_id, Decimal("0"))
            previous_quote = self.order_quotes.get(order_id, Decimal("0"))
            delta = max(Decimal("0"), executed_quantity - previous)
            quote_delta = max(Decimal("0"), quote_quantity - previous_quote)
            if delta <= 0 and quote_delta <= 0:
                return Decimal("0")
            owned = self.positions.get(symbol, Decimal("0"))
            if side == "BUY":
                self.positions[symbol] = owned + delta
                self.costs[symbol] = self.costs.get(symbol, Decimal("0")) + quote_delta
            elif side == "SELL":
                remaining = max(Decimal("0"), owned - delta)
                self.positions[symbol] = remaining
                if owned > 0:
                    self.costs[symbol] = self.costs.get(symbol, Decimal("0")) * remaining / owned
            else:
                return Decimal("0")
            self.order_fills[order_id] = max(previous, executed_quantity)
            self.order_quotes[order_id] = max(previous_quote, quote_quantity)
            self.executions.append({
                "symbol": symbol, "side": side, "order_id": order_id,
                "quantity": str(delta), "quote_quantity": str(quote_delta),
            })
            self._save()
            return delta

    def record_fee(
        self, *, symbol: str, side: str, order_id: str, trade_id: str,
        asset: str, amount: Decimal,
    ) -> None:
        if not trade_id or amount <= 0:
            return
        fee_key = f"{order_id}:{trade_id}"
        with self._lock:
            if fee_key in self.fees:
                return
            base_asset = symbol.removesuffix("USDT")
            if asset == base_asset:
                owned = self.positions.get(symbol, Decimal("0"))
                remaining = max(Decimal("0"), owned - amount)
                self.positions[symbol] = remaining
                if side == "SELL" and owned > 0:
                    self.costs[symbol] = self.costs.get(symbol, Decimal("0")) * remaining / owned
            elif asset == "USDT" and side == "BUY":
                self.costs[symbol] = self.costs.get(symbol, Decimal("0")) + amount
            self.fees[fee_key] = {"symbol": symbol, "asset": asset, "amount": str(amount)}
            self.executions.append({
                "symbol": symbol, "side": side, "order_id": order_id,
                "trade_id": trade_id, "fee_asset": asset, "fee_amount": str(amount),
            })
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "positions": {key: str(value) for key, value in self.positions.items()},
            "costs": {key: str(value) for key, value in self.costs.items()},
            "order_fills": {key: str(value) for key, value in self.order_fills.items()},
            "order_quotes": {key: str(value) for key, value in self.order_quotes.items()},
            "fees": self.fees,
            "executions": self.executions,
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.path)
