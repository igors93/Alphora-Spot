"""Environment of the shared Binance REST weight count (OPT-036, ADR-005).

``ALPHORA_REST_WEIGHT_GUARD_MODE`` decides what the per-cycle heavy-check budget does when the
used weight reported by Binance reaches ``ALPHORA_REST_WEIGHT_GUARD_RATIO`` of
``ALPHORA_REST_WEIGHT_LIMIT_1M``: ``OFF`` ignores it, ``OBSERVE`` (default) records an audit
event, ``ENFORCE`` postpones heavy checks of symbols without an open position.
"""

from __future__ import annotations

import os
from math import isfinite
from typing import TypedDict

GUARD_MODE_ENV = "ALPHORA_REST_WEIGHT_GUARD_MODE"
GUARD_RATIO_ENV = "ALPHORA_REST_WEIGHT_GUARD_RATIO"
WEIGHT_LIMIT_ENV = "ALPHORA_REST_WEIGHT_LIMIT_1M"

GUARD_MODES = ("OFF", "OBSERVE", "ENFORCE")
DEFAULT_GUARD_MODE = "OBSERVE"
DEFAULT_GUARD_RATIO = 0.8
DEFAULT_WEIGHT_LIMIT_1M = 2400


class RestWeightSettingsFields(TypedDict):
    rest_weight_guard_mode: str
    rest_weight_guard_ratio: float
    rest_weight_limit_1m: int


def load_rest_weight_settings() -> RestWeightSettingsFields:
    return {
        "rest_weight_guard_mode": _text(GUARD_MODE_ENV, DEFAULT_GUARD_MODE).upper(),
        "rest_weight_guard_ratio": _ratio(),
        "rest_weight_limit_1m": _limit(),
    }


def validate_rest_weight_settings(*, mode: str, ratio: float, limit: int) -> None:
    if mode not in GUARD_MODES:
        raise ValueError(f"{GUARD_MODE_ENV} precisa ser um de {', '.join(GUARD_MODES)}")
    if not isfinite(ratio) or not 0 < ratio <= 1:
        raise ValueError(f"{GUARD_RATIO_ENV} precisa ficar entre 0 (exclusivo) e 1")
    if limit < 1:
        raise ValueError(f"{WEIGHT_LIMIT_ENV} precisa ser no minimo 1")


def _text(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None or not raw.strip() else raw.strip()


def _ratio() -> float:
    raw = _text(GUARD_RATIO_ENV, str(DEFAULT_GUARD_RATIO))
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{GUARD_RATIO_ENV} precisa ser um numero") from error


def _limit() -> int:
    raw = _text(WEIGHT_LIMIT_ENV, str(DEFAULT_WEIGHT_LIMIT_1M))
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{WEIGHT_LIMIT_ENV} precisa ser um numero inteiro") from error


__all__ = [
    "DEFAULT_GUARD_MODE",
    "DEFAULT_GUARD_RATIO",
    "DEFAULT_WEIGHT_LIMIT_1M",
    "GUARD_MODES",
    "RestWeightSettingsFields",
    "load_rest_weight_settings",
    "validate_rest_weight_settings",
]
