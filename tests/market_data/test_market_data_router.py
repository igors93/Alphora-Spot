from decimal import Decimal
from types import SimpleNamespace

from domain.candle import Candle
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection
from domain.strategy_requirements import StrategyDataRequirements
from governor.market_data_hub import PriceSnapshot
from governor.market_data_router import MarketDataRouter
from market_data.market_snapshots import HeavyMarketSnapshot, LightMarketSnapshot


class FakeClient:
    def __init__(self):
        self.ticker_calls = 0
        self.heavy_calls = 0

    def ticker_price(self, symbol, *, timeout=None):
        self.ticker_calls += 1
        return {"symbol": symbol, "price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        self.heavy_calls += 1
        return {"symbol": symbol, "markPrice": "100.5"}

    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def position_risk(self, symbol, *, timeout=None):
        return [{"symbol": symbol, "positionAmt": "0"}]

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        return {"assets": [{"asset": "USDT", "availableBalance": "100"}]}


class FakeHub:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = []

    def latest_price(self, symbol, *, now=None):
        self.calls.append((symbol, now))
        if not self.snapshots:
            return None
        snapshot = self.snapshots.pop(0)
        if snapshot is None:
            return None
        return PriceSnapshot(
            symbol=symbol,
            price=Decimal(str(snapshot)),
            bid_price=Decimal(str(snapshot)),
            ask_price=Decimal(str(snapshot)),
            event_time=None,
            received_at=now,
        )


class FakeCandleHub:
    def __init__(self):
        self.requirements = StrategyDataRequirements(
            primary_timeframe="1m",
            required_candles={"1m": 1},
        )
        self.candle = Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=0,
            close_time_ms=59_999,
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("10"),
            is_closed=True,
            source="test",
        )

    def candle_snapshot_for_symbol(self, symbol):
        return {"1m": (self.candle,)}

    def diagnostics_for_symbol(self, symbol):
        return {"1m": {"warmup_complete": True}}

    def requirements_for_symbol(self, symbol):
        return self.requirements


def test_market_data_router_caches_light_snapshots():
    client = FakeClient()
    router = MarketDataRouter(client, light_ttl_seconds=10, snapshot_enabled=False)

    first = router.get_light_snapshot("btcusdt", now=100)
    second = router.get_light_snapshot("BTCUSDT", now=105)

    assert first.market.price == second.market.price
    assert second.data_age_seconds == 5
    assert client.ticker_calls == 1


def test_market_data_router_fetches_heavy_data_only_when_requested():
    client = FakeClient()
    router = MarketDataRouter(client, snapshot_enabled=False)

    heavy = router.get_heavy_snapshot("BTCUSDT", now=100)

    assert heavy.exchange_rules is not None
    assert heavy.position.is_open is False
    assert client.heavy_calls == 1


def test_market_data_router_applies_snapshot_settings_overrides():
    client = FakeClient()
    router = MarketDataRouter(
        client,
        snapshot_settings_overrides={
            "rest_timeout_seconds": 2.8,
            "rest_transport_timeout_seconds": 2.4,
            "rest_connect_timeout_seconds": 0.5,
            "rest_read_timeout_seconds": 1.8,
            "execution_max_age_seconds": 2.5,
            "execution_max_skew_seconds": 2.5,
        },
    )
    try:
        settings = router.snapshot_provider.settings

        assert settings.rest_timeout_seconds == 2.8
        assert settings.rest_transport_timeout_seconds == 2.4
        assert settings.rest_connect_timeout_seconds == 0.5
        assert settings.rest_read_timeout_seconds == 1.8
        assert settings.execution_max_age_seconds == 2.5
        assert settings.execution_max_skew_seconds == 2.5
    finally:
        router.snapshot_provider.close()


def test_market_data_router_uses_recent_websocket_price():
    client = FakeClient()
    hub = FakeHub(["101"])
    router = MarketDataRouter(client, market_data_hub=hub, snapshot_enabled=False)

    snapshot = router.get_light_snapshot("btcusdt", now=100.0)

    assert snapshot.market.price == Decimal("101")
    assert snapshot.timestamp == 100.0
    assert snapshot.data_age_seconds == 0.0
    assert client.ticker_calls == 0


def test_market_data_router_injects_active_candle_snapshot():
    client = FakeClient()
    router = MarketDataRouter(
        client,
        candle_data_hub=FakeCandleHub(),
        snapshot_enabled=False,
    )

    snapshot = router.get_light_snapshot("btcusdt", now=100.0)

    assert snapshot.market.candles["1m"][0].close == Decimal("100")
    assert snapshot.market.primary_timeframe == "1m"
    assert snapshot.market.candle_diagnostics["1m"]["warmup_complete"] is True


def test_market_data_router_updates_price_window_from_websocket():
    client = FakeClient()
    hub = FakeHub(["101", "102", "103"])
    router = MarketDataRouter(
        client,
        market_data_hub=hub,
        light_ttl_seconds=0.0,
        price_window=2,
        snapshot_enabled=False,
    )

    first = router.get_light_snapshot("BTCUSDT", now=100.0)
    second = router.get_light_snapshot("BTCUSDT", now=101.0)
    third = router.get_light_snapshot("BTCUSDT", now=102.0)

    assert first.market.prices == [Decimal("101")]
    assert second.market.prices == [Decimal("101"), Decimal("102")]
    assert third.market.prices == [Decimal("102"), Decimal("103")]
    assert client.ticker_calls == 0


