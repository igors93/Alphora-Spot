from types import SimpleNamespace

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


def _service():
    return UserDataCacheService(
        config=SimpleNamespace(),
        journal=FakeJournal(),
        logger=FakeLogger(),
        cache=UserDataShadowCache(),
    )


def _event(payload):
    event = parse_user_data_stream_payload(payload, received_at=100.0)
    assert event is not None
    return event.to_dict()


def test_governor_updates_user_data_shadow_cache_after_private_event():
    service = _service()

    service.record_stream_event(
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
                },
            }
        )
    )

    event_types = [event_type for event_type, _data in service.journal.events]

    assert "user_data_stream_event_shadow" in event_types
    assert "user_data_stream_cache_shadow_updated" in event_types
    assert "BTCUSDT" in service.cache.orders_by_symbol


def test_governor_cache_shadow_update_event_is_auditable_and_non_mutating():
    service = _service()

    service.record_stream_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "E": 1710000000000,
                "a": {
                    "m": "ORDER",
                    "B": [{"a": "USDT", "wb": "100"}],
                    "P": [{"s": "ETHUSDT", "pa": "0.02"}],
                },
            }
        )
    )

    cache_events = [
        data
        for event_type, data in service.journal.events
        if event_type == "user_data_stream_cache_shadow_updated"
    ]

    assert cache_events

    data = cache_events[0]

    assert data["status"] == "UPDATED"
    assert data["category"] == "ACCOUNT"
    assert data["operational_type"] == "POSITION_ACCOUNT_UPDATE"
    assert data["position_cache_updated"] is True
    assert data["account_cache_updated"] is True
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_publishes_private_cache_updates_to_snapshot():
    class FakeProvider:
        def __init__(self):
            self.calls = []

        def publish_user_data_cache_update(self, **kwargs):
            self.calls.append(kwargs)

    service = _service()
    provider = FakeProvider()
    service.provider = provider
    event = _event(
        {
            "e": "ACCOUNT_UPDATE",
            "E": 1710000000000,
            "a": {
                "m": "ORDER",
                "B": [{"a": "USDT", "wb": "100"}],
                "P": [{"s": "BTCUSDT", "pa": "0.02"}],
            },
        }
    )

    service.record_stream_event(event)

    assert len(provider.calls) == 1
    assert provider.calls[0]["cache"] is service.cache
    assert provider.calls[0]["event"] == event
    assert provider.calls[0]["update"].position_cache_updated is True
