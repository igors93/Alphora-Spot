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


class FakeUserDataStreamHub:
    def __init__(self, **health_overrides):
        self.health_overrides = health_overrides

    def health_snapshot(self, now=None):
        data = {
            "enabled": True,
            "running": True,
            "connected": True,
            "last_message_age_seconds": 1.0,
        }
        data.update(self.health_overrides)
        return data


def _config(**overrides):
    config = {
        "user_data_stream_cache_usage_shadow_enabled": True,
        "user_data_stream_cache_usage_require_audit_match": True,
        "user_data_stream_cache_usage_max_age_seconds": 10.0,
        "user_data_stream_cache_usage_max_audit_age_seconds": 60.0,
    }
    config.update(overrides)
    return SimpleNamespace(**config)


def _service(**config_overrides):
    return UserDataCacheService(
        config=_config(**config_overrides),
        journal=FakeJournal(),
        logger=FakeLogger(),
        cache=UserDataShadowCache(),
        hub=FakeUserDataStreamHub(),
        audits={},
    )


def _event(payload):
    event = parse_user_data_stream_payload(payload, received_at=100.0)
    assert event is not None
    return event.to_dict()


def _seed_clean_cache(service):
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
    service.audits["BTCUSDT"] = {
        "status": "MATCH",
        "recorded_at": 100.5,
    }


def test_governor_records_user_data_cache_usage_shadow_eligible():
    service = _service()
    _seed_clean_cache(service)

    service.record_usage_shadow(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        now=101.0,
    )

    assert service.journal.events

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_usage_shadow"
    assert data["status"] == "ELIGIBLE"
    assert data["reason"] == "CACHE_ELIGIBLE"
    assert data["would_use_cache"] is True
    assert data["fallback_required"] is False
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_records_user_data_cache_usage_shadow_ineligible():
    service = _service()
    _seed_clean_cache(service)
    service.hub = FakeUserDataStreamHub(connected=False)

    service.record_usage_shadow(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        now=101.0,
    )

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_usage_shadow"
    assert data["status"] == "INELIGIBLE"
    assert data["reason"] == "USER_DATA_STREAM_NOT_CONNECTED"
    assert data["would_use_cache"] is False
    assert data["fallback_required"] is True


def test_governor_does_not_record_user_data_cache_usage_when_disabled():
    service = _service(user_data_stream_cache_usage_shadow_enabled=False)
    _seed_clean_cache(service)

    service.record_usage_shadow(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        now=101.0,
    )

    assert service.journal.events == []
