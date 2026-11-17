from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, PositionDirection
from governor.user_data_cache import UserDataShadowCache
from governor.user_data_cache_audit import audit_user_data_shadow_cache
from governor.user_data_stream import parse_user_data_stream_payload


def _event(payload):
    event = parse_user_data_stream_payload(payload, received_at=100.0)
    assert event is not None
    return event.to_dict()


def _heavy(
    *,
    position_amount="0",
    open_orders=None,
    account_info=None,
):
    amount = Decimal(str(position_amount))
    if amount > 0:
        direction = PositionDirection.LONG
    elif amount < 0:
        direction = PositionDirection.SHORT
    else:
        direction = PositionDirection.NONE

    return SimpleNamespace(
        position=FuturesPosition(
            symbol="BTCUSDT",
            direction=direction,
            quantity=abs(amount),
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("0"),
        ),
        open_orders=open_orders or [],
        account_info=account_info
        or {
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "100",
                }
            ]
        },
    )


def test_user_data_cache_audit_matches_position_orders_and_account():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {
                    "m": "ORDER",
                    "B": [{"a": "USDT", "wb": "100"}],
                    "P": [{"s": "BTCUSDT", "pa": "0.01"}],
                },
            }
        )
    )

    result = audit_user_data_shadow_cache(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=_heavy(position_amount="0.01"),
        now=101.0,
        max_cache_age_seconds=30.0,
    )

    assert result.status == "MATCH"
    assert result.position_match is True
    assert result.orders_match is True
    assert result.account_match is True


def test_user_data_cache_audit_detects_position_mismatch():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {
                    "m": "ORDER",
                    "B": [{"a": "USDT", "wb": "100"}],
                    "P": [{"s": "BTCUSDT", "pa": "0.01"}],
                },
            }
        )
    )

    result = audit_user_data_shadow_cache(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=_heavy(position_amount="0"),
        now=101.0,
        max_cache_age_seconds=30.0,
    )

    assert result.status == "MISMATCH"
    assert result.reason == "POSITION_AMOUNT_MISMATCH"
    assert result.position_match is False


def test_user_data_cache_audit_detects_open_order_mismatch():
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
                },
            }
        )
    )

    result = audit_user_data_shadow_cache(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=_heavy(
            open_orders=[
                {
                    "orderId": 456,
                    "clientOrderId": "client-456",
                }
            ],
        ),
        now=101.0,
        max_cache_age_seconds=30.0,
    )

    assert result.status == "MISMATCH"
    assert result.reason == "OPEN_ORDERS_MISMATCH"
    assert result.orders_match is False


def test_user_data_cache_audit_reports_stale_cache():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {
                    "m": "ORDER",
                    "B": [{"a": "USDT", "wb": "100"}],
                    "P": [{"s": "BTCUSDT", "pa": "0.01"}],
                },
            }
        )
    )

    result = audit_user_data_shadow_cache(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=_heavy(position_amount="0.01"),
        now=200.0,
        max_cache_age_seconds=30.0,
    )

    assert result.status == "STALE"
    assert result.reason == "CACHE_STALE_OR_EMPTY"
