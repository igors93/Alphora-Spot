"""Order lifecycle contract used by the execution migration (M4).

The lifecycle is a pure value: callers must persist a transition before relying
on it to authorize an external effect. This module performs no I/O or exchange
requests, and it does not turn an ambiguous result into permission to resubmit.
"""

from dataclasses import dataclass, replace
from enum import Enum


class OrderState(str, Enum):
    PLANNED = "PLANNED"
    RESERVED = "RESERVED"
    PERSISTED = "PERSISTED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PROTECTED = "PROTECTED"
    PROTECTION_FAILED = "PROTECTION_FAILED"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"


class OrderPurpose(str, Enum):
    ENTRY = "ENTRY"
    CLOSE = "CLOSE"


# A lookup from UNKNOWN can be repeated. It never authorizes another submission.
# CANCELED describes an order with no fill; a partial fill still needs protection
# or an explicit close before the position can be considered resolved.
ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PLANNED: frozenset({OrderState.RESERVED, OrderState.REJECTED}),
    OrderState.RESERVED: frozenset({OrderState.PERSISTED, OrderState.REJECTED}),
    OrderState.PERSISTED: frozenset({OrderState.SUBMITTING, OrderState.CANCELED}),
    OrderState.SUBMITTING: frozenset(
        {OrderState.ACKNOWLEDGED, OrderState.UNKNOWN, OrderState.REJECTED}
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.UNKNOWN, OrderState.CANCELED}
    ),
    OrderState.UNKNOWN: frozenset({OrderState.RECONCILING}),
    OrderState.RECONCILING: frozenset(
        {
            OrderState.UNKNOWN,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CLOSED,
            OrderState.REJECTED,
            OrderState.CANCELED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {OrderState.FILLED, OrderState.PROTECTED, OrderState.PROTECTION_FAILED}
    ),
    OrderState.FILLED: frozenset({OrderState.PROTECTED, OrderState.PROTECTION_FAILED}),
    OrderState.PROTECTION_FAILED: frozenset({OrderState.PROTECTED, OrderState.EMERGENCY_CLOSE}),
    OrderState.PROTECTED: frozenset({OrderState.CLOSING}),
    OrderState.EMERGENCY_CLOSE: frozenset({OrderState.CLOSED}),
    OrderState.CLOSING: frozenset({OrderState.CLOSED, OrderState.UNKNOWN}),
    OrderState.REJECTED: frozenset(),
    OrderState.CLOSED: frozenset(),
    OrderState.CANCELED: frozenset(),
}

ALLOWED_CLOSE_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    **ALLOWED_TRANSITIONS,
    OrderState.RECONCILING: frozenset(
        {
            OrderState.UNKNOWN,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset({OrderState.FILLED, OrderState.UNKNOWN}),
    OrderState.FILLED: frozenset({OrderState.CLOSED}),
    OrderState.PROTECTED: frozenset(),
    OrderState.PROTECTION_FAILED: frozenset(),
    OrderState.EMERGENCY_CLOSE: frozenset(),
}


class InvalidOrderTransition(ValueError):
    """The requested transition is unsafe or absent from the lifecycle contract."""


@dataclass(frozen=True)
class OrderLifecycle:
    intent_id: str
    client_order_id: str
    purpose: OrderPurpose = OrderPurpose.ENTRY
    history: tuple[OrderState, ...] = (OrderState.PLANNED,)

    def __post_init__(self) -> None:
        if not self.intent_id or not self.client_order_id:
            raise ValueError("intent_id and client_order_id are required")
        if not isinstance(self.purpose, OrderPurpose):
            raise ValueError("purpose must be an OrderPurpose")
        if not isinstance(self.history, tuple) or any(
            not isinstance(state, OrderState) for state in self.history
        ):
            raise ValueError("history must be a tuple of OrderState values")
        if not self.history or self.history[0] is not OrderState.PLANNED:
            raise InvalidOrderTransition("an order lifecycle must start at PLANNED")
        transitions = self.allowed_transitions
        for previous, current in zip(self.history, self.history[1:], strict=False):
            if current not in transitions[previous]:
                raise InvalidOrderTransition(f"{previous.value} -> {current.value}")

    @property
    def allowed_transitions(self) -> dict[OrderState, frozenset[OrderState]]:
        return (
            ALLOWED_TRANSITIONS if self.purpose is OrderPurpose.ENTRY else ALLOWED_CLOSE_TRANSITIONS
        )

    @property
    def state(self) -> OrderState:
        return self.history[-1]

    @property
    def can_submit(self) -> bool:
        return self.state is OrderState.PERSISTED

    def advance(self, next_state: OrderState) -> "OrderLifecycle":
        if not isinstance(next_state, OrderState):
            raise ValueError("next_state must be an OrderState")
        if next_state not in self.allowed_transitions[self.state]:
            raise InvalidOrderTransition(f"{self.state.value} -> {next_state.value}")
        return replace(self, history=(*self.history, next_state))
