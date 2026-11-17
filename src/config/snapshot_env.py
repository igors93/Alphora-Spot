"""Environment variables of the operational snapshot provider and of the candle source.

ADR-002: the documented names are ``ALPHORA_SNAPSHOT_*``. The names used before migration M5
are still read, so existing ``.env`` files keep working; a new name wins when both are set.
ADR-004: candles come from the endpoint in ``ALPHORA_CANDLE_KLINES_URL``, production by default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from math import isfinite
from typing import TypedDict, cast
from urllib.parse import urlsplit

ENV_PREFIX = "ALPHORA_SNAPSHOT_"
LEGACY_ENV_PREFIX = "ALPHORA_COALESTRA_"  # legacy-wire-name (ADR-002)

FALLBACK_ALLOWED_ENV = "ALPHORA_SNAPSHOT_FALLBACK_ALLOWED"
CANDLE_KLINES_URL_ENV = "ALPHORA_CANDLE_KLINES_URL"
DEFAULT_CANDLE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
_KLINES_PATH = "/fapi/v1/klines"


@dataclass(frozen=True)
class _Setting:
    field: str
    suffix: str
    default: float
    kind: str  # "positive", "non_negative" or "count"

    @property
    def env(self) -> str:
        return f"{ENV_PREFIX}{self.suffix}"

    @property
    def legacy_env(self) -> str:
        return f"{LEGACY_ENV_PREFIX}{self.suffix}"


SNAPSHOT_SETTINGS: tuple[_Setting, ...] = (
    _Setting("snapshot_deadline_seconds", "DEADLINE_SECONDS", 7.0, "positive"),
    _Setting("snapshot_rest_timeout_seconds", "REST_TIMEOUT_SECONDS", 6.5, "positive"),
    _Setting(
        "snapshot_rest_connect_timeout_seconds", "REST_CONNECT_TIMEOUT_SECONDS", 1.0, "positive"
    ),
    _Setting("snapshot_rest_read_timeout_seconds", "REST_READ_TIMEOUT_SECONDS", 4.5, "positive"),
    _Setting(
        "snapshot_rest_transport_timeout_seconds", "REST_TRANSPORT_TIMEOUT_SECONDS", 6.0, "positive"
    ),
    _Setting("snapshot_rest_max_attempts", "REST_MAX_ATTEMPTS", 4, "count"),
    _Setting("snapshot_rest_retry_delay_seconds", "REST_RETRY_DELAY_SECONDS", 0.05, "non_negative"),
    _Setting("snapshot_execution_max_age_seconds", "EXECUTION_MAX_AGE_SECONDS", 6.0, "positive"),
    _Setting("snapshot_execution_max_skew_seconds", "EXECUTION_MAX_SKEW_SECONDS", 5.0, "positive"),
    _Setting(
        "snapshot_evaluation_max_age_seconds", "EVALUATION_MAX_AGE_SECONDS", 45.0, "non_negative"
    ),
)


class SnapshotSettingsFields(TypedDict):
    snapshot_deadline_seconds: float
    snapshot_rest_timeout_seconds: float
    snapshot_rest_connect_timeout_seconds: float
    snapshot_rest_read_timeout_seconds: float
    snapshot_rest_transport_timeout_seconds: float
    snapshot_rest_max_attempts: int
    snapshot_rest_retry_delay_seconds: float
    snapshot_execution_max_age_seconds: float
    snapshot_execution_max_skew_seconds: float
    snapshot_evaluation_max_age_seconds: float
    snapshot_fallback_allowed: bool | None


def snapshot_env_name(field: str) -> str:
    """The documented environment variable of a snapshot ``AppConfig`` field."""

    for setting in SNAPSHOT_SETTINGS:
        if setting.field == field:
            return setting.env
    raise KeyError(field)


def load_snapshot_settings() -> SnapshotSettingsFields:
    """The snapshot fields of ``AppConfig``, read from the new names, then the legacy ones."""

    values: dict[str, float | int | bool | None] = {
        setting.field: _read(setting) for setting in SNAPSHOT_SETTINGS
    }
    values["snapshot_fallback_allowed"] = load_snapshot_fallback_allowed()
    return cast(SnapshotSettingsFields, values)


def load_snapshot_fallback_allowed() -> bool | None:
    """Whether a run may start without the provider; unset lets the execution mode decide.

    Live modes always require the provider; the flag only matters for paper and dry runs.
    """

    raw = os.getenv(FALLBACK_ALLOWED_ENV)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value in {"true", "1", "yes", "y", "sim"}:
        return True
    if value in {"false", "0", "no", "n", "nao", "não"}:
        return False
    raise ValueError(f"{FALLBACK_ALLOWED_ENV} precisa ser true ou false")


def load_candle_klines_url() -> str:
    raw = os.getenv(CANDLE_KLINES_URL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_CANDLE_KLINES_URL
    return raw.strip()


def validate_candle_klines_url(url: str) -> None:
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"{CANDLE_KLINES_URL_ENV} precisa ser uma URL https")
    if parts.path.rstrip("/") != _KLINES_PATH or parts.query or parts.fragment:
        raise ValueError(
            f"{CANDLE_KLINES_URL_ENV} precisa apontar para o endpoint {_KLINES_PATH}, sem parametros"
        )


def _raw_value(setting: _Setting) -> tuple[str, str] | None:
    for name in (setting.env, setting.legacy_env):
        value = os.getenv(name)
        if value is not None and value.strip():
            return name, value.strip()
    return None


def _read(setting: _Setting) -> float | int:
    found = _raw_value(setting)
    if found is None:
        return setting.default
    name, raw = found
    if setting.kind == "count":
        try:
            return int(raw)
        except ValueError as error:
            raise ValueError(f"{name} precisa ser um numero inteiro") from error
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} precisa ser um numero") from error
    if not isfinite(value):
        raise ValueError(f"{name} precisa ser um numero valido")
    if setting.kind == "positive" and value <= 0:
        raise ValueError(f"{name} precisa ser maior que zero")
    if setting.kind == "non_negative" and value < 0:
        raise ValueError(f"{name} precisa ser maior ou igual a zero")
    return value


__all__ = [
    "CANDLE_KLINES_URL_ENV",
    "DEFAULT_CANDLE_KLINES_URL",
    "ENV_PREFIX",
    "FALLBACK_ALLOWED_ENV",
    "LEGACY_ENV_PREFIX",
    "SNAPSHOT_SETTINGS",
    "SnapshotSettingsFields",
    "load_candle_klines_url",
    "load_snapshot_fallback_allowed",
    "load_snapshot_settings",
    "snapshot_env_name",
    "validate_candle_klines_url",
]
