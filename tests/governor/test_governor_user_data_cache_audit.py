from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, PositionDirection
from governor.services import UserDataCacheService
from governor.user_data_cache import UserDataShadowCache
from governor.user_data_stream import parse_user_data_stream_payload


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


class FakeLogger:
    def warning(self, *_args, **_kwargs):
        return None


def _service(**config_overrides):
    config = {
        "user_data_stream_cache_audit_enabled": True,
        "user_data_stream_cache_audit_max_age_seconds": 30.0,
    }
    config.update(config_overrides)
    return UserDataCacheService(
        config=SimpleNamespace(**config),
        journal=FakeJournal(),
        logger=FakeLogger(),
        cache=UserDataShadowCache(),
    )


def _event(payload):
    event = parse_user_data_stream_payload(payload, received_at=100.0)
    assert event is not None
    return event.to_dict()


def _heavy(position_amount="0.01"):
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
        open_orders=[],
        account_info={
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "100",
                }
            ]
        },
    )


def test_governor_records_user_data_cache_shadow_audit():
    service = _service()
    service.cache.apply_event(
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

    service.record_shadow_audit(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        heavy=_heavy("0.01"),
        now=101.0,
    )

    assert service.journal.events

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_shadow_audit"
    assert data["status"] == "MATCH"
    assert data["symbol"] == "BTCUSDT"
    assert data["position_match"] is True
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_does_not_record_cache_audit_when_disabled():
    service = _service(user_data_stream_cache_audit_enabled=False)
    service.cache.apply_event(
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

    service.record_shadow_audit(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        heavy=_heavy("0.01"),
        now=101.0,
    )

    assert service.journal.events == []
