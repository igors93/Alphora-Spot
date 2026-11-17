from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, PositionDirection
from governor.user_data_cache import UserDataShadowCache
from governor.user_data_cache_reconciliation import reconcile_user_data_cache_from_rest


def _heavy():
    return SimpleNamespace(
        position=FuturesPosition(
            symbol="BTCUSDT",
            direction=PositionDirection.LONG,
            quantity=Decimal("0.02"),
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("1.5"),
        ),
        account_info={
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "100",
                    "crossWalletBalance": "90",
                    "availableBalance": "80",
                    "marginBalance": "95",
                    "crossUnPnl": "5",
                }
            ]
        },
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
    )


def test_reconcile_user_data_cache_from_rest_updates_all_rest_backed_components():
    cache = UserDataShadowCache()
    cache.dirty_position_symbols.add("BTCUSDT")
    cache.account_dirty = True

    result = reconcile_user_data_cache_from_rest(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=_heavy(),
        now=101.0,
        rest_backed_position=True,
        rest_backed_account=True,
        rest_backed_open_orders=True,
    )

    assert result.status == "RECONCILED"
    assert result.position_reconciled is True
    assert result.account_reconciled is True
    assert result.open_orders_reconciled is True
    assert result.dirty_position_cleared is True
    assert result.account_dirty_cleared is True

    assert "BTCUSDT" not in cache.dirty_position_symbols
    assert cache.account_dirty is False
    assert cache.positions_by_symbol["BTCUSDT"]["position_amount"] == "0.02"
    assert cache.balances_by_asset["USDT"]["wallet_balance"] == "100"
    account_info = cache.account_info_snapshot()
    assert account_info["runtime_balance_fields_trusted"] is True
    assert account_info["assets"][0]["availableBalance"] == "80"
    assert account_info["assets"][0]["marginBalance"] == "95"
    assert account_info["assets"][0]["crossUnPnl"] == "5"
    assert len(cache.open_orders_snapshot("BTCUSDT")) == 1


def test_reconcile_user_data_cache_from_rest_skips_cache_backed_components():
    cache = UserDataShadowCache()
    cache.dirty_position_symbols.add("BTCUSDT")
    cache.account_dirty = True

    result = reconcile_user_data_cache_from_rest(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=_heavy(),
        now=101.0,
        rest_backed_position=False,
        rest_backed_account=False,
        rest_backed_open_orders=False,
    )

    assert result.status == "SKIPPED"
    assert result.reason == "NO_REST_BACKED_COMPONENTS"
    assert result.position_reconciled is False
    assert result.account_reconciled is False
    assert result.open_orders_reconciled is False

    assert "BTCUSDT" in cache.dirty_position_symbols
    assert cache.account_dirty is True


def test_reconcile_user_data_cache_from_rest_does_not_reconcile_position_without_rest_source():
    cache = UserDataShadowCache()
    cache.dirty_position_symbols.add("BTCUSDT")

    result = reconcile_user_data_cache_from_rest(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=_heavy(),
        now=101.0,
        rest_backed_position=True,
        rest_backed_account=False,
        rest_backed_open_orders=False,
        position_source="SYNC_POSITION",
        final_position_source="SYNC_POSITION",
    )

    assert result.status == "SKIPPED"
    assert result.reason == "NO_SAFE_REST_SOURCE"
    assert result.position_reconciled is False
    assert result.dirty_position_cleared is False
    assert result.position_source == "SYNC_POSITION"
    assert result.final_position_source == "SYNC_POSITION"
    assert "BTCUSDT" in cache.dirty_position_symbols


def test_reconcile_user_data_cache_from_rest_records_component_sources():
    cache = UserDataShadowCache()

    result = reconcile_user_data_cache_from_rest(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=_heavy(),
        now=101.0,
        rest_backed_position=True,
        rest_backed_account=True,
        rest_backed_open_orders=False,
        position_source="REST",
        account_source="REST",
        open_orders_source="USER_DATA_CACHE",
        final_position_source="LOCAL_STATE",
    )

    assert result.status == "RECONCILED"
    assert result.position_source == "REST"
    assert result.account_source == "REST"
    assert result.open_orders_source == "USER_DATA_CACHE"
    assert result.final_position_source == "LOCAL_STATE"
    assert result.details["source_guard"]["position_source"] == "REST"
