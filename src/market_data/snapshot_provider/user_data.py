"""Translate a User Data Stream cache update into snapshot resource writes and invalidations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from market_data.snapshot_engine import ResourceKey, ResourceUpdate
from market_data.snapshot_provider.keys import ACCOUNT, ALL_POSITIONS, open_orders, position
from market_data.snapshot_provider.observed_at import event_observed_at
from market_data.snapshot_provider.sources import SOURCE_USER_DATA


@dataclass(slots=True)
class UserDataWrites:
    updates: list[ResourceUpdate[Any]] = field(default_factory=list)
    invalidations: list[ResourceKey] = field(default_factory=list)


def user_data_writes(*, cache: Any, update: Any, event: Mapping[str, Any]) -> UserDataWrites:
    """What one User Data Stream event changes in the snapshot cache.

    Position and order events become normal snapshot resources. Partial account events invalidate
    the REST-backed account view unless the cache explicitly marks its runtime balance fields as
    trusted.
    """

    observed_at = event_observed_at(event)
    metadata = {"event_type": event.get("event_type", "")}
    details = getattr(update, "details", {}) or {}
    position_symbols = tuple(details.get("position_symbols", ()) or ())
    order_symbols = tuple(details.get("order_symbols", ()) or ())
    writes = UserDataWrites()

    def write(key: ResourceKey, value: Any, updated_at: float | None) -> None:
        writes.updates.append(
            ResourceUpdate(
                key,
                value,
                source=SOURCE_USER_DATA,
                observed_at=updated_at or observed_at,
                metadata=dict(metadata),
            )
        )

    for symbol in position_symbols:
        cached = cache.position_snapshot_with_timestamp(symbol)
        if cached is None:
            writes.invalidations.append(position(symbol))
            continue
        position_value, updated_at = cached
        write(position(symbol), position_value, updated_at)

    for symbol in order_symbols:
        cached = cache.open_orders_snapshot_with_timestamp(symbol)
        if cached is None:
            writes.invalidations.append(open_orders(symbol))
            continue
        orders_value, updated_at = cached
        write(open_orders(symbol), orders_value, updated_at)

    if bool(getattr(update, "account_cache_updated", False)):
        account_cached = cache.account_info_snapshot_with_timestamp()
        if account_cached is not None:
            account_value, updated_at = account_cached
            if bool(account_value.get("runtime_balance_fields_trusted", False)):
                write(ACCOUNT, account_value, updated_at)
            else:
                writes.invalidations.append(ACCOUNT)

    dirty_position_symbols = tuple(getattr(update, "dirty_position_symbols", ()) or ())
    writes.invalidations.extend(position(symbol) for symbol in dirty_position_symbols)
    if bool(getattr(update, "account_dirty", False)):
        writes.invalidations.append(ACCOUNT)

    if position_symbols or dirty_position_symbols:
        # The global REST view must be refreshed next cycle after any private position mutation.
        writes.invalidations.append(ALL_POSITIONS)

    return writes


__all__ = ["UserDataWrites", "user_data_writes"]
