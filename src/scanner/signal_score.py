from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from time import time
from typing import Any

ScoreValue = int | float | Decimal


class SignalPriority(Enum):
    COLD = "COLD"
    WARM = "WARM"
    HOT = "HOT"
    READY = "READY"


DEFAULT_REASON = "Score sem motivo informado"
MIN_SCORE = 0
MAX_SCORE = 100


def clamp_score(score: ScoreValue) -> int:
    return max(MIN_SCORE, min(MAX_SCORE, int(score)))


def priority_from_score(score: ScoreValue) -> SignalPriority:
    normalized = clamp_score(score)
    if normalized <= 39:
        return SignalPriority.COLD
    if normalized <= 69:
        return SignalPriority.WARM
    if normalized <= 89:
        return SignalPriority.HOT
    return SignalPriority.READY


def default_next_check_seconds(priority: SignalPriority) -> float:
    intervals = {
        SignalPriority.COLD: 90.0,
        SignalPriority.WARM: 5.0,
        SignalPriority.HOT: 2.0,
        SignalPriority.READY: 0.0,
    }
    return intervals[priority]


def normalize_priority(priority: SignalPriority | str) -> SignalPriority:
    if isinstance(priority, SignalPriority):
        return priority
    return SignalPriority(str(priority).upper())


@dataclass(frozen=True)
class SignalScore:
    symbol: str
    score: ScoreValue
    priority: SignalPriority | None = None
    reason: str = DEFAULT_REASON
    suggested_next_check_seconds: float | int | None = None
    data_age_seconds: float | int = 0
    action: str | None = None
    confidence: ScoreValue | None = None
    strategy: str | None = None
    timestamp: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        symbol = self.symbol.upper().strip()
        if not symbol:
            raise ValueError("SignalScore symbol nao pode ficar vazio")

        normalized_score = clamp_score(self.score)
        expected_priority = priority_from_score(normalized_score)
        priority = (
            normalize_priority(self.priority) if self.priority is not None else expected_priority
        )
        if priority is not expected_priority:
            raise ValueError(
                "SignalScore priority incoerente com score: "
                f"score={normalized_score} priority={priority.value}"
            )

        confidence = None
        if self.confidence is not None:
            confidence = clamp_score(self.confidence)

        data_age_seconds = max(0.0, float(self.data_age_seconds))
        next_check = self.suggested_next_check_seconds
        if next_check is None:
            next_check = default_next_check_seconds(priority)
        metadata = self.metadata if isinstance(self.metadata, dict) else {}

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "score", normalized_score)
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "reason", self.reason.strip() or DEFAULT_REASON)
        object.__setattr__(self, "suggested_next_check_seconds", max(0.0, float(next_check)))
        object.__setattr__(self, "data_age_seconds", data_age_seconds)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(
            self, "timestamp", self.timestamp if self.timestamp is not None else time()
        )
        object.__setattr__(self, "metadata", dict(metadata))


def cold_score(symbol: str, *, reason: str, strategy: str | None = None) -> SignalScore:
    return SignalScore(symbol=symbol, score=0, reason=reason, strategy=strategy)
