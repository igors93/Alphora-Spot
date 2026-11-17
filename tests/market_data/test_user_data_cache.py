from decimal import Decimal

from domain.models import FuturesPosition, PositionDirection
from governor.user_data_cache import UserDataShadowCache
from governor.user_data_stream import parse_user_data_stream_payload


def _event(payload):
    event = parse_user_data_stream_payload(payload, received_at=100.0)
    assert event is not None
    return event.to_dict()


def test_user_data_shadow_cache_updates_order_from_order_trade_update():
    cache = UserDataShadowCache()

    update = cache.apply_event(
        _event(
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": 1710000000000,
                "o": {
                    "s": "BTCUSDT",
                    "i": 123,
                    "c": "client-1",
                    "x": "TRADE",
                    "X": "FILLED",
                    "S": "BUY",
                    "o": "MARKET",
                    "q": "0.01",
                    "z": "0.01",
                    "ap": "100.5",
                    "rp": "1.23",
                },
            }
        )
    )

    assert update is not None
    assert update.order_cache_updated is True
    assert update.position_cache_updated is False
    assert update.dirty_position_symbols == ["BTCUSDT"]
    assert update.account_dirty is True

    assert "BTCUSDT" in cache.orders_by_symbol
    assert "123" in cache.orders_by_symbol["BTCUSDT"]
    assert cache.orders_by_symbol["BTCUSDT"]["123"]["order_status"] == "FILLED"


def test_user_data_shadow_cache_updates_positions_and_balances_from_account_update():
    cache = UserDataShadowCache()

    update = cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "E": 1710000000000,
                "a": {
                    "m": "ORDER",
                    "B": [
                        {
                            "a": "USDT",
                            "wb": "100",
                            "cw": "90",
                            "bc": "1",
                        }
                    ],
                    "P": [
                        {
                            "s": "ETHUSDT",
                            "pa": "0.02",
                            "ep": "2000",
                            "up": "3.5",
                            "ps": "BOTH",
                        }
                    ],
                },
            }
        )
    )

    assert update is not None
    assert update.position_cache_updated is True
    assert update.account_cache_updated is True
    assert update.account_dirty is False

    assert cache.positions_by_symbol["ETHUSDT"]["position_amount"] == "0.02"
    assert cache.balances_by_asset["USDT"]["wallet_balance"] == "100"


def test_user_data_shadow_cache_ignores_unknown_event_without_update_flags():
    cache = UserDataShadowCache()

    update = cache.apply_event(
        _event(
            {
                "e": "UNKNOWN_PRIVATE_EVENT",
                "E": 1710000000000,
                "s": "BTCUSDT",
            }
        )
    )

    assert update is None
    assert cache.summary()["orders_total_count"] == 0
    assert cache.summary()["positions_count"] == 0


def test_user_data_shadow_cache_updates_margin_call_positions():
    cache = UserDataShadowCache()

    update = cache.apply_event(
        _event(
            {
                "e": "MARGIN_CALL",
                "E": 1710000000000,
                "p": [
                    {
                        "s": "BTCUSDT",
                        "pa": "0.01",
                        "ep": "100",
                        "up": "-10",
                        "ps": "BOTH",
                    }
                ],
            }
        )
    )

    assert update is not None
    assert update.position_cache_updated is True
    assert update.account_cache_updated is True
    assert update.severity == "CRITICAL"

    assert cache.positions_by_symbol["BTCUSDT"]["position_amount"] == "0.01"
    assert cache.account_meta["last_margin_call_at"] == 100.0


def test_user_data_shadow_cache_builds_futures_position_for_symbol():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {
                    "m": "ORDER",
                    "P": [
                        {
                            "s": "BTCUSDT",
                            "pa": "0.02",
                            "ep": "100",
                            "up": "1.5",
                        }
                    ],
                },
            }
        )
    )

    position = cache.futures_position_for_symbol("BTCUSDT")

    assert position is not None
    assert position.symbol == "BTCUSDT"
    assert str(position.quantity) == "0.02"
    assert str(position.entry_price) == "100"
    assert str(position.unrealized_pnl) == "1.5"
    assert position.direction.value == "LONG"


