from decimal import Decimal

from domain.models import PositionDirection


class StubPositionLedger:
    """In-memory position projection for tests that bypass engine construction."""

    def __init__(self, position=None):
        self.position = position

    def observe_local_state(self, position):
        self.position = position


class FakeFuturesClient:
    def __init__(self):
        self.price = Decimal("100")
        self.mark = Decimal("100")
        self.direction = PositionDirection.NONE
        self.quantity = Decimal("0")
        self.entry_price = Decimal("0")
        self.open_normal_orders = []
        self.open_algo_order_items = []
        self.new_order_calls = []
        self.new_algo_order_calls = []
        self.cancel_order_calls = []
        self.cancel_algo_order_calls = []
        self.query_order_response = None
        self.query_algo_order_response = None
        self.new_order_error = None
        self.open_orders_error = None
        self.open_algo_orders_error = None
        self.income_history_response = []

    def sync_time(self):
        return None

    def ping(self):
        return {}

    def account_info(self, *, timeout=None):
        return {
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "1000",
                    "availableBalance": "1000",
                    "marginBalance": "1000",
                    "unrealizedProfit": "0",
                }
            ]
        }

    def income_history(self, **params):
        return list(self.income_history_response)

    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                        },
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def get_position_mode(self):
        return {"dualSidePosition": False}

    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": str(self.price)}

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": str(self.mark)}

    def position_risk(self, symbol, *, timeout=None):
        if self.direction is PositionDirection.NONE:
            return [{"symbol": symbol, "positionAmt": "0", "entryPrice": "0"}]

        amount = self.quantity
        if self.direction is PositionDirection.SHORT:
            amount = -amount

        return [
            {
                "symbol": symbol,
                "positionAmt": str(amount),
                "entryPrice": str(self.entry_price),
                "unRealizedProfit": "0",
                "positionSide": "BOTH",
            }
        ]

    def open_orders(self, symbol, *, timeout=None):
        if self.open_orders_error is not None:
            raise self.open_orders_error
        return list(self.open_normal_orders)

    def open_algo_orders(self, symbol, *, timeout=None):
        if self.open_algo_orders_error is not None:
            raise self.open_algo_orders_error
        return list(self.open_algo_order_items)

    def new_order(self, **params):
        self.new_order_calls.append(params)
        if self.new_order_error is not None:
            raise self.new_order_error
        return {
            "status": "FILLED",
            "executedQty": params.get("quantity", "0"),
            "avgPrice": str(self.price),
            "clientOrderId": params.get("newClientOrderId"),
        }

    def new_algo_order(self, **params):
        self.new_algo_order_calls.append(params)
        response = {
            "algoStatus": "NEW",
            "clientAlgoId": params.get("clientAlgoId"),
            "orderType": params.get("type"),
            "symbol": params.get("symbol"),
            "side": params.get("side"),
            "triggerPrice": params.get("triggerPrice"),
            "closePosition": params.get("closePosition"),
            "workingType": params.get("workingType"),
        }
        self.open_algo_order_items.append(response)
        return response

    def cancel_order(self, **params):
        self.cancel_order_calls.append(params)
        return {"status": "CANCELED", **params}

    def cancel_algo_order(self, **params):
        self.cancel_algo_order_calls.append(params)
        return {"algoStatus": "CANCELED", **params}

    def change_margin_type(self, **params):
        return {"code": 200, **params}

    def change_leverage(self, **params):
        return {"leverage": params.get("leverage")}

    def query_order(self, **params):
        if self.query_order_response is None:
            return {"status": "FILLED", "executedQty": "1", **params}
        return self.query_order_response

    def query_algo_order(self, **params):
        if self.query_algo_order_response is None:
            return {"algoStatus": "NEW", **params}
        return self.query_algo_order_response


def candles_from_closes(closes, *, interval: str = "1m", symbol: str = "BTCUSDT") -> list:
    """OHLCV candles around a close series, for tests that train or run the AI model.

    The AI model reads open, high, low and volume as well as the close (IA0.1), so tests
    feed it candles with a real range and a changing volume instead of bare prices.
    """

    from domain.candle import Candle
    from domain.timeframe import timeframe_duration_ms

    duration = timeframe_duration_ms(interval)
    candles = []
    previous = Decimal(str(closes[0]))
    for index, raw_close in enumerate(closes):
        close = Decimal(str(raw_close))
        open_ = previous
        spread = abs(close) * Decimal("0.001") * (1 + index % 3)
        candles.append(
            Candle(
                symbol=symbol,
                interval=interval,
                open_time_ms=index * duration,
                close_time_ms=(index + 1) * duration - 1,
                open=open_,
                high=max(open_, close) + spread,
                low=min(open_, close) - spread,
                close=close,
                volume=Decimal(10 + (index * 7) % 13),
                is_closed=True,
            )
        )
        previous = close
    return candles
