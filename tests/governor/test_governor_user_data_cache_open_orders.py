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
        "user_data_stream_cache_open_orders_enabled": True,
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


def _seed_open_order(service):
    service.cache.apply_event(
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
                },
            }
        )
    )


def test_governor_returns_cached_open_orders_when_plan_allows_it():
    service = _service()
    _seed_open_order(service)
    assisted_plan = SimpleNamespace(would_use_order_cache=True)

    open_orders = service.cached_open_orders(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert open_orders is not None
    assert len(open_orders) == 1
    assert open_orders[0]["orderId"] == "123"

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_open_orders_used"
    assert data["status"] == "USED"
    assert data["open_orders_count"] == 1
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_can_return_empty_cached_open_orders_when_plan_allows_it():
    service = _service()
    assisted_plan = SimpleNamespace(would_use_order_cache=True)

    open_orders = service.cached_open_orders(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert open_orders == []

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_open_orders_used"
    assert data["open_orders_count"] == 0


def test_governor_does_not_use_cached_open_orders_when_disabled():
    service = _service(user_data_stream_cache_open_orders_enabled=False)
    _seed_open_order(service)
    assisted_plan = SimpleNamespace(would_use_order_cache=True)

    open_orders = service.cached_open_orders(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert open_orders is None
    assert service.journal.events == []


def test_governor_does_not_use_cached_open_orders_when_plan_disallows_it():
    service = _service()
    _seed_open_order(service)
    assisted_plan = SimpleNamespace(would_use_order_cache=False)

    open_orders = service.cached_open_orders(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert open_orders is None
    assert service.journal.events == []
