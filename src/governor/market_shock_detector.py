from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class MarketShockEvent:
    symbol: str
    direction: str
    severity: str
    reason: str
    price: str
    reference_price: str
    move_bps: float
    window_seconds: float
    observed_seconds: float
    event_time: float | None
    received_at: float
    shadow_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "severity": self.severity,
            "reason": self.reason,
            "price": self.price,
            "reference_price": self.reference_price,
            "move_bps": self.move_bps,
            "window_seconds": self.window_seconds,
            "observed_seconds": self.observed_seconds,
            "event_time": self.event_time,
            "received_at": self.received_at,
            "shadow_only": self.shadow_only,
        }


class MarketShockDetector:
    def __init__(
        self,
        *,
        window_seconds: float = 3.0,
        move_bps_threshold: float = 50.0,
        critical_bps_threshold: float = 120.0,
        cooldown_seconds: float = 10.0,
        min_samples: int = 2,
        max_pending_events: int = 200,
    ):
        if window_seconds <= 0:
            raise ValueError("window_seconds precisa ser maior que zero")
        if move_bps_threshold <= 0:
            raise ValueError("move_bps_threshold precisa ser maior que zero")
        if critical_bps_threshold < move_bps_threshold:
            raise ValueError("critical_bps_threshold precisa ser maior ou igual ao threshold")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds nao pode ser negativo")
        if min_samples < 2:
            raise ValueError("min_samples precisa ser no minimo 2")
        if max_pending_events < 1:
            raise ValueError("max_pending_events precisa ser no minimo 1")

        self.window_seconds = float(window_seconds)
        self.move_bps_threshold = float(move_bps_threshold)
        self.critical_bps_threshold = float(critical_bps_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self.min_samples = int(min_samples)
        self._history: dict[str, deque[tuple[float, Decimal]]] = defaultdict(deque)
        self._last_event_at: dict[str, float] = {}
        self._events: deque[MarketShockEvent] = deque(maxlen=max_pending_events)

    @property
    def pending_count(self) -> int:
        return len(self._events)

    def evaluate_snapshot(self, snapshot: Any) -> MarketShockEvent | None:
        symbol = str(getattr(snapshot, "symbol", "")).upper().strip()
        if not symbol:
            return None

        price = _decimal_or_none(getattr(snapshot, "price", None))
        if price is None or price <= 0:
            return None

        received_at = float(getattr(snapshot, "received_at", 0.0) or 0.0)
        event_time = getattr(snapshot, "event_time", None)

        history = self._history[symbol]
        history.append((received_at, price))
        self._prune_history(history, received_at)

        if len(history) < self.min_samples:
            return None

        reference_time, reference_price = history[0]
        if reference_price <= 0:
            return None

        observed_seconds = max(0.0, received_at - reference_time)
        if observed_seconds <= 0:
            return None

        move_bps_decimal = ((price - reference_price) / reference_price) * Decimal("10000")
        move_bps = float(move_bps_decimal)

        if abs(move_bps) < self.move_bps_threshold:
            return None

        last_event_at = self._last_event_at.get(symbol)
        if last_event_at is not None and received_at - last_event_at < self.cooldown_seconds:
            return None

        direction = "DROP" if move_bps < 0 else "RISE"
        severity = "HIGH" if abs(move_bps) >= self.critical_bps_threshold else "MEDIUM"
        reason = f"{direction}_{round(abs(move_bps), 2)}BPS_IN_{round(observed_seconds, 3)}S"

        event = MarketShockEvent(
            symbol=symbol,
            direction=direction,
            severity=severity,
            reason=reason,
            price=str(price),
            reference_price=str(reference_price),
            move_bps=round(move_bps, 6),
            window_seconds=self.window_seconds,
            observed_seconds=round(observed_seconds, 6),
            event_time=event_time,
            received_at=received_at,
        )

        self._last_event_at[symbol] = received_at
        self._events.append(event)
        return event

    def drain_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            return []

        events: list[dict[str, Any]] = []
        while self._events and len(events) < limit:
            events.append(self._events.popleft().to_dict())

        return events

    def _prune_history(
        self,
        history: deque[tuple[float, Decimal]],
        now: float,
    ) -> None:
        cutoff = now - self.window_seconds
        while history and history[0][0] < cutoff:
            history.popleft()


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None

    if not decimal.is_finite():
        return None

    return decimal
