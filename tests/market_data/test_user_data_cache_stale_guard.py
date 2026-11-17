from governor.user_data_cache import UserDataShadowCache
from governor.user_data_cache_stale_guard import evaluate_user_data_cache_stale_guard
from governor.user_data_stream import parse_user_data_stream_payload


def _event(payload):
    event = parse_user_data_stream_payload(payload, received_at=100.0)
    assert event is not None
    return event.to_dict()


def _stream_health(**overrides):
    data = {
        "enabled": True,
        "running": True,
        "connected": True,
        "listen_key_active": True,
        "last_message_age_seconds": 1.0,
    }
    data.update(overrides)
    return data


def _seed_clean_cache():
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
    return cache


def test_stale_guard_allows_healthy_cache():
    cache = _seed_clean_cache()

    evaluation = evaluate_user_data_cache_stale_guard(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(),
        last_audit={"status": "MATCH", "recorded_at": 100.5},
        max_cache_age_seconds=10.0,
        max_stream_message_age_seconds=30.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
        require_active_listen_key=True,
    )

    assert evaluation.status == "HEALTHY"
    assert evaluation.reason == "CACHE_GUARD_HEALTHY"
    assert evaluation.cache_allowed is True
    assert evaluation.fallback_required is False


def test_stale_guard_blocks_disconnected_stream():
    cache = _seed_clean_cache()

    evaluation = evaluate_user_data_cache_stale_guard(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(connected=False),
        last_audit={"status": "MATCH", "recorded_at": 100.5},
    )

    assert evaluation.status == "DISCONNECTED"
    assert evaluation.reason == "USER_DATA_STREAM_NOT_CONNECTED"
    assert evaluation.cache_allowed is False
    assert evaluation.fallback_required is True


def test_stale_guard_blocks_inactive_listen_key():
    cache = _seed_clean_cache()

    evaluation = evaluate_user_data_cache_stale_guard(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(listen_key_active=False),
        last_audit={"status": "MATCH", "recorded_at": 100.5},
        require_active_listen_key=True,
    )

    assert evaluation.status == "LISTEN_KEY_INACTIVE"
    assert evaluation.reason == "USER_DATA_STREAM_LISTEN_KEY_INACTIVE"
    assert evaluation.cache_allowed is False


def test_stale_guard_blocks_stale_stream_messages():
    cache = _seed_clean_cache()

    evaluation = evaluate_user_data_cache_stale_guard(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(last_message_age_seconds=45.0),
        last_audit={"status": "MATCH", "recorded_at": 100.5},
        max_stream_message_age_seconds=30.0,
    )

    assert evaluation.status == "STREAM_STALE"
    assert evaluation.reason == "USER_DATA_STREAM_STALE"
    assert evaluation.cache_allowed is False


def test_stale_guard_blocks_audit_mismatch():
    cache = _seed_clean_cache()

    evaluation = evaluate_user_data_cache_stale_guard(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(),
        last_audit={"status": "MISMATCH", "recorded_at": 100.5},
        require_audit_match=True,
    )

    assert evaluation.status == "AUDIT_MISMATCH"
    assert evaluation.reason == "AUDIT_MISMATCH"
    assert evaluation.cache_allowed is False
