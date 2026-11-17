from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from time import time
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from scanner.signal_score import SignalPriority
from trading.enums import SUPPORTED_ACTIONS


def _new_intent_id() -> str:
    return f"ti_{uuid4().hex}"


@dataclass(frozen=True, init=False)
class TradeIntent:
    symbol: str
    action: str
    confidence: int
    reason: str
    strategy_name: str
    score: int
    priority: SignalPriority
    market_price: Decimal | None
    suggested_next_check_seconds: float
    created_at: float
    intent_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        symbol: str,
        action: str,
        confidence: int | float | Decimal | None = None,
        reason: str = "",
        strategy_name: str = "",
        metadata: dict[str, Any] | None = None,
        *,
        score: int | float | Decimal | None = None,
        priority: SignalPriority | str | None = None,
        market_price: Decimal | str | int | float | None = None,
        suggested_next_check_seconds: float | int | None = None,
        created_at: float | int | None = None,
        intent_id: str | None = None,
    ) -> None:
        normalized_symbol = str(symbol or "").upper().strip()
        normalized_action = str(action or "").upper().strip() or "HOLD"
        normalized_score = _clamp_score(score if score is not None else confidence)
        normalized_confidence = _clamp_score(
            confidence if confidence is not None else normalized_score
        )
        normalized_reason = str(reason or "").strip() or "Intent sem motivo informado"
        normalized_strategy = str(strategy_name or "").strip() or "unknown_strategy"
        normalized_priority = _normalize_priority(priority, normalized_score)
        normalized_market_price = _decimal_or_none(market_price)
        normalized_next_check = max(0.0, float(suggested_next_check_seconds or 0.0))
        normalized_created_at = float(created_at if created_at is not None else time())
        normalized_intent_id = str(intent_id or "").strip() or _new_intent_id()

        if not normalized_symbol:
            raise ValueError("TradeIntent symbol nao pode ficar vazio")
        if normalized_action not in SUPPORTED_ACTIONS:
            raise ValueError(f"TradeIntent action nao suportada: {normalized_action}")

        object.__setattr__(self, "symbol", normalized_symbol)
        object.__setattr__(self, "action", normalized_action)
        object.__setattr__(self, "confidence", normalized_confidence)
        object.__setattr__(self, "reason", normalized_reason)
        object.__setattr__(self, "strategy_name", normalized_strategy)
        object.__setattr__(self, "score", normalized_score)
        object.__setattr__(self, "priority", normalized_priority)
        object.__setattr__(self, "market_price", normalized_market_price)
        object.__setattr__(self, "suggested_next_check_seconds", normalized_next_check)
        object.__setattr__(self, "created_at", normalized_created_at)
        object.__setattr__(self, "intent_id", normalized_intent_id)
        object.__setattr__(self, "metadata", _freeze_metadata(metadata or {}))

    def with_updates(self, **updates: Any) -> "TradeIntent":
        next_action = str(updates.get("action", self.action) or "").upper().strip()
        next_intent_id = updates.get("intent_id", self.intent_id)
        if next_action != self.action and next_intent_id == self.intent_id:
            if "intent_id" in updates:
                raise ValueError("Nao reutilize intent_id para uma acao diferente")
            next_intent_id = _new_intent_id()
        return TradeIntent(
            symbol=updates.get("symbol", self.symbol),
            action=next_action,
            confidence=updates.get("confidence", self.confidence),
            reason=updates.get("reason", self.reason),
            strategy_name=updates.get("strategy_name", self.strategy_name),
            metadata=updates.get("metadata", dict(self.metadata)),
            score=updates.get("score", self.score),
            priority=updates.get("priority", self.priority),
            market_price=updates.get("market_price", self.market_price),
            suggested_next_check_seconds=updates.get(
                "suggested_next_check_seconds",
                self.suggested_next_check_seconds,
            ),
            created_at=updates.get("created_at", self.created_at),
            intent_id=next_intent_id,
        )


def _clamp_score(value: int | float | Decimal | None) -> int:
    if value is None:
        return 0
    return max(0, min(100, int(value)))


def _normalize_priority(priority: SignalPriority | str | None, score: int) -> SignalPriority:
    if isinstance(priority, SignalPriority):
        return priority
    if priority is not None:
        return SignalPriority(str(priority).upper())
    if score <= 39:
        return SignalPriority.COLD
    if score <= 69:
        return SignalPriority.WARM
    if score <= 89:
        return SignalPriority.HOT
    return SignalPriority.READY


def _decimal_or_none(value: Decimal | str | int | float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _freeze_metadata(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})


def metadata_to_plain_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _thaw_value(item) for key, item in value.items()}


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_metadata(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return metadata_to_plain_dict(value)
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    if isinstance(value, frozenset):
        return [_thaw_value(item) for item in value]
    return value
