from decimal import Decimal

from domain.models import FuturesPosition, PositionDirection
from governor.market_data_router import MarketDataRouter


class FakeClient:
    def __init__(self):
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
        return {"assets": []}


def test_heavy_snapshot_uses_cached_position_without_position_risk_call():
    client = FakeClient()
    router = MarketDataRouter(client)

    cached_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.01"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    heavy = router.get_heavy_snapshot(
        "BTCUSDT",
        now=100.0,
        cached_position=cached_position,
    )

    assert heavy.position == cached_position
    assert client.position_risk_calls == 0


def test_heavy_snapshot_uses_rest_position_when_cached_position_is_missing():
    client = FakeClient()
    router = MarketDataRouter(client)

    router.get_heavy_snapshot("BTCUSDT", now=100.0)

    assert client.position_risk_calls == 1


def test_cached_position_snapshot_does_not_pollute_heavy_cache():
    client = FakeClient()
    router = MarketDataRouter(client)

    cached_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.01"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    router.get_heavy_snapshot(
        "BTCUSDT",
        now=100.0,
        cached_position=cached_position,
    )

    router.get_heavy_snapshot("BTCUSDT", now=101.0)

    assert client.position_risk_calls == 1
