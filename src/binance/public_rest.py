"""Shared accounting and back-off for public Binance REST requests."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import requests

from binance.rest_weight import RestBackoff, RestWeightLedger, host_of, rest_source


class RestHostBackoff(RuntimeError):
    def __init__(self, backoff: RestBackoff) -> None:
        self.backoff = backoff
        super().__init__(
            f"{backoff.host} em back-off apos HTTP {backoff.status_code} "
            f"por mais {backoff.remaining_seconds:.1f}s"
        )


def tracked_public_get(
    url: str,
    *,
    params: dict[str, Any],
    timeout: float,
    source: str,
    ledger: RestWeightLedger,
) -> requests.Response:
    """Count every HTTP response and refuse to send during a host back-off."""

    host = host_of(url)
    backoff = ledger.active_backoff(host)
    if backoff is not None:
        raise RestHostBackoff(backoff)
    with rest_source(source):
        response = requests.get(url, params=params, timeout=timeout)
        ledger.record(
            host=host,
            path=urlsplit(url).path,
            status_code=int(getattr(response, "status_code", 0) or 0),
            headers=getattr(response, "headers", None),
        )
    return response
