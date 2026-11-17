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
        "user_data_stream_cache_account_enabled": True,
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


def _seed_account(service):
    service.cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {
                    "m": "ORDER",
                    "B": [{"a": "USDT", "wb": "100", "cw": "90"}],
                },
            }
        )
    )


def _reconcile_trusted_account(service):
    service.cache.reconcile_account_from_rest(
        account_info={
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "100",
                    "availableBalance": "95",
                    "marginBalance": "100",
                    "crossUnPnl": "0",
                    "unrealizedProfit": "0",
                }
            ]
        },
        received_at=100.2,
    )


def test_governor_returns_cached_account_when_plan_allows_it():
    service = _service()
    _seed_account(service)
    _reconcile_trusted_account(service)
    assisted_plan = SimpleNamespace(would_use_account_cache=True)

    account_info = service.cached_account_info(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert account_info is not None
    assert account_info["assets"][0]["asset"] == "USDT"
    assert account_info["assets"][0]["walletBalance"] == "100"

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_account_used"
    assert data["status"] == "USED"
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_falls_back_when_cached_account_balance_fields_are_untrusted():
    service = _service()
    _seed_account(service)
    assisted_plan = SimpleNamespace(would_use_account_cache=True)

    account_info = service.cached_account_info(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert account_info is None

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_account_fallback"
    assert data["status"] == "REST_FALLBACK"
    assert data["reason"] == "ACCOUNT_RUNTIME_BALANCE_FIELDS_UNTRUSTED"
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_does_not_use_cached_account_when_disabled():
    service = _service(user_data_stream_cache_account_enabled=False)
    _seed_account(service)
    assisted_plan = SimpleNamespace(would_use_account_cache=True)

    account_info = service.cached_account_info(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert account_info is None
    assert service.journal.events == []


def test_governor_does_not_use_cached_account_when_plan_disallows_it():
    service = _service()
    _seed_account(service)
    assisted_plan = SimpleNamespace(would_use_account_cache=False)

    account_info = service.cached_account_info(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
    )

    assert account_info is None
    assert service.journal.events == []
