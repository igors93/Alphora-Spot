from collections import deque
from dataclasses import replace
from decimal import Decimal
from time import time
from typing import Any

from binance.futures_client import BinanceFuturesClient
from domain.models import FuturesPosition
from futures.exchange_rules import extract_symbol_rules
from market_data.market_snapshots import (
    HeavyMarketSnapshot,
    LightMarketSnapshot,
    enrich_market_snapshot_with_candles,
    extract_funding_rate,
    extract_index_price,
    extract_next_funding_time,
    extract_position_from_risk,
    market_snapshot_from_price,
    normalize_symbol,
    refresh_light_age,
)
from market_data.snapshot_provider import (
    ACCOUNT,
    ALL_POSITIONS,
    SnapshotProviderSettings,
    build_snapshot_provider,
    wire_names,
)
from market_data.snapshot_provider import (
    open_algo_orders as snapshot_open_algo_orders,
)
from market_data.snapshot_provider import (
    open_orders as snapshot_open_orders,
)
from market_data.snapshot_provider import (
    position as snapshot_position,
)
from utils.events import EventChannel, observe_event


class MarketDataRouter:
    def __init__(
        self,
        client: BinanceFuturesClient,
        *,
        market_data_hub=None,
        light_ttl_seconds: float = 2.0,
        heavy_ttl_seconds: float = 8.0,
        max_data_age_seconds: float = 30.0,
        price_window: int = 120,
        candle_data_hub=None,
        snapshot_provider=None,
        snapshot_enabled: bool = True,
        snapshot_execution_mode=None,
        snapshot_environment=None,
        snapshot_fallback_allowed: bool | None = None,
        snapshot_settings_overrides: dict[str, Any] | None = None,
        user_data_cache=None,
        logger=None,
    ):
        self.client = client
        self.market_data_hub = market_data_hub
        self.light_ttl_seconds = float(light_ttl_seconds)
        self.heavy_ttl_seconds = float(heavy_ttl_seconds)
        self.max_data_age_seconds = float(max_data_age_seconds)
        self._prices: dict[str, deque[Decimal]] = {}
        self._price_window = int(price_window)
        self._light_cache: dict[str, LightMarketSnapshot] = {}
        self.candle_data_hub = candle_data_hub
        self.snapshot_provider = snapshot_provider
        self.last_operational_snapshot = None
        if self.snapshot_provider is None and snapshot_enabled:
            snapshot_settings = SnapshotProviderSettings.from_runtime(
                light_ttl_seconds=self.light_ttl_seconds,
                heavy_ttl_seconds=self.heavy_ttl_seconds,
                max_data_age_seconds=self.max_data_age_seconds,
                price_window=self._price_window,
            )
            if snapshot_settings_overrides:
                allowed_fields = set(SnapshotProviderSettings.__dataclass_fields__)
                overrides = {
                    key: value
                    for key, value in snapshot_settings_overrides.items()
                    if key in allowed_fields and value is not None
                }
                if overrides:
                    snapshot_settings = replace(snapshot_settings, **overrides)
            self.snapshot_provider = build_snapshot_provider(
                client=client,
                market_data_hub=market_data_hub,
                candle_data_hub=candle_data_hub,
                user_data_cache=user_data_cache,
                settings=snapshot_settings,
                logger=logger,
                execution_mode=snapshot_execution_mode,
                environment=snapshot_environment,
                fallback_allowed=snapshot_fallback_allowed,
            )

    def _cycle_snapshot_refresh_age_seconds(self) -> float:
        provider = self.snapshot_provider
        settings = getattr(provider, "settings", None)
        value = getattr(settings, "execution_max_age_seconds", None)
        if value is None:
            return max(self.light_ttl_seconds, self.heavy_ttl_seconds)
        return max(0.0, float(value))

    def _cycle_snapshot_requires_refresh(self, snapshot) -> bool:
        return bool(
            snapshot.stale
            or float(snapshot.data_age_seconds) > self._cycle_snapshot_refresh_age_seconds()
        )

    def get_light_snapshot(
        self,
        symbol: str,
        *,
        now: float | None = None,
        operational_snapshot=None,
    ) -> LightMarketSnapshot:
        if self.snapshot_provider is not None:
            snapshot_now = time() if operational_snapshot is not None else now
            cycle_snapshot = None
            refreshed_from_cycle = False
            try:
                cycle_snapshot = self.snapshot_provider.get_light_snapshot(
                    symbol,
                    now=snapshot_now,
                    operational_snapshot=operational_snapshot,
                )
            except Exception as error:
                if operational_snapshot is None:
                    raise
                observe_event(
                    wire_names.CYCLE_LIGHT_FALLBACK,
                    component=__name__,
                    category="market_data",
                    channel=EventChannel.AUDIT,
                    severity="WARNING",
                    symbol=normalize_symbol(symbol),
                    reason=str(error),
                    error_type=type(error).__name__,
                )
            if operational_snapshot is not None and (
                cycle_snapshot is None or self._cycle_snapshot_requires_refresh(cycle_snapshot)
            ):
                try:
                    cycle_snapshot = self.snapshot_provider.get_light_snapshot(
                        symbol,
                        now=time(),
                    )
                except Exception as refresh_error:
                    observe_event(
                        wire_names.CYCLE_LIGHT_REFRESH_FAILED,
                        component=__name__,
                        category="market_data",
                        channel=EventChannel.AUDIT,
                        severity="WARNING",
                        symbol=normalize_symbol(symbol),
                        reason=str(refresh_error),
                        error_type=type(refresh_error).__name__,
                    )
                else:
                    refreshed_from_cycle = True
                    observe_event(
                        wire_names.CYCLE_LIGHT_REFRESHED,
                        component=__name__,
                        category="market_data",
                        channel=EventChannel.AUDIT,
                        symbol=normalize_symbol(symbol),
                        data_age_seconds=cycle_snapshot.data_age_seconds,
                    )
            if cycle_snapshot is not None:
                snapshot = cycle_snapshot
                self.last_operational_snapshot = (
                    self.snapshot_provider.last_snapshot
                    if refreshed_from_cycle
                    else operational_snapshot or self.snapshot_provider.last_snapshot
                )
                self._record_snapshot_event(
                    "market_snapshot_stale" if snapshot.stale else "market_snapshot_loaded",
                    snapshot=snapshot,
                    source=(
                        wire_names.SOURCE_LABEL_CYCLE
                        if operational_snapshot is not None
                        else wire_names.SOURCE_LABEL_PROVIDER
                    ),
                    severity="WARNING" if snapshot.stale else "INFO",
                )
                return self._enrich_light_snapshot(snapshot)

        current_time = time() if now is None else now
        normalized = normalize_symbol(symbol)
        ws_snapshot = None
        if self.market_data_hub is not None:
            ws_snapshot = self.market_data_hub.latest_price(normalized, now=current_time)

        if ws_snapshot is not None:
            snapshot = LightMarketSnapshot(
                symbol=normalized,
                market=market_snapshot_from_price(
                    symbol=normalized,
                    price=ws_snapshot.price,
                    price_windows=self._prices,
                    price_window=self._price_window,
                    candle_data_hub=self.candle_data_hub,
                ),
                timestamp=ws_snapshot.received_at,
                data_age_seconds=max(0.0, current_time - ws_snapshot.received_at),
                stale=False,
            )
            self._light_cache[normalized] = snapshot
            self._record_snapshot_event(
                "websocket_price_used",
                snapshot=snapshot,
                source="websocket",
            )
            return self._enrich_light_snapshot(snapshot)

        cached = self._light_cache.get(normalized)
        if cached is not None and current_time - cached.timestamp <= self.light_ttl_seconds:
            snapshot = self._enrich_light_snapshot(
                refresh_light_age(cached, current_time, self.max_data_age_seconds)
            )
            self._record_snapshot_event(
                "market_snapshot_stale" if snapshot.stale else "market_snapshot_loaded",
                snapshot=snapshot,
                source="light_cache",
                severity="WARNING" if snapshot.stale else "INFO",
            )
            return snapshot

        data = self.client.ticker_price(normalized)
        price = Decimal(str(data["price"]))
        snapshot = LightMarketSnapshot(
            symbol=normalized,
            market=market_snapshot_from_price(
                symbol=normalized,
                price=price,
                price_windows=self._prices,
                price_window=self._price_window,
                candle_data_hub=self.candle_data_hub,
            ),
            timestamp=current_time,
            data_age_seconds=0.0,
            stale=False,
        )
        snapshot = self._enrich_light_snapshot(snapshot)
        self._light_cache[normalized] = snapshot
        self._record_snapshot_event("rest_price_used", snapshot=snapshot, source="rest")
        return snapshot

    def get_heavy_snapshot(
        self,
        symbol: str,
        *,
        now: float | None = None,
        cached_position: FuturesPosition | None = None,
        cached_account_info: dict[str, Any] | None = None,
        cached_open_orders: list[dict[str, Any]] | None = None,
        operational_snapshot=None,
    ) -> HeavyMarketSnapshot:
        if self.snapshot_provider is not None:
            snapshot_now = time() if operational_snapshot is not None else now
            cycle_snapshot = None
            refreshed_from_cycle = False
            try:
                cycle_snapshot = self.snapshot_provider.get_heavy_snapshot(
                    symbol,
                    now=snapshot_now,
                    cached_position=cached_position,
                    cached_account_info=cached_account_info,
                    cached_open_orders=cached_open_orders,
                    operational_snapshot=operational_snapshot,
                )
            except Exception as error:
                if operational_snapshot is None:
                    raise
                observe_event(
                    wire_names.CYCLE_HEAVY_FALLBACK,
                    component=__name__,
                    category="market_data",
                    channel=EventChannel.AUDIT,
                    severity="WARNING",
                    symbol=normalize_symbol(symbol),
                    reason=str(error),
                    error_type=type(error).__name__,
                )
            if operational_snapshot is not None and (
                cycle_snapshot is None or self._cycle_snapshot_requires_refresh(cycle_snapshot)
            ):
                try:
                    cycle_snapshot = self.snapshot_provider.get_heavy_snapshot(
                        symbol,
                        now=time(),
                        cached_position=cached_position,
                        cached_account_info=cached_account_info,
                        cached_open_orders=cached_open_orders,
                    )
                except Exception as refresh_error:
                    observe_event(
                        wire_names.CYCLE_HEAVY_REFRESH_FAILED,
                        component=__name__,
                        category="market_data",
                        channel=EventChannel.AUDIT,
                        severity="WARNING",
                        symbol=normalize_symbol(symbol),
                        reason=str(refresh_error),
                        error_type=type(refresh_error).__name__,
                    )
                else:
                    refreshed_from_cycle = True
                    observe_event(
                        wire_names.CYCLE_HEAVY_REFRESHED,
                        component=__name__,
                        category="market_data",
                        channel=EventChannel.AUDIT,
                        symbol=normalize_symbol(symbol),
                        data_age_seconds=cycle_snapshot.data_age_seconds,
                    )
            if cycle_snapshot is not None:
                snapshot = cycle_snapshot
                self.last_operational_snapshot = (
                    self.snapshot_provider.last_snapshot
                    if refreshed_from_cycle
                    else operational_snapshot or self.snapshot_provider.last_snapshot
                )
                self._record_snapshot_event(
                    "market_snapshot_stale" if snapshot.stale else "market_snapshot_loaded",
                    snapshot=snapshot,
                    source=(
                        wire_names.SOURCE_LABEL_CYCLE
                        if operational_snapshot is not None
                        else wire_names.SOURCE_LABEL_PROVIDER
                    ),
                    severity="WARNING" if snapshot.stale else "INFO",
                    cache_assisted=(
                        cached_position is not None
                        or cached_account_info is not None
                        or cached_open_orders is not None
                    ),
                )
                return self._enrich_heavy_snapshot(snapshot)

        current_time = time() if now is None else now
        normalized = normalize_symbol(symbol)
        cache_assisted = (
            cached_position is not None
            or cached_account_info is not None
            or cached_open_orders is not None
        )

        light = self.get_light_snapshot(normalized, now=current_time)
        mark_data = self.client.mark_price(normalized)
        exchange_info = self.get_exchange_info(now=current_time)
        rules = extract_symbol_rules(exchange_info, normalized)

        positions = [] if cached_position is not None else self.client.position_risk(normalized)

        open_orders = (
            cached_open_orders
            if cached_open_orders is not None
            else self.client.open_orders(normalized)
        )
        open_algo_orders = self.client.open_algo_orders(normalized)
        account_info = (
            cached_account_info
            if cached_account_info is not None
            else self.get_account_info(now=current_time)
        )

        snapshot = HeavyMarketSnapshot(
            symbol=normalized,
            market=light.market,
            mark_price=Decimal(str(mark_data.get("markPrice", light.market.price))),
            exchange_rules=rules,
            position=(
                cached_position
                if cached_position is not None
                else extract_position_from_risk(normalized, positions)
            ),
            open_orders=open_orders,
            open_algo_orders=open_algo_orders,
            account_info=account_info,
            timestamp=current_time,
            data_age_seconds=0.0,
            stale=False,
            funding_rate=extract_funding_rate(mark_data),
            next_funding_time=extract_next_funding_time(mark_data),
            index_price=extract_index_price(mark_data),
        )
        snapshot = self._enrich_heavy_snapshot(snapshot)

        self._record_snapshot_event(
            "market_snapshot_loaded",
            snapshot=snapshot,
            source="heavy_rest",
            cache_assisted=cache_assisted,
        )
        return snapshot

    def prefetch_heavy_snapshot(
        self,
        symbol: str,
        *,
        now: float | None = None,
        cached_position: FuturesPosition | None = None,
        cached_account_info: dict[str, Any] | None = None,
    ) -> HeavyMarketSnapshot:
        """Resolve one isolated heavy snapshot without mutating cycle snapshot state."""

        current_time = time() if now is None else float(now)
        if self.snapshot_provider is None:
            return self.get_heavy_snapshot(
                symbol,
                now=current_time,
                cached_position=cached_position,
                cached_account_info=cached_account_info,
            )

        snapshot = self.snapshot_provider.get_heavy_snapshot(
            symbol,
            now=current_time,
            cached_position=cached_position,
            cached_account_info=cached_account_info,
        )
        self._record_snapshot_event(
            "market_snapshot_prefetched",
            snapshot=snapshot,
            source=wire_names.SOURCE_LABEL_PARALLEL_PREFETCH,
            cache_assisted=(cached_position is not None or cached_account_info is not None),
        )
        return self._enrich_heavy_snapshot(snapshot)

    def invalidate_private_state_after_mutation(
        self,
        symbol: str,
        *,
        reason: str,
    ) -> bool:
        provider = self.snapshot_provider
        if provider is None:
            self.last_operational_snapshot = None
            return False

        normalized = normalize_symbol(symbol)
        keys = (
            ACCOUNT,
            ALL_POSITIONS,
            snapshot_position(normalized),
            snapshot_open_orders(normalized),
            snapshot_open_algo_orders(normalized),
        )
        try:
            provider.publisher.invalidate_many(
                keys,
                reason=f"alphora-order-mutation:{reason}",
            )
        except Exception as error:
            observe_event(
                "private_state_invalidation_failed",
                component=__name__,
                category="market_data",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                symbol=normalized,
                reason=str(error),
                error_type=type(error).__name__,
            )
            return False

        self.last_operational_snapshot = None
        observe_event(
            "private_state_invalidated_after_mutation",
            component=__name__,
            category="market_data",
            channel=EventChannel.AUDIT,
            symbol=normalized,
            reason=reason,
            resources=[str(key) for key in keys],
        )
        return True

    def _enrich_light_snapshot(self, snapshot: LightMarketSnapshot) -> LightMarketSnapshot:
        market = enrich_market_snapshot_with_candles(
            snapshot.market,
            candle_data_hub=self.candle_data_hub,
        )
        if market is snapshot.market:
            return snapshot
        return replace(snapshot, market=market)

    def _enrich_heavy_snapshot(self, snapshot: HeavyMarketSnapshot) -> HeavyMarketSnapshot:
        market = enrich_market_snapshot_with_candles(
            snapshot.market,
            candle_data_hub=self.candle_data_hub,
        )
        if market is snapshot.market:
            return snapshot
        return replace(snapshot, market=market)

    def get_exchange_info(
        self,
        *,
        now: float | None = None,
        operational_snapshot=None,
    ) -> dict[str, Any]:
        if self.snapshot_provider is not None:
            data = self.snapshot_provider.get_exchange_info(
                now=now,
                operational_snapshot=operational_snapshot,
            )
            self.last_operational_snapshot = (
                operational_snapshot or self.snapshot_provider.last_snapshot
            )
            return data

        data = self.client.exchange_info()
        return data

    def get_account_info(
        self,
        *,
        now: float | None = None,
        operational_snapshot=None,
    ) -> dict[str, Any]:
        if self.snapshot_provider is not None:
            data = self.snapshot_provider.get_account_info(
                now=now,
                operational_snapshot=operational_snapshot,
            )
            self.last_operational_snapshot = (
                operational_snapshot or self.snapshot_provider.last_snapshot
            )
            return data

        data = self.client.account_info()
        return data

    @staticmethod
    def _record_snapshot_event(
        event_type: str,
        *,
        snapshot: LightMarketSnapshot | HeavyMarketSnapshot,
        source: str,
        severity: str = "INFO",
        **payload: Any,
    ) -> None:
        observe_event(
            event_type,
            component=__name__,
            category="market_data",
            channel=EventChannel.AUDIT,
            severity=severity,
            symbol=snapshot.symbol,
            source=source,
            data_age_seconds=snapshot.data_age_seconds,
            stale=snapshot.stale,
            timestamp=snapshot.timestamp,
            **payload,
        )
