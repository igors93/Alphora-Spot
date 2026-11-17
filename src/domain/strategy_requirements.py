from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

from domain.timeframe import normalize_timeframe

EVALUATION_EVERY_CYCLE = "EVERY_CYCLE"
EVALUATION_ON_PRIMARY_CANDLE_CLOSE = "ON_PRIMARY_CANDLE_CLOSE"
EVALUATION_ON_ANY_REQUIRED_CANDLE_CLOSE = "ON_ANY_REQUIRED_CANDLE_CLOSE"

_SUPPORTED_POLICIES = {
    EVALUATION_EVERY_CYCLE,
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    EVALUATION_ON_ANY_REQUIRED_CANDLE_CLOSE,
}


class LegacyStrategyMetadata(Protocol):
    """Fields read from an old-style strategy metadata object (``StrategyMetadata``).

    Declared structurally so market data does not depend on the strategies package.
    """

    @property
    def required_timeframes(self) -> list[str]: ...

    @property
    def required_history(self) -> int: ...


@dataclass(frozen=True)
class StrategyDataRequirements:
    primary_timeframe: str
    required_candles: dict[str, int]
    evaluation_policy: str = EVALUATION_EVERY_CYCLE
    closed_candles_only: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "primary_timeframe", normalize_timeframe(self.primary_timeframe))
        if self.primary_timeframe not in self.required_candles:
            raise ValueError(
                f"primary_timeframe '{self.primary_timeframe}' deve estar em required_candles"
            )
        if self.evaluation_policy not in _SUPPORTED_POLICIES:
            raise ValueError(f"evaluation_policy invalida: {self.evaluation_policy!r}")
        for tf, count in self.required_candles.items():
            normalize_timeframe(tf)
            if count <= 0:
                raise ValueError(f"required_candles['{tf}'] deve ser > 0")

    def with_minimum_candles(self, timeframe: str, count: int) -> StrategyDataRequirements:
        """These requirements plus at least ``count`` closed candles of ``timeframe``."""

        normalized = normalize_timeframe(timeframe)
        required = dict(self.required_candles)
        required[normalized] = max(int(required.get(normalized, 0)), int(count))
        return replace(self, required_candles=required)

    @classmethod
    def from_ai_model(cls, model: dict[str, Any]) -> StrategyDataRequirements:
        """Build requirements from an AI model dict.

        Reads from the model's ``input_contract`` when present; falls back to
        deriving from ``interval`` and ``required_history`` for legacy models.
        Always sets evaluation_policy=ON_PRIMARY_CANDLE_CLOSE.
        """
        contract = model.get("input_contract")
        if isinstance(contract, dict):
            timeframe = str(contract.get("timeframe", "")).strip() or "1m"
            required_history = max(1, int(contract.get("required_history", 20) or 20))
            closed_candles_only = bool(contract.get("closed_candles_only", True))
        else:
            timeframe = str(model.get("interval", "")).strip() or "1m"
            required_history = max(1, int(model.get("required_history", 20) or 20))
            closed_candles_only = True
        return cls(
            primary_timeframe=timeframe,
            required_candles={timeframe: required_history},
            evaluation_policy=EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
            closed_candles_only=closed_candles_only,
        )


def requirements_from_legacy_metadata(
    metadata: LegacyStrategyMetadata,
) -> StrategyDataRequirements:
    """Convert old-style StrategyMetadata without changing evaluation behavior."""
    timeframes = list(getattr(metadata, "required_timeframes", None) or ["1m"])
    primary = str(timeframes[0]).strip() if timeframes else "1m"
    history = max(1, int(getattr(metadata, "required_history", 1) or 1))
    candles: dict[str, int] = {}
    for tf in timeframes:
        normalized = str(tf).strip()
        if normalized:
            candles[normalized] = history
    if not candles or primary not in candles:
        candles[primary or "1m"] = history
    return StrategyDataRequirements(
        primary_timeframe=primary or "1m",
        required_candles=candles,
        evaluation_policy=EVALUATION_EVERY_CYCLE,
        closed_candles_only=True,
    )


def ai_input_contract_from_model(model: dict[str, Any]) -> dict[str, Any]:
    """Return or derive an AI model's input_contract.

    Returns the model's own ``input_contract`` unchanged when present.
    For legacy models without one, derives the contract from ``interval``,
    ``required_history``, and ``model_type``.
    """
    if isinstance(model.get("input_contract"), dict):
        return dict(model["input_contract"])

    timeframe = str(model.get("interval", "")).strip() or "1m"
    required_history = max(1, int(model.get("required_history", 20) or 20))
    model_type = str(model.get("model_type", "")).strip()
    schema_version = f"{model_type.lower()}_features_v1" if model_type else "unknown_features_v1"
    return {
        "timeframe": timeframe,
        "series_type": "CANDLE_CLOSE",
        "required_history": required_history,
        "closed_candles_only": True,
        "feature_schema_version": schema_version,
    }
