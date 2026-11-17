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
        self.order_fills: dict[str, Decimal] = {}
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

    def quantity(self, symbol: str) -> Decimal:
        with self._lock:
            return self.positions.get(symbol, Decimal("0"))

    def record_order_fill(
        self, *, symbol: str, side: str, order_id: str, executed_quantity: Decimal,
    ) -> Decimal:
        with self._lock:
            previous = self.order_fills.get(order_id, Decimal("0"))
            delta = max(Decimal("0"), executed_quantity - previous)
            if delta <= 0:
                return Decimal("0")
            owned = self.positions.get(symbol, Decimal("0"))
            if side == "BUY":
                self.positions[symbol] = owned + delta
            elif side == "SELL":
                self.positions[symbol] = max(Decimal("0"), owned - delta)
            else:
                return Decimal("0")
            self.order_fills[order_id] = executed_quantity
            self._save()
            return delta

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "positions": {key: str(value) for key, value in self.positions.items()},
            "order_fills": {key: str(value) for key, value in self.order_fills.items()},
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.path)
