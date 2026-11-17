"""Read the Governor's light and heavy market snapshots out of an operational snapshot."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, TypeVar

from domain.models import FuturesPosition, MarketSnapshot
from market_data.market_snapshots import (
    HeavyMarketSnapshot,
    LightMarketSnapshot,
    extract_funding_rate,
    extract_index_price,
    extract_next_funding_time,
    normalize_symbol,
)
from market_data.snapshot_engine import ResourceKey, Snapshot
from market_data.snapshot_provider.keys import (
    ACCOUNT,
    exchange_rules,
    mark_price,
    market_state,
    open_algo_orders,
    open_orders,
    position,
)
from market_data.snapshot_provider.observed_at import domain_observed_at

T = TypeVar("T")


def expect(value: Any, expected_type: type[T], key: ResourceKey) -> T:
    """Fail loudly when a source resolved a resource to an unexpected type."""

    if not isinstance(value, expected_type):
        raise TypeError(
            f"Snapshot resource {key} returned {type(value).__name__}; "
            f"expected {expected_type.__name__}"
        )
    return value


def light_snapshot_from_snapshot(
    snapshot: Snapshot,
    symbol: str,
    *,
    now: float,
    max_data_age_seconds: float,
) -> LightMarketSnapshot:
    key = market_state(symbol)
    item = snapshot[key]
    market = expect(item.value, MarketSnapshot, key)
    observed_at = domain_observed_at(item)
    age = max(0.0, now - observed_at)
    return LightMarketSnapshot(
        symbol=key.subject,
        market=market,
        timestamp=observed_at,
        data_age_seconds=age,
        stale=item.stale or age > max_data_age_seconds,
    )


def heavy_snapshot_from_snapshot(
    snapshot: Snapshot,
    symbol: str,
    *,
    now: float,
    max_data_age_seconds: float,
    cached_position: FuturesPosition | None = None,
    cached_account_info: dict[str, Any] | None = None,
    cached_open_orders: list[dict[str, Any]] | None = None,
) -> HeavyMarketSnapshot:
    normalized = normalize_symbol(symbol)
    # Operational keys drive the age/stale calculation.  exchange_rules has a 6-hour TTL
    # and is only structural — including it in min(observed_at) would make a snapshot whose
    # operational data is fresh appear stale because exchange_rules was fetched 127+ seconds ago.
    operational_keys: list[ResourceKey] = [
        market_state(normalized),
        mark_price(normalized),
        open_algo_orders(normalized),
    ]
    if cached_position is None:
        operational_keys.append(position(normalized))
    if cached_open_orders is None:
        operational_keys.append(open_orders(normalized))
    if cached_account_info is None:
        operational_keys.append(ACCOUNT)

    operational_items = [snapshot[key] for key in operational_keys]
    rules_key = exchange_rules(normalized)
    rules_item = snapshot[rules_key] if rules_key in snapshot else None

    market = expect(
        snapshot.value(market_state(normalized)),
        MarketSnapshot,
        market_state(normalized),
    )
    observed_at = min(domain_observed_at(item) for item in operational_items)
    age = max(0.0, now - observed_at)
    # exchange_rules is stale only if the engine's own TTL (6 h) is exceeded — not by the
    # global max_data_age_seconds that governs operational freshness.
    rules_stale = bool(rules_item is not None and rules_item.stale)
    mark_metadata = snapshot[mark_price(normalized)].metadata
    return HeavyMarketSnapshot(
        symbol=normalized,
        market=market,
        mark_price=expect(snapshot.value(mark_price(normalized)), Decimal, mark_price(normalized)),
        exchange_rules=snapshot.value(exchange_rules(normalized)),
        position=(
            cached_position
            if cached_position is not None
            else expect(snapshot.value(position(normalized)), FuturesPosition, position(normalized))
        ),
        open_orders=(
            list(cached_open_orders)
            if cached_open_orders is not None
            else list(snapshot.value(open_orders(normalized), list))
        ),
        open_algo_orders=list(snapshot.value(open_algo_orders(normalized), list)),
        account_info=(
            dict(cached_account_info)
            if cached_account_info is not None
            else dict(snapshot.value(ACCOUNT, dict))
        ),
        timestamp=observed_at,
        data_age_seconds=age,
        stale=(
            any(item.stale for item in operational_items)
            or age > max_data_age_seconds
            or rules_stale
        ),
        funding_rate=extract_funding_rate(mark_metadata),
        next_funding_time=extract_next_funding_time(mark_metadata),
        index_price=extract_index_price(mark_metadata),
    )


def missing_heavy_snapshot_keys(snapshot: Snapshot, symbol: str) -> tuple[ResourceKey, ...]:
    """The keys a heavy snapshot needs that ``snapshot`` does not hold."""

    normalized = normalize_symbol(symbol)
    required = (
        market_state(normalized),
        mark_price(normalized),
        exchange_rules(normalized),
        position(normalized),
        open_orders(normalized),
        open_algo_orders(normalized),
        ACCOUNT,
    )
    return tuple(key for key in required if key not in snapshot.resources)


__all__ = [
    "expect",
    "heavy_snapshot_from_snapshot",
    "light_snapshot_from_snapshot",
    "missing_heavy_snapshot_keys",
]
