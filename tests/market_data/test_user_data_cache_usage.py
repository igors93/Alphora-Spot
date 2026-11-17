from governor.user_data_cache import UserDataShadowCache
from governor.user_data_cache_usage import evaluate_user_data_cache_usage_shadow
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
        "last_message_age_seconds": 1.0,
    }
    data.update(overrides)
    return data


def test_cache_usage_shadow_is_eligible_when_cache_is_fresh_and_audit_matches():
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

    evaluation = evaluate_user_data_cache_usage_shadow(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(),
        last_audit={
            "status": "MATCH",
            "recorded_at": 100.5,
        },
        max_cache_age_seconds=10.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
    )

    assert evaluation.status == "ELIGIBLE"
    assert evaluation.reason == "CACHE_ELIGIBLE"
    assert evaluation.would_use_cache is True
    assert evaluation.fallback_required is False


def test_cache_usage_shadow_rejects_stale_cache():
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

    evaluation = evaluate_user_data_cache_usage_shadow(
        cache=cache,
        symbol="BTCUSDT",
        now=120.0,
        stream_health=_stream_health(),
        last_audit={
            "status": "MATCH",
            "recorded_at": 100.5,
        },
        max_cache_age_seconds=10.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
    )

    assert evaluation.status == "INELIGIBLE"
    assert evaluation.reason == "CACHE_STALE"
    assert evaluation.fallback_required is True


def test_cache_usage_shadow_rejects_dirty_position():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ORDER_TRADE_UPDATE",
                "o": {
                    "s": "BTCUSDT",
                    "i": 123,
                    "x": "TRADE",
                    "X": "FILLED",
                },
            }
        )
    )

    evaluation = evaluate_user_data_cache_usage_shadow(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(),
        last_audit={
            "status": "MATCH",
            "recorded_at": 100.5,
        },
        max_cache_age_seconds=10.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
    )

    assert evaluation.status == "INELIGIBLE"
    assert evaluation.reason == "CACHE_DIRTY_POSITION"
    assert evaluation.dirty_position is True


def test_cache_usage_shadow_rejects_audit_mismatch():
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

    evaluation = evaluate_user_data_cache_usage_shadow(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(),
        last_audit={
            "status": "MISMATCH",
            "recorded_at": 100.5,
        },
        max_cache_age_seconds=10.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
    )

    assert evaluation.status == "INELIGIBLE"
    assert evaluation.reason == "AUDIT_MISMATCH"


def test_cache_usage_shadow_rejects_disconnected_stream():
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

    evaluation = evaluate_user_data_cache_usage_shadow(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(connected=False),
        last_audit={
            "status": "MATCH",
            "recorded_at": 100.5,
        },
        max_cache_age_seconds=10.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
    )

    assert evaluation.status == "INELIGIBLE"
    assert evaluation.reason == "USER_DATA_STREAM_NOT_CONNECTED"


def test_cache_usage_shadow_rejects_stale_user_data_stream_messages():
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

    evaluation = evaluate_user_data_cache_usage_shadow(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(last_message_age_seconds=45.0),
        last_audit={
            "status": "MATCH",
            "recorded_at": 100.5,
        },
        max_cache_age_seconds=10.0,
        max_stream_message_age_seconds=30.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
        require_active_listen_key=True,
    )

    assert evaluation.status == "INELIGIBLE"
    assert evaluation.reason == "USER_DATA_STREAM_STALE"
    assert evaluation.would_use_cache is False
    assert evaluation.fallback_required is True
