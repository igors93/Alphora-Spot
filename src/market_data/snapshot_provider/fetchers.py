"""The fetch functions behind every snapshot source, from the cache down to REST."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any, Protocol

from domain.models import MarketSnapshot
from futures.exchange_rules import extract_symbol_rules
from market_data.market_snapshots import (
    empty_position,
    enrich_market_snapshot_with_candles,
    extract_funding_rate,
    extract_index_price,
    extract_next_funding_time,
    extract_position_from_risk,
    extract_positions_from_risk,
    normalize_symbol,
)
from market_data.price_window import MarketPriceWindow
from market_data.snapshot_engine import (
    ResourceKey,
    Snapshot,
    SourcePayload,
    SourceUnavailableError,
)
from market_data.snapshot_provider.keys import ACCOUNT, ALL_POSITIONS, EXCHANGE_INFO, is_key
from market_data.snapshot_provider.observed_at import (
    DOMAIN_OBSERVED_AT_KEY,
    context_now,
    domain_observed_at,
    mapping_observed_at,
    normalize_observed_at,
)
from market_data.snapshot_provider.settings import SnapshotProviderSettings
from market_data.snapshot_provider.sources import SOURCE_REST_EXCHANGE, SOURCE_REST_MARKET
from market_data.snapshot_provider.transport import RestTransport

USER_DATA_CACHE_TRANSPORT = "user_data_cache"


class MarketState(Protocol):
    """What the market stream source reads from the WebSocket hub for one symbol."""

    @property
    def price_snapshot(self) -> Any: ...

    @property
    def prices(self) -> Any: ...


class SourceFetchers:
    """Resolve resource keys from the user data cache, the market stream and Binance REST."""

    def __init__(
        self,
        *,
        client: Any,
        transport: RestTransport,
        settings: SnapshotProviderSettings,
        market_data_hub: Any,
        candle_data_hub: Any,
        user_data_cache: Any,
        price_window: MarketPriceWindow,
        wall_clock: Callable[[], float],
    ) -> None:
        self.client = client
        self.transport = transport
        self.settings = settings
        self.market_data_hub = market_data_hub
        self.candle_data_hub = candle_data_hub
        self.user_data_cache = user_data_cache
        self.price_window = price_window
        self._wall_clock = wall_clock

    # --- user data cache (highest authority for private state) -----------------------------

    def user_data_cache_supports(self, key: ResourceKey) -> bool:
        """Expose the shadow source only while the requested value is present.

        ``supports`` is deliberately dynamic for this source, so the engine adapter disables
        support-result caching. An empty/disabled shadow cache then creates no source failure or
        circuit-breaker pressure, while a later USER_DATA event becomes visible immediately.
        """

        cache = self.user_data_cache
        if cache is None:
            return False
        if is_key(key, "account", "position"):
            return cache.position_snapshot_with_timestamp(key.subject) is not None
        if is_key(key, "orders", "open"):
            return cache.open_orders_snapshot_with_timestamp(key.subject) is not None
        if key == ACCOUNT:
            cached = cache.account_info_snapshot_with_timestamp()
            return cached is not None and bool(
                cached[0].get("runtime_balance_fields_trusted", False)
            )
        return False

    def user_data_cache_batch(
        self,
        keys: Iterable[ResourceKey],
        _context: Any,
    ) -> dict[ResourceKey, SourcePayload[Any]]:
        cache = self.user_data_cache
        if cache is None:
            return {}
        resolved: dict[ResourceKey, SourcePayload[Any]] = {}
        for key in keys:
            if is_key(key, "account", "position"):
                cached = cache.position_snapshot_with_timestamp(key.subject)
            elif is_key(key, "orders", "open"):
                cached = cache.open_orders_snapshot_with_timestamp(key.subject)
            elif key == ACCOUNT:
                cached = cache.account_info_snapshot_with_timestamp()
                if cached is not None and not bool(
                    cached[0].get("runtime_balance_fields_trusted", False)
                ):
                    continue
            else:
                continue
            if cached is None:
                continue
            value, observed_at = cached
            resolved[key] = SourcePayload(
                value=value,
                observed_at=observed_at,
                metadata={"transport": USER_DATA_CACHE_TRANSPORT},
            )
        return resolved

    # --- WebSocket market stream -------------------------------------------------------------

    def market_stream_batch(
        self,
        keys: Iterable[ResourceKey],
        context: Any,
    ) -> dict[ResourceKey, SourcePayload[Any]]:
        if self.market_data_hub is None:
            return {}
        current_time = context_now(context)
        resolved: dict[ResourceKey, SourcePayload[Any]] = {}
        for key in keys:
            state = self.market_data_hub.latest_market_state(key.subject, now=current_time)
            if state is None:
                continue
            snapshot = state.price_snapshot
            received_at = normalize_observed_at(
                domain_observed_at=snapshot.received_at,
                reference_time=context.requested_at,
            )
            # Cap to received_at so exchange clock skew cannot create a future observation.
            observed_at = min(
                normalize_observed_at(
                    domain_observed_at=(
                        snapshot.event_time if snapshot.event_time is not None else received_at
                    ),
                    reference_time=received_at,
                ),
                received_at,
            )
            resolved[key] = SourcePayload(
                value=self.market_snapshot_from_state(state),
                observed_at=observed_at,
                metadata={
                    DOMAIN_OBSERVED_AT_KEY: observed_at,
                    "transport_received_at": received_at,
                    "transport_delay_ms": max(0.0, (received_at - observed_at) * 1000.0),
                    "transport": snapshot.source,
                    "event_time": snapshot.event_time,
                    "bid": str(snapshot.bid_price),
                    "ask": str(snapshot.ask_price),
                    "price_window_size": len(state.prices),
                },
            )
        return resolved

    # --- REST ---------------------------------------------------------------------------------

    def rest_market(self, key: ResourceKey, context: Any) -> SourcePayload[Any]:
        if is_key(key, "market", "state"):
            # Binance Testnet can return a ticker ``time`` that reflects the last market
            # update rather than the freshness of this HTTP response. Quiet symbols may
            # therefore carry a server timestamp tens of seconds old even though the value
            # was fetched just now. Treat the local response receipt as the observation time,
            # exactly as mark-price already does. Execution safety still compares contract
            # and mark prices before dispatch, so a genuinely stale price remains fail-closed.
            return self.rest_payload(
                context=context,
                transport="binance_rest_ticker",
                fetch=lambda: self.transport.call_client(
                    self.client.ticker_price,
                    key.subject,
                    source_name=SOURCE_REST_MARKET,
                ),
                transform=lambda data: self.market_snapshot_from_price(
                    key.subject, Decimal(str(data["price"]))
                ),
            )
        if is_key(key, "market", "mark-price"):
            # observed_at intentionally uses response_received_at (no observed_at_fields).
            # On Testnet the exchange "time" field reflects when Binance last recalculated the
            # mark price, which can lag 30-120s behind wall clock due to low trading activity.
            # Using local receive time is semantically correct: it represents when our system
            # observed this value, and execution always force-refreshes before any order anyway.
            return self.rest_payload(
                context=context,
                transport="binance_rest_mark_price",
                fetch=lambda: self.transport.call_client(
                    self.client.mark_price,
                    key.subject,
                    source_name=SOURCE_REST_MARKET,
                ),
                transform=lambda data: Decimal(str(data.get("markPrice", "0"))),
                extra_metadata=_mark_price_metadata,
            )
        raise SourceUnavailableError(f"alphora-rest-market does not support {key}")

    def rest_private(self, key: ResourceKey, context: Any) -> SourcePayload[Any]:
        call = self.transport.call_private_client
        if key == ACCOUNT:
            return self.rest_payload(
                context=context,
                transport="binance_rest_account",
                fetch=lambda: call("account_info"),
            )
        if key == ALL_POSITIONS:
            return self.rest_payload(
                context=context,
                transport="binance_rest_all_positions",
                fetch=lambda: call("position_risk", None),
                transform=extract_positions_from_risk,
            )
        if is_key(key, "account", "position"):
            return self.rest_payload(
                context=context,
                transport="binance_rest_position_risk",
                fetch=lambda: call("position_risk", key.subject),
                transform=lambda positions: extract_position_from_risk(key.subject, positions),
            )
        if is_key(key, "orders", "open"):
            return self.rest_payload(
                context=context,
                transport="binance_rest_open_orders",
                fetch=lambda: call("open_orders", key.subject),
            )
        if is_key(key, "orders", "open-algo"):
            return self.rest_payload(
                context=context,
                transport="binance_rest_open_algo_orders",
                fetch=lambda: call("open_algo_orders", key.subject),
            )
        raise SourceUnavailableError(f"alphora-rest-private does not support {key}")

    def rest_exchange(self, key: ResourceKey, context: Any) -> SourcePayload[Any]:
        if key != EXCHANGE_INFO:
            raise SourceUnavailableError(f"alphora-rest-exchange does not support {key}")

        settings = self.settings
        return self.rest_payload(
            context=context,
            transport="binance_rest_exchange_info",
            fetch=lambda: self.transport.call_client(
                self.client.exchange_info,
                source_name=SOURCE_REST_EXCHANGE,
                budget_seconds=settings.exchange_info_transport_timeout_seconds,
                connect_timeout_seconds=settings.exchange_info_connect_timeout_seconds,
                read_timeout_seconds=settings.exchange_info_read_timeout_seconds,
            ),
            # Do NOT use serverTime from the response as the observed_at timestamp.
            # Binance's serverTime reflects the server's internal clock at request time,
            # not the age of the exchangeInfo data. On TESTNET this value is often hours
            # or days behind wall clock, which would make the freshly-fetched payload
            # appear stale (age >> ttl) and trigger StaleSourceData.
            # Using response_received_at (wall clock at arrival) is correct: we just
            # fetched this data, so it is fresh as of right now.
        )

    def rest_payload(
        self,
        *,
        context: Any,
        transport: str,
        fetch: Callable[[], Any],
        transform: Callable[[Any], Any] | None = None,
        observed_at_fields: tuple[str, ...] = (),
        extra_metadata: Callable[[Any], dict[str, Any]] | None = None,
    ) -> SourcePayload[Any]:
        request_started_at = float(self._wall_clock())
        response = fetch()
        response_received_at = float(self._wall_clock())
        # Cap to response_received_at: exchange server timestamps can be slightly ahead of
        # local clock, which would produce a future observed_at and cause SourceProtocolError.
        observed_at = min(
            mapping_observed_at(
                response,
                fields=observed_at_fields,
                fallback=response_received_at,
            ),
            response_received_at,
        )
        value = response if transform is None else transform(response)
        metadata: dict[str, Any] = {
            DOMAIN_OBSERVED_AT_KEY: observed_at,
            "request_started_at": request_started_at,
            "response_received_at": response_received_at,
            "source_latency_ms": max(
                0.0,
                (response_received_at - request_started_at) * 1000.0,
            ),
            "cycle_reference_at": context_now(context),
            "transport": transport,
        }
        if extra_metadata is not None:
            metadata.update(extra_metadata(response))
        return SourcePayload(
            value=value,
            observed_at=observed_at,
            metadata=metadata,
        )

    # --- derived resources --------------------------------------------------------------------

    def derive_position(
        self,
        key: ResourceKey,
        snapshot: Snapshot,
        _context: Any,
    ) -> SourcePayload[Any]:
        positions = snapshot.value(ALL_POSITIONS, dict)
        return _derived_payload(
            positions.get(key.subject, empty_position(key.subject)),
            snapshot=snapshot,
            dependency_key=ALL_POSITIONS,
        )

    def derive_exchange_rules(
        self,
        key: ResourceKey,
        snapshot: Snapshot,
        _context: Any,
    ) -> SourcePayload[Any]:
        exchange_info = snapshot.value(EXCHANGE_INFO, dict)
        return _derived_payload(
            extract_symbol_rules(exchange_info, key.subject),
            snapshot=snapshot,
            dependency_key=EXCHANGE_INFO,
        )

    # --- market snapshot values ---------------------------------------------------------------

    def market_snapshot_from_state(self, state: MarketState) -> MarketSnapshot:
        snapshot = state.price_snapshot
        return enrich_market_snapshot_with_candles(
            MarketSnapshot(
                symbol=normalize_symbol(snapshot.symbol),
                price=snapshot.price,
                prices=list(state.prices),
            ),
            candle_data_hub=self.candle_data_hub,
        )

    def market_snapshot_from_price(self, symbol: str, price: Decimal) -> MarketSnapshot:
        normalized = normalize_symbol(symbol)
        prices = self.price_window.record(normalized, price)
        return enrich_market_snapshot_with_candles(
            MarketSnapshot(symbol=normalized, price=Decimal(str(price)), prices=list(prices)),
            candle_data_hub=self.candle_data_hub,
        )


def _derived_payload(
    value: Any,
    *,
    snapshot: Snapshot,
    dependency_key: ResourceKey,
) -> SourcePayload[Any]:
    dependency = snapshot[dependency_key]
    return SourcePayload(
        value=value,
        observed_at=dependency.observed_at,
        metadata={
            DOMAIN_OBSERVED_AT_KEY: domain_observed_at(dependency),
            "derived_from": str(dependency_key),
            "dependency_source": dependency.source,
            "dependency_observed_at": dependency.observed_at,
            "dependency_fetched_at": dependency.fetched_at,
            "dependency_from_cache": dependency.from_cache,
        },
    )


def _mark_price_metadata(response: Any) -> dict[str, Any]:
    """Attach funding and index fields returned alongside ``markPrice`` by ``/fapi/v1/premiumIndex``."""

    extra: dict[str, Any] = {}
    funding_rate = extract_funding_rate(response)
    if funding_rate is not None:
        extra["funding_rate"] = str(funding_rate)
    next_funding_time = extract_next_funding_time(response)
    if next_funding_time is not None:
        extra["next_funding_time"] = next_funding_time
    index_price = extract_index_price(response)
    if index_price is not None:
        extra["index_price"] = str(index_price)
    return extra


__all__ = ["SourceFetchers", "USER_DATA_CACHE_TRANSPORT"]
