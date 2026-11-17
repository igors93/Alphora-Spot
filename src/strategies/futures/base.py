from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from domain.models import FuturesPosition, MarketSnapshot, TradeDecision
from domain.strategy_requirements import (
    StrategyDataRequirements,
    requirements_from_legacy_metadata,
)

__all__ = [
    "FuturesStrategy",
    "FuturesStrategyV2",
    "StrategyMetadata",
    "StrategyDataRequirements",
    "requirements_from_legacy_metadata",
]


@dataclass(frozen=True)
class StrategyMetadata:
    strategy_name: str = ""
    supports_scoring: bool = False
    supports_long: bool = True
    supports_short: bool = False
    required_history: int = 0
    required_timeframes: list[str] = field(default_factory=list)
    required_indicators: list[str | dict[str, Any]] = field(default_factory=list)

    def to_data_requirements(self) -> StrategyDataRequirements:
        return requirements_from_legacy_metadata(self)


class FuturesStrategy(ABC):
    @abstractmethod
    def score_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ):
        pass

    @abstractmethod
    def decide_symbol(
        self,
        symbol: str,
        market: MarketSnapshot,
        position: FuturesPosition,
        state: dict[str, Any],
    ) -> TradeDecision:
        pass


class FuturesStrategyV2(FuturesStrategy):
    metadata = StrategyMetadata()

    def data_requirements(self) -> StrategyDataRequirements:
        """Return data requirements for this strategy instance.

        Override this method to declare custom timeframes or evaluation policy.
        By default, derived from class-level metadata (backward-compat).
        """
        return self.metadata.to_data_requirements()

    def requirements_for_symbol(
        self,
        symbol: str,
        symbol_config: Any | None = None,
    ) -> StrategyDataRequirements:
        """Return data requirements for a specific symbol.

        Override in subclasses to provide per-symbol dynamic requirements
        (e.g. reading the actual loaded model's interval and history).
        Default: delegates to data_requirements() — same for all symbols.
        """
        return self.data_requirements()

    @classmethod
    def describe_parameters(cls) -> dict[str, Any]:
        return {}

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "FuturesStrategyV2":
        return cls(**params)
