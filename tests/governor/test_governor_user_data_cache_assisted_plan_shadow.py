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
    def health_snapshot(self, now=None):
        return {
            "enabled": True,
            "running": True,
            "connected": True,
            "last_message_age_seconds": 1.0,
        }


def _config(**overrides):
    config = {
        "user_data_stream_cache_usage_shadow_enabled": True,
        "user_data_stream_cache_usage_require_audit_match": True,
        "user_data_stream_cache_usage_max_age_seconds": 10.0,
        "user_data_stream_cache_usage_max_audit_age_seconds": 60.0,
        "user_data_stream_cache_assisted_plan_shadow_enabled": True,
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
    service.audits["BTCUSDT"] = {
        "status": "MATCH",
        "recorded_at": 100.5,
        "position_match": True,
        "orders_match": True,
        "account_match": True,
    }


def test_governor_records_cache_assisted_plan_shadow_ready():
    service = _service()
    _seed_clean_cache(service)

    usage = service.record_usage_shadow(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        now=101.0,
    )
    service.record_assisted_plan_shadow(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        usage_evaluation=usage,
    )

    event_types = [event_type for event_type, _data in service.journal.events]

    assert "user_data_stream_cache_usage_shadow" in event_types
    assert "user_data_stream_cache_assisted_plan_shadow" in event_types

    plan_events = [
        data
        for event_type, data in service.journal.events
        if event_type == "user_data_stream_cache_assisted_plan_shadow"
    ]

    assert plan_events

    data = plan_events[0]

    assert data["status"] == "READY"
    assert data["reason"] == "CACHE_ASSISTED_PLAN_READY"
    assert data["would_use_position_cache"] is True
    assert data["would_use_account_cache"] is True
    assert data["would_use_order_cache"] is True
    assert data["fallback_rest_required"] is False
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_does_not_record_cache_assisted_plan_when_disabled():
    service = _service(
        user_data_stream_cache_assisted_plan_shadow_enabled=False,
    )
    _seed_clean_cache(service)

    usage = service.record_usage_shadow(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        now=101.0,
    )
    service.record_assisted_plan_shadow(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        usage_evaluation=usage,
    )

    event_types = [event_type for event_type, _data in service.journal.events]

    assert "user_data_stream_cache_usage_shadow" in event_types
    assert "user_data_stream_cache_assisted_plan_shadow" not in event_types