def test_market_data_router_uses_rest_when_websocket_absent():
    client = FakeClient()
    router = MarketDataRouter(client, market_data_hub=FakeHub([None]), snapshot_enabled=False)

    snapshot = router.get_light_snapshot("BTCUSDT", now=100.0)

    assert snapshot.market.price == Decimal("100")
    assert client.ticker_calls == 1


def test_market_data_router_uses_rest_when_websocket_stale():
    client = FakeClient()
    stale_hub = FakeHub([None])
    router = MarketDataRouter(client, market_data_hub=stale_hub, snapshot_enabled=False)

    snapshot = router.get_light_snapshot("BTCUSDT", now=100.0)

    assert snapshot.market.price == Decimal("100")
    assert stale_hub.calls == [("BTCUSDT", 100.0)]
    assert client.ticker_calls == 1


def test_market_data_router_uses_light_cache_when_websocket_temporarily_stale():
    client = FakeClient()
    hub = FakeHub(["101", None])
    router = MarketDataRouter(
        client, market_data_hub=hub, light_ttl_seconds=10, snapshot_enabled=False
    )

    first = router.get_light_snapshot("BTCUSDT", now=100.0)
    second = router.get_light_snapshot("BTCUSDT", now=104.0)

    assert first.market.price == Decimal("101")
    assert second.market.price == Decimal("101")
    assert second.data_age_seconds == 4.0
    assert client.ticker_calls == 0


def test_router_refreshes_stale_light_cycle_snapshot_before_returning_it():
    stale = LightMarketSnapshot(
        symbol="BTCUSDT",
        market=MarketSnapshot(symbol="BTCUSDT", price=Decimal("99"), prices=[Decimal("99")]),
        timestamp=1.0,
        data_age_seconds=20.0,
        stale=True,
    )
    fresh = LightMarketSnapshot(
        symbol="BTCUSDT",
        market=MarketSnapshot(
            symbol="BTCUSDT",
            price=Decimal("100"),
            prices=[Decimal("100")],
        ),
        timestamp=2.0,
        data_age_seconds=0.0,
        stale=False,
    )

    class Provider:
        settings = SimpleNamespace(execution_max_age_seconds=2.0)
        last_snapshot = object()

        def __init__(self):
            self.calls = []

        def get_light_snapshot(self, symbol, *, now=None, operational_snapshot=None):
            self.calls.append((symbol, operational_snapshot))
            return stale if operational_snapshot is not None else fresh

    provider = Provider()
    router = MarketDataRouter(FakeClient(), snapshot_provider=provider)

    result = router.get_light_snapshot("BTCUSDT", operational_snapshot=object())

    assert result is fresh
    assert provider.calls[0][1] is not None
    assert provider.calls[1][1] is None


def test_router_refreshes_stale_heavy_cycle_snapshot_before_returning_it():
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )

    def heavy(price, *, age, stale):
        return HeavyMarketSnapshot(
            symbol="BTCUSDT",
            market=MarketSnapshot(symbol="BTCUSDT", price=price, prices=[price]),
            mark_price=price,
            exchange_rules=None,
            position=position,
            open_orders=[],
            open_algo_orders=[],
            account_info={},
            timestamp=1.0,
            data_age_seconds=age,
            stale=stale,
        )

    stale_snapshot = heavy(Decimal("99"), age=20.0, stale=True)
    fresh_snapshot = heavy(Decimal("100"), age=0.0, stale=False)

    class Provider:
        settings = SimpleNamespace(execution_max_age_seconds=2.0)
        last_snapshot = object()

        def __init__(self):
            self.calls = []

        def get_heavy_snapshot(
            self,
            symbol,
            *,
            now=None,
            cached_position=None,
            cached_account_info=None,
            cached_open_orders=None,
            operational_snapshot=None,
        ):
            self.calls.append((symbol, operational_snapshot))
            return stale_snapshot if operational_snapshot is not None else fresh_snapshot

    provider = Provider()
    router = MarketDataRouter(FakeClient(), snapshot_provider=provider)

    result = router.get_heavy_snapshot("BTCUSDT", operational_snapshot=object())

    assert result is fresh_snapshot
    assert provider.calls[0][1] is not None
    assert provider.calls[1][1] is None


def test_router_invalidates_private_snapshot_resources_after_order_mutation():
    class Publisher:
        def __init__(self):
            self.calls = []

        def invalidate_many(self, keys, *, reason):
            self.calls.append((tuple(keys), reason))

    publisher = Publisher()
    provider = SimpleNamespace(publisher=publisher)
    router = MarketDataRouter(
        FakeClient(),
        snapshot_provider=provider,
        snapshot_enabled=False,
    )
    router.last_operational_snapshot = object()

    invalidated = router.invalidate_private_state_after_mutation(
        "btcusdt",
        reason="POSITION_CHANGED",
    )

    assert invalidated is True
    assert router.last_operational_snapshot is None
    keys, reason = publisher.calls[0]
    assert reason == "alphora-order-mutation:POSITION_CHANGED"
    assert {str(key) for key in keys} == {
        "account:summary",
        "account:positions",
        "account:position:BTCUSDT",
        "orders:open:BTCUSDT",
        "orders:open-algo:BTCUSDT",
    }
