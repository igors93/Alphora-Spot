from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.regime_policy_modes import (
    REGIME_MODE_BLOCK_COUNTER_TREND,
    REGIME_MODE_BLOCK_STRONG_COUNTER_TREND,
    REGIME_MODE_CONSERVATIVE,
    REGIME_MODE_OBSERVE,
    VALID_REGIME_POLICY_MODES,
    normalize_regime_policy_mode,
)
from futures.trend.context import (
    MarketRegime,
    TrendContext,
    TrendDirection,
    TrendStrength,
)

OPEN_LONG = "OPEN_LONG"
OPEN_SHORT = "OPEN_SHORT"


@dataclass(frozen=True)
class RegimePolicyDecision:
    allowed: bool
    reason: str
    mode: str
    against_trend: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "mode": self.mode,
            "against_trend": self.against_trend,
            "diagnostics": dict(self.diagnostics),
        }


def evaluate_regime_policy(
    *,
    action: Any,
    trend_context: TrendContext | None,
    mode: Any,
) -> RegimePolicyDecision:
    normalized_mode = normalize_regime_policy_mode(mode)
    normalized_action = str(getattr(action, "value", action) or "").upper().strip()

    if normalized_action not in {OPEN_LONG, OPEN_SHORT}:
        return RegimePolicyDecision(
            allowed=True,
            reason="REGIME_POLICY_NOT_AN_OPEN_ACTION",
            mode=normalized_mode,
        )

    if trend_context is None:
        return RegimePolicyDecision(
            allowed=True,
            reason="REGIME_CONTEXT_UNAVAILABLE",
            mode=normalized_mode,
            diagnostics={"context_available": False},
        )

    direction = trend_context.direction
    strength = trend_context.strength
    regime = trend_context.regime
    against_trend = (normalized_action == OPEN_LONG and direction is TrendDirection.DOWN) or (
        normalized_action == OPEN_SHORT and direction is TrendDirection.UP
    )
    diagnostics = {
        "context_available": True,
        "action": normalized_action,
        "trend_direction": direction.value,
        "trend_strength": strength.value,
        "market_regime": regime.value,
        "trend_confidence": float(trend_context.confidence),
        "against_trend": against_trend,
    }

    if normalized_mode == REGIME_MODE_OBSERVE:
        return RegimePolicyDecision(
            allowed=True,
            reason="REGIME_POLICY_OBSERVED",
            mode=normalized_mode,
            against_trend=against_trend,
            diagnostics=diagnostics,
        )

    block = False
    reason = "REGIME_POLICY_APPROVED"
    if normalized_mode == REGIME_MODE_BLOCK_STRONG_COUNTER_TREND:
        block = against_trend and strength is TrendStrength.STRONG
        if block:
            reason = "STRONG_COUNTER_TREND_ENTRY_BLOCKED"
    elif normalized_mode == REGIME_MODE_BLOCK_COUNTER_TREND:
        block = against_trend
        if block:
            reason = "COUNTER_TREND_ENTRY_BLOCKED"
    elif normalized_mode == REGIME_MODE_CONSERVATIVE:
        block = (
            against_trend and strength in {TrendStrength.MODERATE, TrendStrength.STRONG}
        ) or regime is MarketRegime.HIGH_VOLATILITY
        if block:
            reason = (
                "HIGH_VOLATILITY_ENTRY_BLOCKED"
                if regime is MarketRegime.HIGH_VOLATILITY
                else "COUNTER_TREND_ENTRY_BLOCKED"
            )

    return RegimePolicyDecision(
        allowed=not block,
        reason=reason,
        mode=normalized_mode,
        against_trend=against_trend,
        diagnostics=diagnostics,
    )


__all__ = [
    "REGIME_MODE_BLOCK_COUNTER_TREND",
    "REGIME_MODE_BLOCK_STRONG_COUNTER_TREND",
    "REGIME_MODE_CONSERVATIVE",
    "REGIME_MODE_OBSERVE",
    "RegimePolicyDecision",
    "VALID_REGIME_POLICY_MODES",
    "evaluate_regime_policy",
    "normalize_regime_policy_mode",
]
