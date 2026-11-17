import json

from governor.user_data_stream import (
    UserDataStreamHub,
    parse_user_data_stream_payload,
)


class FakeClient:
    def __init__(self):
        self.started = 0
        self.keepalive = 0
        self.closed = 0

    def start_user_data_stream(self):
        self.started += 1
        return {"listenKey": "abc123"}

    def keepalive_user_data_stream(self):
        self.keepalive += 1
        return {}

    def close_user_data_stream(self):
        self.closed += 1
        return {}


def test_parse_user_data_stream_order_update_extracts_symbol():
    payload = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1710000000000,
        "T": 1710000000100,
        "o": {
            "s": "BTCUSDT",
            "X": "FILLED",
        },
    }

    event = parse_user_data_stream_payload(payload, received_at=100.0)

    assert event is not None
    assert event.event_type == "ORDER_TRADE_UPDATE"
    assert event.symbol == "BTCUSDT"
    assert event.event_time == 1710000000.0
    assert event.transaction_time == 1710000000.1
    assert event.shadow_only is True
    assert event.classification.category == "ORDER"
    assert event.classification.operational_type == "ORDER_FILL_STATUS"
    assert event.classification.order_status == "FILLED"


def test_parse_user_data_stream_account_update_extracts_first_position_symbol():
    payload = {
        "e": "ACCOUNT_UPDATE",
        "E": 1710000000000,
        "a": {
            "P": [
                {
                    "s": "ETHUSDT",
                    "pa": "0.01",
                }
            ]
        },
    }

    event = parse_user_data_stream_payload(payload, received_at=100.0)

    assert event is not None
    assert event.event_type == "ACCOUNT_UPDATE"
    assert event.symbol == "ETHUSDT"
    assert event.classification.category == "ACCOUNT"
    assert event.classification.operational_type == "POSITION_ACCOUNT_UPDATE"
    assert event.classification.symbols == ["ETHUSDT"]


def test_user_data_stream_hub_ingests_event_and_calls_callback():
    observed = []
    hub = UserDataStreamHub(
        client=FakeClient(),
        base_url="wss://example.test",
        event_callback=observed.append,
    )

    payload = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1710000000000,
        "o": {"s": "BTCUSDT"},
    }

    assert hub.ingest_message(json.dumps(payload), received_at=100.0) is True
    assert len(observed) == 1
    assert observed[0]["event_type"] == "ORDER_TRADE_UPDATE"
    assert observed[0]["symbol"] == "BTCUSDT"

    drained = hub.drain_events()
    assert len(drained) == 1
    assert drained[0]["symbol"] == "BTCUSDT"


def test_user_data_stream_hub_health_snapshot_reports_shadow_state():
    hub = UserDataStreamHub(
        client=FakeClient(),
        base_url="wss://example.test",
        shadow_only=True,
    )
    hub.ingest_message(
        {
            "e": "ACCOUNT_UPDATE",
            "E": 1710000000000,
        },
        received_at=100.0,
    )

    health = hub.health_snapshot(now=101.0)

    assert health["enabled"] is True
    assert health["shadow_only"] is True
    assert health["pending_events"] == 1
    assert health["last_message_age_seconds"] == 1.0
