"""Resource keys of the operational snapshot: one per Binance read the Governor relies on."""

from __future__ import annotations

from collections.abc import Iterable

from market_data.market_snapshots import normalize_symbol
from market_data.snapshot_engine import ResourceKey

ACCOUNT = ResourceKey("account", "summary")
ALL_POSITIONS = ResourceKey("account", "positions")
EXCHANGE_INFO = ResourceKey("exchange", "info")


def market_state(symbol: str) -> ResourceKey:
    return ResourceKey("market", "state", normalize_symbol(symbol))


def mark_price(symbol: str) -> ResourceKey:
    return ResourceKey("market", "mark-price", normalize_symbol(symbol))


def position(symbol: str) -> ResourceKey:
    return ResourceKey("account", "position", normalize_symbol(symbol))


def open_orders(symbol: str) -> ResourceKey:
    return ResourceKey("orders", "open", normalize_symbol(symbol))


def open_algo_orders(symbol: str) -> ResourceKey:
    return ResourceKey("orders", "open-algo", normalize_symbol(symbol))


def exchange_rules(symbol: str) -> ResourceKey:
    return ResourceKey("exchange", "rules", normalize_symbol(symbol))


def is_key(key: ResourceKey, namespace: str, name: str) -> bool:
    return key.namespace == namespace and key.name == name


def symbol_evaluation_keys(symbol: str) -> list[ResourceKey]:
    """Everything the heavy evaluation of one symbol reads."""

    normalized = normalize_symbol(symbol)
    return [
        ACCOUNT,
        market_state(normalized),
        mark_price(normalized),
        position(normalized),
        open_orders(normalized),
        open_algo_orders(normalized),
        exchange_rules(normalized),
    ]


def normalized_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    """Normalized symbols in first-seen order, without duplicates."""

    return tuple(dict.fromkeys(normalize_symbol(item) for item in symbols))


__all__ = [
    "ACCOUNT",
    "ALL_POSITIONS",
    "EXCHANGE_INFO",
    "exchange_rules",
    "is_key",
    "mark_price",
    "market_state",
    "normalized_symbols",
    "open_algo_orders",
    "open_orders",
    "position",
    "symbol_evaluation_keys",
]
