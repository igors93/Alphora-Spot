from governor.market_data_router import MarketDataRouter


class FakeClient:
    def __init__(self):
        self.open_orders_calls = 0

    def ticker_price(self, symbol, *, timeout=None):
        return {"price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        return {"markPrice": "100"}

    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "minQty": "0.001",
                            "stepSize": "0.001",
                        },
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.1",
                        },
                        {
                            "filterType": "MIN_NOTIONAL",
                            "notional": "5",
                        },
                    ],
                }
            ]
        }

    def position_risk(self, symbol, *, timeout=None):
        return []

    def open_orders(self, symbol, *, timeout=None):
        self.open_orders_calls += 1
        return [{"symbol": symbol, "orderId": "REST"}]

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        return {"assets": []}


def test_heavy_snapshot_uses_cached_open_orders_without_open_orders_call():
    client = FakeClient()
    router = MarketDataRouter(client)

    cached_open_orders = [
        {
            "symbol": "BTCUSDT",
            "orderId": "123",
            "clientOrderId": "client-123",
            "status": "NEW",
        }
    ]

    heavy = router.get_heavy_snapshot(
        "BTCUSDT",
        now=100.0,
        cached_open_orders=cached_open_orders,
    )

    assert heavy.open_orders == cached_open_orders
    assert client.open_orders_calls == 0


def test_heavy_snapshot_uses_rest_open_orders_when_cache_is_missing():
    client = FakeClient()
    router = MarketDataRouter(client)

    heavy = router.get_heavy_snapshot("BTCUSDT", now=100.0)

    assert heavy.open_orders[0]["orderId"] == "REST"
    assert client.open_orders_calls == 1


def test_cached_open_orders_snapshot_does_not_pollute_heavy_cache():
    client = FakeClient()
    router = MarketDataRouter(client)

    router.get_heavy_snapshot(
        "BTCUSDT",
        now=100.0,
        cached_open_orders=[],
    )

    heavy = router.get_heavy_snapshot("BTCUSDT", now=101.0)

    assert heavy.open_orders[0]["orderId"] == "REST"
    assert client.open_orders_calls == 1
