"""Bootstrap exchangeInfo from REST before the first cycle, bypassing any cached copy."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from market_data.snapshot_engine import SourceUnavailableError
from market_data.snapshot_provider.keys import EXCHANGE_INFO
from market_data.snapshot_provider.observed_at import DOMAIN_OBSERVED_AT_KEY, coerce_now
from market_data.snapshot_provider.settings import ExchangeInfoBootstrapResult
from market_data.snapshot_provider.sources import SOURCE_REST_EXCHANGE

if TYPE_CHECKING:
    from market_data.snapshot_provider.provider import SnapshotProvider


def bootstrap_exchange_info(
    provider: SnapshotProvider,
    *,
    now: float | None = None,
) -> ExchangeInfoBootstrapResult:
    """Fetch a guaranteed-fresh exchangeInfo from the Binance REST API.

    Invalidation before attempt 1 is mandatory: the engine cache may hold
    a value from a previous process lifetime (hours or days old) that passes
    the in-memory resource check but would be rejected by the freshness policy
    at resolve time — surfacing as StaleSourceData.  Invalidating upfront
    forces a live fetch on every attempt, eliminating that failure mode.

    Retry schedule (exponential backoff, capped):
      attempt 1 → fetch immediately after initial invalidation
      attempt 2 → base_delay * 2^0  (default: 1 s)
      attempt 3 → base_delay * 2^1  (default: 2 s)
      attempt 4 → base_delay * 2^2  (default: 4 s, capped at max_delay)
      …
    Default settings expose 4 attempts with delays 0/1/2/4 s.
    """
    settings = provider.settings
    started_at = time.monotonic()
    last_error: Exception | None = None

    for attempt in range(1, settings.exchange_info_bootstrap_attempts + 1):
        # Always invalidate before every attempt — not just on retries.
        # On attempt 1 this evicts any stale value carried over from a
        # previous process lifetime.  On subsequent attempts it clears the
        # previous failed fetch so the engine does not serve a cached error.
        provider.publisher.invalidate(
            EXCHANGE_INFO,
            reason=(
                "alphora-exchange-info-bootstrap-init"
                if attempt == 1
                else "alphora-exchange-info-bootstrap-retry"
            ),
        )

        try:
            return _bootstrap_once(provider, attempt=attempt, now=now, started_at=started_at)
        except Exception as error:
            last_error = error

            if attempt >= settings.exchange_info_bootstrap_attempts:
                break

            # Exponential backoff: base * 2^(attempt-1), capped at max.
            delay_seconds = min(
                settings.exchange_info_bootstrap_max_delay_seconds,
                settings.exchange_info_bootstrap_base_delay_seconds * (2 ** (attempt - 1)),
            )

            if delay_seconds > 0:
                provider.sleep(delay_seconds)

    if last_error is None:
        raise RuntimeError("Exchange info bootstrap ended without a result")

    raise last_error


def _bootstrap_once(
    provider: SnapshotProvider,
    *,
    attempt: int,
    now: float | None,
    started_at: float,
) -> ExchangeInfoBootstrapResult:
    settings = provider.settings
    exchange_info = provider.transport.call_client(
        provider.client.exchange_info,
        source_name=SOURCE_REST_EXCHANGE,
        budget_seconds=settings.exchange_info_bootstrap_timeout_seconds,
        connect_timeout_seconds=settings.exchange_info_bootstrap_connect_timeout_seconds,
        read_timeout_seconds=settings.exchange_info_bootstrap_read_timeout_seconds,
        max_attempts=1,
    )
    symbols = exchange_info.get("symbols")

    if not isinstance(symbols, list) or not symbols:
        provider.publisher.invalidate(
            EXCHANGE_INFO,
            reason="alphora-exchange-info-bootstrap-invalid-payload",
        )
        raise SourceUnavailableError("exchangeInfo returned no symbols")

    observed_at = coerce_now(now)
    provider.publisher.publish(
        EXCHANGE_INFO,
        dict(exchange_info),
        source=SOURCE_REST_EXCHANGE,
        observed_at=observed_at,
        metadata={
            "transport": "binance_rest_exchange_info_bootstrap",
            DOMAIN_OBSERVED_AT_KEY: observed_at,
        },
        force=True,
        replace_equal=True,
    )
    snapshot = provider.get_exchange_info(now=now)
    last_snapshot = provider.last_snapshot
    snapshot_item = (
        last_snapshot.resources.get(EXCHANGE_INFO) if last_snapshot is not None else None
    )

    return ExchangeInfoBootstrapResult(
        attempts=attempt,
        duration_ms=max(0.0, (time.monotonic() - started_at) * 1000.0),
        symbol_count=len(snapshot.get("symbols", symbols)),
        from_cache=bool(getattr(snapshot_item, "from_cache", False)),
        stale=bool(getattr(snapshot_item, "stale", False)),
    )


__all__ = ["bootstrap_exchange_info"]