def test_user_data_shadow_cache_builds_account_info_snapshot():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {
                    "m": "ORDER",
                    "B": [
                        {
                            "a": "USDT",
                            "wb": "100",
                            "cw": "90",
                            "bc": "1",
                        }
                    ],
                },
            }
        )
    )

    account_info = cache.account_info_snapshot()

    assert account_info is not None
    assert account_info["source"] == "user_data_shadow_cache"
    assert account_info["shadow_only"] is True
    assert account_info["runtime_balance_fields_trusted"] is False
    assert account_info["assets"][0]["asset"] == "USDT"
    assert account_info["assets"][0]["walletBalance"] == "100"
    assert account_info["assets"][0]["crossWalletBalance"] == "90"
    assert account_info["assets"][0]["runtimeBalanceFieldsTrusted"] is False
    assert "availableBalance" not in account_info["assets"][0]
    assert "crossUnPnl" not in account_info["assets"][0]


def test_user_data_shadow_cache_builds_open_orders_snapshot():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ORDER_TRADE_UPDATE",
                "o": {
                    "s": "BTCUSDT",
                    "i": 123,
                    "c": "client-123",
                    "x": "NEW",
                    "X": "NEW",
                    "S": "BUY",
                    "o": "LIMIT",
                    "q": "0.01",
                    "p": "100",
                    "z": "0",
                },
            }
        )
    )

    open_orders = cache.open_orders_snapshot("BTCUSDT")

    assert len(open_orders) == 1
    assert open_orders[0]["symbol"] == "BTCUSDT"
    assert open_orders[0]["orderId"] == "123"
    assert open_orders[0]["clientOrderId"] == "client-123"
    assert open_orders[0]["status"] == "NEW"
    assert open_orders[0]["source"] == "user_data_shadow_cache"


def test_user_data_shadow_cache_open_orders_snapshot_ignores_filled_orders():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ORDER_TRADE_UPDATE",
                "o": {
                    "s": "BTCUSDT",
                    "i": 123,
                    "c": "client-123",
                    "x": "TRADE",
                    "X": "FILLED",
                    "S": "BUY",
                    "o": "MARKET",
                    "q": "0.01",
                    "z": "0.01",
                },
            }
        )
    )

    assert cache.open_orders_snapshot("BTCUSDT") == []


def test_user_data_shadow_cache_reconciles_position_from_rest_and_clears_dirty():
    cache = UserDataShadowCache()
    cache.dirty_position_symbols.add("BTCUSDT")

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.02"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("1.5"),
    )

    assert (
        cache.reconcile_position_from_rest(
            symbol="BTCUSDT",
            position=position,
            received_at=101.0,
        )
        is True
    )

    assert "BTCUSDT" not in cache.dirty_position_symbols
    assert cache.positions_by_symbol["BTCUSDT"]["position_amount"] == "0.02"
    assert cache.positions_by_symbol["BTCUSDT"]["source"] == "rest_reconciliation"


def test_user_data_shadow_cache_reconciles_account_from_rest_and_clears_dirty():
    cache = UserDataShadowCache()
    cache.account_dirty = True

    assert (
        cache.reconcile_account_from_rest(
            account_info={
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "100",
                        "crossWalletBalance": "90",
                    }
                ]
            },
            received_at=101.0,
        )
        is True
    )

    assert cache.account_dirty is False
    assert cache.balances_by_asset["USDT"]["wallet_balance"] == "100"
    assert cache.balances_by_asset["USDT"]["source"] == "rest_reconciliation"


def test_user_data_shadow_cache_reconciles_open_orders_from_rest():
    cache = UserDataShadowCache()

    assert (
        cache.reconcile_open_orders_from_rest(
            symbol="BTCUSDT",
            open_orders=[
                {
                    "symbol": "BTCUSDT",
                    "orderId": 123,
                    "clientOrderId": "client-123",
                    "side": "BUY",
                    "type": "LIMIT",
                    "status": "NEW",
                    "origQty": "0.01",
                    "price": "100",
                    "executedQty": "0",
                }
            ],
            received_at=101.0,
        )
        is True
    )

    open_orders = cache.open_orders_snapshot("BTCUSDT")

    assert len(open_orders) == 1
    assert open_orders[0]["orderId"] == "123"
    assert open_orders[0]["source"] == "user_data_shadow_cache"
