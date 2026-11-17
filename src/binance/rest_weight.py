"""One count of Binance REST traffic per host, shared by every caller (OPT-036).

Binance limits request weight per IP and per minute (``REQUEST_WEIGHT``) and reports the weight
already used in ``X-MBX-USED-WEIGHT-1M``. A 429 means the limit was hit; requests sent after it
turn into a 418, an IP ban that grows from minutes to days. Both carry ``Retry-After``.

Before M5 three controls reacted to that pressure without a common count: the Retry Engine, the
per-cycle heavy-check budget and the snapshot engine's resilience. The ledger is the single
count: the Binance client and the candle provider record every response here, a 429/418 starts a
back-off that every caller of the same host honours, and the heavy-check budget and the cycle
metrics read the same numbers.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

USED_WEIGHT_HEADER = "x-mbx-used-weight-1m"
ORDER_COUNT_HEADER = "x-mbx-order-count-1m"
RETRY_AFTER_HEADER = "retry-after"
RATE_LIMITED_STATUS = 429
IP_BANNED_STATUS = 418
# Binance's error code for both statuses: "Too many requests" / "IP banned until ...".
TOO_MANY_REQUESTS_CODE = -1003
# Back-off when a 429/418 carries no usable Retry-After. 418 bans last two minutes at least.
DEFAULT_BACKOFF_SECONDS = {RATE_LIMITED_STATUS: 10.0, IP_BANNED_STATUS: 120.0}
MAX_BACKOFF_SECONDS = 3 * 24 * 3600.0
# Futures REQUEST_WEIGHT limit per minute and IP, used only to turn the header into a ratio.
DEFAULT_WEIGHT_LIMIT_1M = 2400
# A used-weight reading older than this no longer describes the current minute.
_WEIGHT_READING_TTL_SECONDS = 60.0

_SOURCE: ContextVar[str] = ContextVar("binance_rest_source", default="unattributed")


@contextmanager
def rest_source(name: str) -> Iterator[None]:
    """Attribute the REST requests made inside the block to ``name`` in the ledger."""

    token = _SOURCE.set(str(name))
    try:
        yield
    finally:
        _SOURCE.reset(token)


def current_rest_source() -> str:
    return _SOURCE.get()


def host_of(url: str) -> str:
    return urlsplit(str(url)).netloc.lower()


def parse_retry_after(
    headers: Mapping[str, Any] | None, *, now: float | None = None
) -> float | None:
    """Seconds from ``Retry-After`` (delay-seconds or HTTP date); ``None`` when absent or invalid."""

    raw = _header(headers, RETRY_AFTER_HEADER)
    if raw is None:
        return None
    text = str(raw).strip()
    if text.isascii() and text.isdigit():
        return min(float(text), MAX_BACKOFF_SECONDS)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None or moment.tzinfo is None:
        return None
    reference = time.time() if now is None else now
    return min(max(0.0, moment.timestamp() - reference), MAX_BACKOFF_SECONDS)


@dataclass
class _HostState:
    requests: int = 0
    by_source: Counter[str] = field(default_factory=Counter)
    by_path: Counter[str] = field(default_factory=Counter)
    rate_limited: int = 0
    banned: int = 0
    refused_during_backoff: int = 0
    used_weight_1m: int | None = None
    used_weight_at: float = 0.0
    max_used_weight_1m: int = 0
    order_count_1m: int | None = None
    backoff_until: float = 0.0
    backoff_status: int = 0


@dataclass(frozen=True)
class RestBackoff:
    """An active back-off of one host: refuse to send until ``remaining_seconds`` elapse."""

    host: str
    status_code: int
    remaining_seconds: float


class RestWeightLedger:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        weight_limit_1m: int = DEFAULT_WEIGHT_LIMIT_1M,
    ) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._hosts: dict[str, _HostState] = {}
        self.weight_limit_1m = max(1, int(weight_limit_1m))

    def set_weight_limit(self, weight_limit_1m: int) -> None:
        """The REQUEST_WEIGHT limit per minute the used-weight ratio is computed against."""
        self.weight_limit_1m = max(1, int(weight_limit_1m))

    # --- recording ----------------------------------------------------------------------------

    def record(
        self,
        *,
        host: str,
        path: str,
        status_code: int,
        headers: Mapping[str, Any] | None,
        source: str | None = None,
    ) -> None:
        """Record one response; a 429/418 starts (or extends) the host's back-off."""

        now = self._monotonic()
        used = _int_header(headers, USED_WEIGHT_HEADER)
        orders = _int_header(headers, ORDER_COUNT_HEADER)
        with self._lock:
            state = self._hosts.setdefault(host, _HostState())
            state.requests += 1
            state.by_source[source or current_rest_source()] += 1
            state.by_path[str(path)] += 1
            if used is not None:
                state.used_weight_1m = used
                state.used_weight_at = now
                state.max_used_weight_1m = max(state.max_used_weight_1m, used)
            if orders is not None:
                state.order_count_1m = orders
            if status_code in DEFAULT_BACKOFF_SECONDS:
                if status_code == IP_BANNED_STATUS:
                    state.banned += 1
                else:
                    state.rate_limited += 1
                delay = parse_retry_after(headers)
                if delay is None:
                    delay = DEFAULT_BACKOFF_SECONDS[status_code]
                # A later, shorter Retry-After never shortens a back-off already running.
                if state.backoff_until <= now:
                    state.backoff_status = 0
                state.backoff_until = max(state.backoff_until, now + delay)
                # A running ban stays the reported reason until it expires.
                if state.backoff_status != IP_BANNED_STATUS:
                    state.backoff_status = status_code

    def active_backoff(self, host: str) -> RestBackoff | None:
        """The host's back-off, counting the refused request; ``None`` when sending is allowed."""

        now = self._monotonic()
        with self._lock:
            state = self._hosts.get(host)
            if state is None or state.backoff_until <= now:
                if state is not None and state.backoff_status:
                    state.backoff_status = 0
                return None
            state.refused_during_backoff += 1
            return RestBackoff(
                host=host,
                status_code=state.backoff_status or RATE_LIMITED_STATUS,
                remaining_seconds=state.backoff_until - now,
            )

    # --- reading ------------------------------------------------------------------------------

    def pressure(self) -> float | None:
        """Highest fresh used-weight ratio across hosts (1.0 while a host is backing off)."""

        now = self._monotonic()
        ratios: list[float] = []
        with self._lock:
            for state in self._hosts.values():
                if state.backoff_until > now:
                    ratios.append(1.0)
                elif (
                    state.used_weight_1m is not None
                    and now - state.used_weight_at <= _WEIGHT_READING_TTL_SECONDS
                ):
                    ratios.append(state.used_weight_1m / self.weight_limit_1m)
        return max(ratios) if ratios else None

    def totals(self) -> dict[str, int]:
        with self._lock:
            return {
                "requests": sum(s.requests for s in self._hosts.values()),
                "rate_limited": sum(s.rate_limited for s in self._hosts.values()),
                "banned": sum(s.banned for s in self._hosts.values()),
                "refused_during_backoff": sum(
                    s.refused_during_backoff for s in self._hosts.values()
                ),
            }

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe view per host, for cycle metrics and diagnostics."""

        now = self._monotonic()
        with self._lock:
            hosts = {
                host: {
                    "requests": state.requests,
                    "by_source": dict(state.by_source),
                    "top_paths": dict(state.by_path.most_common(8)),
                    "rate_limited": state.rate_limited,
                    "banned": state.banned,
                    "refused_during_backoff": state.refused_during_backoff,
                    "used_weight_1m": (
                        state.used_weight_1m
                        if now - state.used_weight_at <= _WEIGHT_READING_TTL_SECONDS
                        else None
                    ),
                    "max_used_weight_1m": state.max_used_weight_1m,
                    "order_count_1m": state.order_count_1m,
                    "backoff_remaining_seconds": round(max(0.0, state.backoff_until - now), 3),
                }
                for host, state in self._hosts.items()
            }
        return {"weight_limit_1m": self.weight_limit_1m, "hosts": hosts}

    def reset(self) -> None:
        with self._lock:
            self._hosts.clear()


def _header(headers: Mapping[str, Any] | None, name: str) -> Any:
    if not headers:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return value
    for key, value in dict(headers).items():
        if str(key).lower() == name:
            return value
    return None


def _int_header(headers: Mapping[str, Any] | None, name: str) -> int | None:
    raw = _header(headers, name)
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


_SHARED = RestWeightLedger()


def shared_rest_weight_ledger() -> RestWeightLedger:
    """The process-wide ledger every Binance REST caller records into."""

    return _SHARED


__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_WEIGHT_LIMIT_1M",
    "IP_BANNED_STATUS",
    "RATE_LIMITED_STATUS",
    "TOO_MANY_REQUESTS_CODE",
    "RestBackoff",
    "RestWeightLedger",
    "current_rest_source",
    "host_of",
    "parse_retry_after",
    "rest_source",
    "shared_rest_weight_ledger",
]
