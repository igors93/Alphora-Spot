from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from uuid import uuid4

from domain.models import TradeAction

_OPEN_ACTIONS = {TradeAction.OPEN_LONG.value, TradeAction.OPEN_SHORT.value}

_KEEP = object()


def _new_candidate_id() -> str:
    return f"cand_{uuid4().hex}"


def _freeze(d: Mapping) -> MappingProxyType:
    return MappingProxyType(dict(d))


@dataclass(frozen=True)
class ExecutionCandidate:
    """Immutable, revisioned representation of the operational payload.

    Every material change (quantity, stop, take, entry) produces a new revision
    via ``revise()``, keeping the original instance untouched. Approvals carry the
    revision they were granted for, so stale approvals are automatically detectable.
    """

    candidate_id: str
    revision: int
    intent_id: str
    trade_plan_id: str | None
    symbol: str
    action: TradeAction
    quantity: Decimal | None
    entry_price: Decimal
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    planned_exit_source: str | None
    notional: Decimal
    reason: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        action = TradeAction(self.action)
        symbol = str(self.symbol or "").upper().strip()
        if not symbol:
            raise ValueError("ExecutionCandidate symbol is required")
        entry_price = Decimal(str(self.entry_price))
        if entry_price <= 0:
            raise ValueError("ExecutionCandidate entry_price must be positive")
        quantity = Decimal(str(self.quantity)) if self.quantity is not None else None
        stop_price = Decimal(str(self.stop_price)) if self.stop_price is not None else None
        take_profit_price = (
            Decimal(str(self.take_profit_price)) if self.take_profit_price is not None else None
        )
        notional = Decimal(str(self.notional))

        if action.value in _OPEN_ACTIONS:
            if quantity is not None and quantity <= 0:
                raise ValueError("ExecutionCandidate quantity must be positive for openings")
            if stop_price is not None:
                if action is TradeAction.OPEN_LONG and stop_price >= entry_price:
                    raise ValueError("Long stop price must be below entry price")
                if action is TradeAction.OPEN_SHORT and stop_price <= entry_price:
                    raise ValueError("Short stop price must be above entry price")
            if take_profit_price is not None:
                if action is TradeAction.OPEN_LONG and take_profit_price <= entry_price:
                    raise ValueError("Long take-profit price must be above entry price")
                if action is TradeAction.OPEN_SHORT and take_profit_price >= entry_price:
                    raise ValueError("Short take-profit price must be below entry price")

        metadata = _freeze(self.metadata)

        object.__setattr__(self, "action", action)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "entry_price", entry_price)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "stop_price", stop_price)
        object.__setattr__(self, "take_profit_price", take_profit_price)
        object.__setattr__(self, "notional", notional)
        object.__setattr__(self, "metadata", metadata)

    def revise(
        self,
        *,
        quantity=_KEEP,
        stop_price=_KEEP,
        take_profit_price=_KEEP,
        entry_price=_KEEP,
        reason: str = "",
    ) -> ExecutionCandidate:
        """Return a new revision. notional is recalculated when quantity or entry_price change."""
        new_quantity = self.quantity if quantity is _KEEP else quantity
        new_stop = self.stop_price if stop_price is _KEEP else stop_price
        new_take = self.take_profit_price if take_profit_price is _KEEP else take_profit_price
        new_entry = self.entry_price if entry_price is _KEEP else Decimal(str(entry_price))

        if new_quantity is not None:
            new_notional = abs(Decimal(str(new_quantity)) * new_entry)
        else:
            new_notional = self.notional

        raw_history = self.metadata.get("revision_history", [])
        history = list(raw_history) if isinstance(raw_history, list) else []
        history.append({"from_revision": self.revision, "reason": reason})
        new_metadata = {**self.metadata, "revision_history": history}

        return ExecutionCandidate(
            candidate_id=self.candidate_id,
            revision=self.revision + 1,
            intent_id=self.intent_id,
            trade_plan_id=self.trade_plan_id,
            symbol=self.symbol,
            action=self.action,
            quantity=new_quantity,
            entry_price=new_entry,
            stop_price=new_stop,
            take_profit_price=new_take,
            planned_exit_source=self.planned_exit_source,
            notional=new_notional,
            reason=reason or self.reason,
            metadata=new_metadata,
        )

    @classmethod
    def from_intent_and_plan(
        cls,
        *,
        intent_id: str,
        trade_plan_id: str | None,
        symbol: str,
        action: TradeAction | str,
        quantity: Decimal | None,
        entry_price: Decimal,
        stop_price: Decimal | None,
        take_profit_price: Decimal | None,
        planned_exit_source: str | None = None,
        notional: Decimal,
        reason: str,
        metadata: dict | None = None,
    ) -> ExecutionCandidate:
        """Factory: create revision-0 candidate from intent and trade plan data."""
        return cls(
            candidate_id=_new_candidate_id(),
            revision=0,
            intent_id=intent_id,
            trade_plan_id=trade_plan_id,
            symbol=symbol,
            action=TradeAction(action),
            quantity=quantity,
            entry_price=entry_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            planned_exit_source=planned_exit_source,
            notional=notional,
            reason=reason,
            metadata=metadata or {},
        )
