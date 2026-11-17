from types import SimpleNamespace

from governor.services import UserDataCacheService
from governor.user_data_cache import UserDataShadowCache
from governor.user_data_stream import parse_user_data_stream_payload


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


def _service(**config_overrides):
    config = {
        "user_data_stream_cache_position_enabled": True,
    }
    config.update(config_overrides)
    return UserDataCacheService(
        config=SimpleNamespace(**config),
        journal=FakeJournal(),
        cache=UserDataShadowCache(),
    )


def _event(payload):
    event = parse_user_data_stream_payload(payload, received_at=100.0)
    assert event is not None
    return event.to_dict()


def _seed_position(service):
    service.cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {
                    "m": "ORDER",
                    "P": [{"s": "BTCUSDT", "pa": "0.01", "ep": "100"}],
                },
            }
        )
    )


def test_governor_returns_cached_position_when_plan_allows_it():
    service = _service()
    _seed_position(service)
    assisted_plan = SimpleNamespace(would_use_position_cache=True)

    position = service.cached_position(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert position is not None
    assert position.symbol == "BTCUSDT"
    assert str(position.quantity) == "0.01"

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_position_used"
    assert data["status"] == "USED"
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_does_not_use_cached_position_when_disabled():
    service = _service(user_data_stream_cache_position_enabled=False)
    _seed_position(service)
    assisted_plan = SimpleNamespace(would_use_position_cache=True)

    position = service.cached_position(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert position is None
    assert service.journal.events == []


def test_governor_does_not_use_cached_position_when_plan_disallows_it():
    service = _service()
    _seed_position(service)
    assisted_plan = SimpleNamespace(would_use_position_cache=False)

    position = service.cached_position(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert position is None
    assert service.journal.events == []
