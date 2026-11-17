"""Modes of the trend regime policy (``trend_context_mode``).

They are configuration values validated at load time, so they live in ``config``.
The policy that applies them is ``futures.trend.regime_policy``.
"""

from __future__ import annotations

from typing import Any

REGIME_MODE_OBSERVE = "OBSERVE"
REGIME_MODE_BLOCK_STRONG_COUNTER_TREND = "BLOCK_STRONG_COUNTER_TREND"
REGIME_MODE_BLOCK_COUNTER_TREND = "BLOCK_COUNTER_TREND"
REGIME_MODE_CONSERVATIVE = "CONSERVATIVE"

VALID_REGIME_POLICY_MODES = {
    REGIME_MODE_OBSERVE,
    REGIME_MODE_BLOCK_STRONG_COUNTER_TREND,
    REGIME_MODE_BLOCK_COUNTER_TREND,
    REGIME_MODE_CONSERVATIVE,
}


def normalize_regime_policy_mode(value: Any) -> str:
    mode = str(value or REGIME_MODE_OBSERVE).upper().strip()
    if mode not in VALID_REGIME_POLICY_MODES:
        allowed = ", ".join(sorted(VALID_REGIME_POLICY_MODES))
        raise ValueError(f"trend_context_mode invalido: {mode}. Valores aceitos: {allowed}")
    return mode


__all__ = [
    "REGIME_MODE_BLOCK_COUNTER_TREND",
    "REGIME_MODE_BLOCK_STRONG_COUNTER_TREND",
    "REGIME_MODE_CONSERVATIVE",
    "REGIME_MODE_OBSERVE",
    "VALID_REGIME_POLICY_MODES",
    "normalize_regime_policy_mode",
]
