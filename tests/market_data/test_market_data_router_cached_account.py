from governor.market_data_router import MarketDataRouter


class FakeClient:
    def __init__(self):
        self.account_info_calls = 0
        self.position_risk_calls = 0

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
        self.position_risk_calls += 1
        return []

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        self.account_info_calls += 1
        return {"assets": [{"asset": "USDT", "walletBalance": "REST"}]}


def test_heavy_snapshot_uses_cached_account_info_without_account_info_call():
    client = FakeClient()
    router = MarketDataRouter(client)

    cached_account_info = {
        "assets": [
            {
                "asset": "USDT",
                "walletBalance": "100",
            }
        ],
        "source": "user_data_shadow_cache",
    }

    heavy = router.get_heavy_snapshot(
        "BTCUSDT",
        now=100.0,
        cached_account_info=cached_account_info,
    )

    assert heavy.account_info == cached_account_info
    assert client.account_info_calls == 0


def test_heavy_snapshot_uses_rest_account_info_when_cached_account_is_missing():
    client = FakeClient()
    router = MarketDataRouter(client)

    heavy = router.get_heavy_snapshot("BTCUSDT", now=100.0)

    assert heavy.account_info["assets"][0]["walletBalance"] == "REST"
    assert client.account_info_calls == 1


def test_cache_assisted_snapshot_does_not_pollute_heavy_cache():
    client = FakeClient()
    router = MarketDataRouter(client)

    cached_account_info = {
        "assets": [
            {
                "asset": "USDT",
                "walletBalance": "CACHE",
            }
        ],
        "source": "user_data_shadow_cache",
    }

    router.get_heavy_snapshot(
        "BTCUSDT",
        now=100.0,
        cached_account_info=cached_account_info,
    )

    heavy = router.get_heavy_snapshot("BTCUSDT", now=101.0)

    assert heavy.account_info["assets"][0]["walletBalance"] == "REST"
    assert client.account_info_calls == 1
