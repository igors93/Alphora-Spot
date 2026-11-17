"""Durable entry intent and lifecycle sidecar for the M4 execution migration.

The existing RuntimeStateStore format remains unchanged. A pending entry lives
beside it until the exchange outcome and protection have been confirmed.
"""

import json
import os
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

from domain.orders import OrderLifecycle, OrderPurpose, OrderState


@dataclass(frozen=True)
class PendingOrderIntent:
    symbol: str
    side: str
    quantity: Decimal
    lifecycle: OrderLifecycle

    def __post_init__(self) -> None:
        if not self.symbol or self.side not in {"BUY", "SELL"}:
            raise ValueError("entry intent requires a symbol and BUY or SELL side")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("entry intent quantity must be finite and positive")


class OrderIntentStore:
    """One pending order per symbol and purpose, written before network mutation."""

    def __init__(self, state_path: str, purpose: OrderPurpose) -> None:
        state_file = Path(state_path)
        self.purpose = purpose
        self.path = state_file.with_name(f"{state_file.stem}.{purpose.value.lower()}_intent.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> PendingOrderIntent | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if data["schema_version"] != 1:
                raise ValueError("unsupported entry intent schema")
            lifecycle = OrderLifecycle(
                intent_id=data["intent_id"],
                client_order_id=data["client_order_id"],
                purpose=OrderPurpose(data.get("purpose", "ENTRY")),
                history=tuple(OrderState(value) for value in data["history"]),
            )
            if lifecycle.purpose is not self.purpose:
                raise ValueError("order intent purpose mismatch")
            return PendingOrderIntent(
                symbol=data["symbol"],
                side=data["side"],
                quantity=Decimal(data["quantity"]),
                lifecycle=lifecycle,
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            raise ValueError(f"invalid entry intent at {self.path}") from error

    def begin(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        trade_id: str,
        client_order_id: str,
    ) -> PendingOrderIntent:
        if self.load() is not None:
            raise ValueError(f"pending entry intent already exists at {self.path}")
        lifecycle = (
            OrderLifecycle(
                intent_id=trade_id,
                client_order_id=client_order_id,
                purpose=self.purpose,
            )
            .advance(OrderState.RESERVED)
            .advance(OrderState.PERSISTED)
        )
        intent = PendingOrderIntent(
            symbol=symbol,
            side=side,
            quantity=quantity,
            lifecycle=lifecycle,
        )
        self._save(intent)
        return intent

    def advance(self, next_state: OrderState) -> PendingOrderIntent:
        intent = self.load()
        if intent is None:
            raise ValueError("entry intent is missing")
        updated = replace(intent, lifecycle=intent.lifecycle.advance(next_state))
        self._save(updated)
        return updated

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def _save(self, intent: PendingOrderIntent) -> None:
        payload = {
            "schema_version": 1,
            "purpose": self.purpose.value,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": str(intent.quantity),
            "intent_id": intent.lifecycle.intent_id,
            "client_order_id": intent.lifecycle.client_order_id,
            "history": [state.value for state in intent.lifecycle.history],
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


class EntryIntentStore(OrderIntentStore):
    def __init__(self, state_path: str) -> None:
        super().__init__(state_path, OrderPurpose.ENTRY)


class CloseIntentStore(OrderIntentStore):
    def __init__(self, state_path: str) -> None:
        super().__init__(state_path, OrderPurpose.CLOSE)
