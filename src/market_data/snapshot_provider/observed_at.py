"""Observation times: the clock the snapshot engine judges freshness with."""

from __future__ import annotations

import time
from collections.abc import Mapping
from math import isfinite
from typing import Any

# Metadata key carrying the observation time in the Governor's own clock.
DOMAIN_OBSERVED_AT_KEY = "alphora_observed_at"
CONTEXT_NOW_KEY = "alphora_now"


def context_now(context: Any) -> float:
    """The cycle reference time of a resolution context, or its request time."""

    metadata = getattr(context, "metadata", {}) or {}
    try:
        return float(metadata.get(CONTEXT_NOW_KEY, context.requested_at))
    except (TypeError, ValueError):
        return float(context.requested_at)


def coerce_now(now: float | None) -> float:
    if now is None:
        return time.time()
    return float(now)


def domain_observed_at(item: Any) -> float:
    """When the Governor observed a resolved resource (falls back to the engine's time)."""

    metadata = getattr(item, "metadata", {}) or {}
    try:
        return float(metadata.get(DOMAIN_OBSERVED_AT_KEY, item.observed_at))
    except (TypeError, ValueError):
        return float(item.observed_at)


def normalize_observed_at(
    *,
    domain_observed_at: float | int | str | None,
    reference_time: float | None = None,
) -> float:
    """Normalize epoch units without rewriting legitimately old observations.

    Values in milliseconds, microseconds, or nanoseconds are converted to seconds. Invalid values
    use the supplied reference time, but valid old values remain old so the engine's freshness
    policies can reject them instead of accidentally treating them as current.
    """

    fallback = time.time() if reference_time is None else float(reference_time)
    if domain_observed_at is None:
        return fallback
    try:
        normalized = float(domain_observed_at)
    except (TypeError, ValueError):
        return fallback
    if not isfinite(normalized) or normalized <= 0:
        return fallback
    while abs(normalized) >= 100_000_000_000:
        normalized /= 1000.0
    return normalized


def mapping_observed_at(
    response: Any,
    *,
    fields: tuple[str, ...],
    fallback: float,
) -> float:
    if isinstance(response, Mapping):
        for field_name in fields:
            candidate = response.get(field_name)
            if candidate is None:
                continue
            return normalize_observed_at(domain_observed_at=candidate, reference_time=fallback)
    return float(fallback)


def event_observed_at(event: Mapping[str, Any]) -> float:
    """Exchange time of a User Data Stream event (transaction, then event, then receipt)."""

    fallback = normalize_observed_at(
        domain_observed_at=event.get("received_at"),
        reference_time=time.time(),
    )
    for key in ("transaction_time", "event_time", "received_at"):
        value = event.get(key)
        if value is None:
            continue
        return normalize_observed_at(domain_observed_at=value, reference_time=fallback)
    return fallback


__all__ = [
    "CONTEXT_NOW_KEY",
    "DOMAIN_OBSERVED_AT_KEY",
    "coerce_now",
    "context_now",
    "domain_observed_at",
    "event_observed_at",
    "mapping_observed_at",
    "normalize_observed_at",
]
