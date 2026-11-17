from types import SimpleNamespace

from governor.composition import _build_user_data_stream_hub
from governor.services import UserDataCacheService


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


class FakeLogger:
    def warning(self, *_args, **_kwargs):
        return None


class FakeClient:
    pass


def _config(**overrides):
    config = {
        "user_data_stream_enabled": True,
        "user_data_stream_shadow_only": True,
        "user_data_stream_ws_base_url": "wss://example.test",
        "user_data_stream_keepalive_seconds": 1800.0,
        "user_data_stream_reconnect_seconds": 5.0,
        "user_data_stream_heartbeat_seconds": 60.0,
        "user_data_stream_max_pending_events": 500,
    }
    config.update(overrides)
    return SimpleNamespace(**config)


def _build_hub(**config_overrides):
    return _build_user_data_stream_hub(
        config=_config(**config_overrides),
        client=FakeClient(),
        event_callback=lambda _event: None,
    )


def _service(**config_overrides):
    return UserDataCacheService(
        config=_config(**config_overrides),
        journal=FakeJournal(),
        logger=FakeLogger(),
    )


def test_governor_builds_user_data_stream_hub_when_enabled():
    hub = _build_hub()

    assert hub is not None
    assert hub.base_url == "wss://example.test"
    assert hub.shadow_only is True


def test_governor_does_not_build_user_data_stream_hub_when_disabled():
    assert _build_hub(user_data_stream_enabled=False) is None


def test_governor_records_user_data_stream_event_shadow():
    service = _service()

    service.record_stream_event(
        {
            "event_type": "ORDER_TRADE_UPDATE",
            "symbol": "BTCUSDT",
            "event_time": 100.0,
            "transaction_time": 101.0,
            "received_at": 102.0,
            "shadow_only": True,
            "source": "user_data_stream",
            "classification": {
                "category": "ORDER",
                "operational_type": "ORDER_TRADE_FILL",
                "severity": "INFO",
                "symbol": "BTCUSDT",
                "symbols": ["BTCUSDT"],
                "summary": "Ordem recebeu execucao/fill.",
                "should_update_order_cache": True,
                "should_update_position_cache": True,
                "should_update_account_cache": True,
            },
            "operational_category": "ORDER",
            "operational_type": "ORDER_TRADE_FILL",
            "severity": "INFO",
            "raw": {"e": "ORDER_TRADE_UPDATE"},
        }
    )

    assert service.journal.events

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_event_shadow"
    assert data["status"] == "OBSERVED"
    assert data["user_event_type"] == "ORDER_TRADE_UPDATE"
    assert data["symbol"] == "BTCUSDT"
    assert data["operational_category"] == "ORDER"
    assert data["operational_type"] == "ORDER_TRADE_FILL"
    assert data["severity"] == "INFO"
    assert data["should_update_order_cache"] is True
    assert data["should_update_position_cache"] is True
    assert data["should_update_account_cache"] is True
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False
