"""Validated settings of the smart execution and smart sizing modules.

``AppConfig`` embeds these settings, so they belong to ``config``. The engines
that consume them stay in ``governor.smart_execution`` and ``governor.smart_sizing``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

DEFAULT_MAX_SPREAD_BPS = Decimal("5")
DEFAULT_MAX_PRICE_DRIFT_BPS = Decimal("10")
DEFAULT_MAX_DATA_AGE_SECONDS = Decimal("3")
DEFAULT_LIMIT_ENTRY_TIMEOUT_SECONDS = Decimal("3")


@dataclass(frozen=True)
class SmartExecutionSettings:
    max_spread_bps: Any = DEFAULT_MAX_SPREAD_BPS
    max_price_drift_bps: Any = DEFAULT_MAX_PRICE_DRIFT_BPS
    max_market_data_age_seconds: Any = DEFAULT_MAX_DATA_AGE_SECONDS
    limit_entry_enabled: bool = False
    limit_entry_timeout_seconds: Any = DEFAULT_LIMIT_ENTRY_TIMEOUT_SECONDS
    limit_entry_fallback_to_market: bool = False
    limit_entry_post_only: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "max_spread_bps",
            "max_price_drift_bps",
            "max_market_data_age_seconds",
            "limit_entry_timeout_seconds",
        ):
            value = _setting_decimal(getattr(self, field_name), field_name)
            if value < 0:
                raise ValueError(f"{field_name} precisa ser maior ou igual a zero")
            object.__setattr__(self, field_name, value)

        object.__setattr__(self, "limit_entry_enabled", bool(self.limit_entry_enabled))
        object.__setattr__(
            self,
            "limit_entry_fallback_to_market",
            bool(self.limit_entry_fallback_to_market),
        )
        object.__setattr__(
            self,
            "limit_entry_post_only",
            bool(self.limit_entry_post_only),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_spread_bps": str(self.max_spread_bps),
            "max_price_drift_bps": str(self.max_price_drift_bps),
            "max_market_data_age_seconds": str(self.max_market_data_age_seconds),
            "limit_entry_enabled": self.limit_entry_enabled,
            "limit_entry_timeout_seconds": str(self.limit_entry_timeout_seconds),
            "limit_entry_fallback_to_market": self.limit_entry_fallback_to_market,
            "limit_entry_post_only": self.limit_entry_post_only,
        }


FULL_SIZE_MULTIPLIER = Decimal("1")
MEDIUM_QUALITY_MULTIPLIER = Decimal("0.75")
LOW_QUALITY_MULTIPLIER = Decimal("0.50")
HIGH_RISK_MULTIPLIER = Decimal("0.25")

MEDIUM_REWARD_RISK = Decimal("1.50")
LOW_REWARD_RISK = Decimal("1.00")
HIGH_COST_TO_REWARD_RATIO = Decimal("0.50")
ELEVATED_COST_TO_REWARD_RATIO = Decimal("0.30")
LOW_SCORE_THRESHOLD = Decimal("50")
MEDIUM_SCORE_THRESHOLD = Decimal("70")
ELEVATED_CONSECUTIVE_LOSSES = 2
HIGH_CONSECUTIVE_LOSSES = 3
ELEVATED_DRAWDOWN = Decimal("0.05")
HIGH_DRAWDOWN = Decimal("0.10")


class SmartSizingMinNotionalPolicy(str, Enum):
    BLOCK = "BLOCK"
    FLOOR_TO_MIN = "FLOOR_TO_MIN"
    FALLBACK_TO_BASE = "FALLBACK_TO_BASE"

    @classmethod
    def normalize(cls, value: SmartSizingMinNotionalPolicy | str | None):
        if isinstance(value, cls):
            return value
        normalized = str(value or cls.BLOCK.value).upper().strip()
        try:
            return cls(normalized)
        except ValueError:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(
                f"smart_sizing min_notional_policy invalido: {normalized}. "
                f"Valores aceitos: {allowed}."
            ) from None


@dataclass(frozen=True)
class SmartSizingSettings:
    full_size_multiplier: Any = FULL_SIZE_MULTIPLIER
    medium_quality_multiplier: Any = MEDIUM_QUALITY_MULTIPLIER
    low_quality_multiplier: Any = LOW_QUALITY_MULTIPLIER
    high_risk_multiplier: Any = HIGH_RISK_MULTIPLIER
    medium_reward_risk: Any = MEDIUM_REWARD_RISK
    low_reward_risk: Any = LOW_REWARD_RISK
    elevated_cost_to_reward_ratio: Any = ELEVATED_COST_TO_REWARD_RATIO
    high_cost_to_reward_ratio: Any = HIGH_COST_TO_REWARD_RATIO
    low_score_threshold: Any = LOW_SCORE_THRESHOLD
    medium_score_threshold: Any = MEDIUM_SCORE_THRESHOLD
    elevated_consecutive_losses: int = ELEVATED_CONSECUTIVE_LOSSES
    high_consecutive_losses: int = HIGH_CONSECUTIVE_LOSSES
    elevated_drawdown: Any = ELEVATED_DRAWDOWN
    high_drawdown: Any = HIGH_DRAWDOWN
    min_notional_policy: SmartSizingMinNotionalPolicy | str = SmartSizingMinNotionalPolicy.BLOCK

    def __post_init__(self) -> None:
        for field_name in (
            "full_size_multiplier",
            "medium_quality_multiplier",
            "low_quality_multiplier",
            "high_risk_multiplier",
            "medium_reward_risk",
            "low_reward_risk",
            "elevated_cost_to_reward_ratio",
            "high_cost_to_reward_ratio",
            "low_score_threshold",
            "medium_score_threshold",
            "elevated_drawdown",
            "high_drawdown",
        ):
            value = _setting_decimal(getattr(self, field_name), field_name)
            if value < 0:
                raise ValueError(f"{field_name} precisa ser maior ou igual a zero")
            object.__setattr__(self, field_name, value)

        for field_name in ("elevated_consecutive_losses", "high_consecutive_losses"):
            int_value = int(getattr(self, field_name) or 0)
            if int_value < 0:
                raise ValueError(f"{field_name} precisa ser maior ou igual a zero")
            object.__setattr__(self, field_name, int_value)

        object.__setattr__(
            self,
            "min_notional_policy",
            SmartSizingMinNotionalPolicy.normalize(self.min_notional_policy),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_size_multiplier": str(self.full_size_multiplier),
            "medium_quality_multiplier": str(self.medium_quality_multiplier),
            "low_quality_multiplier": str(self.low_quality_multiplier),
            "high_risk_multiplier": str(self.high_risk_multiplier),
            "medium_reward_risk": str(self.medium_reward_risk),
            "low_reward_risk": str(self.low_reward_risk),
            "elevated_cost_to_reward_ratio": str(self.elevated_cost_to_reward_ratio),
            "high_cost_to_reward_ratio": str(self.high_cost_to_reward_ratio),
            "low_score_threshold": str(self.low_score_threshold),
            "medium_score_threshold": str(self.medium_score_threshold),
            "elevated_consecutive_losses": self.elevated_consecutive_losses,
            "high_consecutive_losses": self.high_consecutive_losses,
            "elevated_drawdown": str(self.elevated_drawdown),
            "high_drawdown": str(self.high_drawdown),
            "min_notional_policy": SmartSizingMinNotionalPolicy.normalize(
                self.min_notional_policy
            ).value,
        }


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not decimal.is_finite():
        return None
    return decimal


def _setting_decimal(value: Any, name: str) -> Decimal:
    decimal = _decimal_or_none(value)
    if decimal is None:
        raise ValueError(f"{name} precisa ser um numero decimal valido")
    return decimal


__all__ = [
    "DEFAULT_MAX_SPREAD_BPS",
    "DEFAULT_MAX_PRICE_DRIFT_BPS",
    "DEFAULT_MAX_DATA_AGE_SECONDS",
    "DEFAULT_LIMIT_ENTRY_TIMEOUT_SECONDS",
    "FULL_SIZE_MULTIPLIER",
    "MEDIUM_QUALITY_MULTIPLIER",
    "LOW_QUALITY_MULTIPLIER",
    "HIGH_RISK_MULTIPLIER",
    "MEDIUM_REWARD_RISK",
    "LOW_REWARD_RISK",
    "HIGH_COST_TO_REWARD_RATIO",
    "ELEVATED_COST_TO_REWARD_RATIO",
    "LOW_SCORE_THRESHOLD",
    "MEDIUM_SCORE_THRESHOLD",
    "ELEVATED_CONSECUTIVE_LOSSES",
    "HIGH_CONSECUTIVE_LOSSES",
    "ELEVATED_DRAWDOWN",
    "HIGH_DRAWDOWN",
    "SmartExecutionSettings",
    "SmartSizingMinNotionalPolicy",
    "SmartSizingSettings",
]
