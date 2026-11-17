from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection
from domain.symbol_rules import SymbolRules
from utils.events import EventChannel, observe_event


@dataclass(frozen=True)
class LightMarketSnapshot:
    symbol: str
    market: MarketSnapshot
    timestamp: float
    data_age_seconds: float
    stale: bool


@dataclass(frozen=True)
class HeavyMarketSnapshot:
    symbol: str
    market: MarketSnapshot
    mark_price: Decimal
    exchange_rules: SymbolRules | None
    position: FuturesPosition
    open_orders: list[dict[str, Any]]
    open_algo_orders: list[dict[str, Any]]
    account_info: dict[str, Any]
    timestamp: float
    data_age_seconds: float
    stale: bool
    funding_rate: Decimal | None = None
    next_funding_time: int | None = None
    index_price: Decimal | None = None


def market_snapshot_from_price(
    *,
    symbol: str,
    price: Decimal,
    price_windows: dict[str, deque[Decimal]],
    price_window: int,
    candle_data_hub: Any = None,
) -> MarketSnapshot:
    normalized = normalize_symbol(symbol)
    prices = price_windows.setdefault(normalized, deque(maxlen=max(1, int(price_window))))
    prices.append(price)
    return enrich_market_snapshot_with_candles(
        MarketSnapshot(
            symbol=normalized,
            price=price,
            prices=list(prices),
        ),
        candle_data_hub=candle_data_hub,
    )


def enrich_market_snapshot_with_candles(
    market: MarketSnapshot,
    *,
    candle_data_hub: Any = None,
) -> MarketSnapshot:
    """Attach candle data and diagnostics without changing price-window semantics."""

    if candle_data_hub is None:
        return market

    normalized = normalize_symbol(market.symbol)
    try:
        candles = candle_data_hub.candle_snapshot_for_symbol(normalized)
        diagnostics = candle_data_hub.diagnostics_for_symbol(normalized)
        requirements_for_symbol = getattr(candle_data_hub, "requirements_for_symbol", None)
        requirements = (
            requirements_for_symbol(normalized) if callable(requirements_for_symbol) else None
        )
    except Exception as error:
        observe_event(
            "candle_snapshot_injection_failed",
            component=__name__,
            category="market_data",
            channel=EventChannel.AUDIT,
            severity="WARNING",
            symbol=normalized,
            reason=str(error),
            error_type=type(error).__name__,
        )
        return market

    if not candles and not diagnostics and requirements is None:
        return market

    return replace(
        market,
        candles=dict(candles) if candles else None,
        candle_diagnostics=dict(diagnostics) if diagnostics else None,
        primary_timeframe=(
            getattr(requirements, "primary_timeframe", None)
            if requirements is not None
            else market.primary_timeframe
        ),
    )


def extract_position_from_risk(symbol: str, positions: list[dict[str, Any]]) -> FuturesPosition:
    normalized = normalize_symbol(symbol)
    for data in positions:
        amount = Decimal(str(data.get("positionAmt", "0")))
        if amount == 0:
            continue
        direction = PositionDirection.LONG if amount > 0 else PositionDirection.SHORT
        return FuturesPosition(
            symbol=normalized,
            direction=direction,
            quantity=abs(amount),
            entry_price=Decimal(str(data.get("entryPrice", "0"))),
            unrealized_pnl=Decimal(str(data.get("unRealizedProfit", "0"))),
        )
    return empty_position(normalized)


def extract_positions_from_risk(positions: list[dict[str, Any]]) -> dict[str, FuturesPosition]:
    """Convert a global Binance position-risk payload into a symbol-indexed read model."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for data in positions:
        if not isinstance(data, dict):
            continue
        symbol = normalize_symbol(data.get("symbol", ""))
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(data)

    return {
        symbol: extract_position_from_risk(symbol, payloads) for symbol, payloads in grouped.items()
    }


def empty_position(symbol: str) -> FuturesPosition:
    return FuturesPosition(
        symbol=normalize_symbol(symbol),
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def refresh_light_age(
    snapshot: LightMarketSnapshot,
    now: float,
    max_data_age_seconds: float,
) -> LightMarketSnapshot:
    age = max(0.0, now - snapshot.timestamp)
    return LightMarketSnapshot(
        symbol=snapshot.symbol,
        market=snapshot.market,
        timestamp=snapshot.timestamp,
        data_age_seconds=age,
        stale=age > max_data_age_seconds,
    )


def refresh_heavy_age(
    snapshot: HeavyMarketSnapshot,
    now: float,
    max_data_age_seconds: float,
) -> HeavyMarketSnapshot:
    age = max(0.0, now - snapshot.timestamp)
    return replace(
        snapshot,
        data_age_seconds=age,
        stale=age > max_data_age_seconds,
    )


def extract_funding_rate(payload: Any) -> Decimal | None:
    """Parse ``lastFundingRate`` (or ``fundingRate``) from a premium-index payload."""

    if not isinstance(payload, Mapping):
        return None
    for key in ("lastFundingRate", "fundingRate", "funding_rate"):
        raw = payload.get(key)
        if raw is None or raw == "":
            continue
        try:
            rate = Decimal(str(raw))
        except Exception:
            continue
        if rate.is_finite():
            return rate
    return None


def extract_next_funding_time(payload: Any) -> int | None:
    """Parse ``nextFundingTime`` (milliseconds) from a premium-index payload."""

    if not isinstance(payload, Mapping):
        return None
    for key in ("nextFundingTime", "next_funding_time"):
        raw = payload.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = int(raw)
        except Exception:
            continue
        if value > 0:
            return value
    return None


def extract_index_price(payload: Any) -> Decimal | None:
    """Parse ``indexPrice`` from a premium-index payload when positive."""

    if not isinstance(payload, Mapping):
        return None
    for key in ("indexPrice", "index_price"):
        raw = payload.get(key)
        if raw is None or raw == "":
            continue
        try:
            price = Decimal(str(raw))
        except Exception:
            continue
        if price.is_finite() and price > 0:
            return price
    return None


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").upper().strip()
