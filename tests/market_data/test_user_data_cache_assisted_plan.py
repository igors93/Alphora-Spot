from governor.user_data_cache import UserDataShadowCache
from governor.user_data_cache_assisted_plan import (
    build_user_data_cache_assisted_plan_shadow,
)
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


def _eligible_usage(cache, *, last_audit=None):
    return evaluate_user_data_cache_usage_shadow(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(),
        last_audit=last_audit
        or {
            "status": "MATCH",
            "recorded_at": 100.5,
            "position_match": True,
            "orders_match": True,
            "account_match": True,
        },
        max_cache_age_seconds=10.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
    )


def _reconcile_trusted_account(cache):
    cache.reconcile_account_from_rest(
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


def test_cache_assisted_plan_ready_when_usage_is_eligible_and_cache_has_data():
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
    _reconcile_trusted_account(cache)
    usage = _eligible_usage(cache)

    plan = build_user_data_cache_assisted_plan_shadow(
        cache=cache,
        symbol="BTCUSDT",
        usage_evaluation=usage,
    )

    assert plan.status == "READY"
    assert plan.reason == "CACHE_ASSISTED_PLAN_READY"
    assert plan.would_use_position_cache is True
    assert plan.would_use_account_cache is True
    assert plan.would_use_order_cache is True
    assert plan.fallback_rest_required is False


def test_cache_assisted_plan_is_ineligible_when_usage_is_ineligible():
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

    usage = evaluate_user_data_cache_usage_shadow(
        cache=cache,
        symbol="BTCUSDT",
        now=101.0,
        stream_health=_stream_health(connected=False),
        last_audit={
            "status": "MATCH",
            "recorded_at": 100.5,
            "position_match": True,
            "orders_match": True,
            "account_match": True,
        },
        max_cache_age_seconds=10.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
    )

    plan = build_user_data_cache_assisted_plan_shadow(
        cache=cache,
        symbol="BTCUSDT",
        usage_evaluation=usage,
    )

    assert plan.status == "INELIGIBLE"
    assert plan.reason == "USER_DATA_STREAM_NOT_CONNECTED"
    assert plan.fallback_rest_required is True
    assert plan.would_use_position_cache is False


def test_cache_assisted_plan_partial_when_position_cache_is_missing():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {
                    "m": "ORDER",
                    "B": [{"a": "USDT", "wb": "100"}],
                    "P": [],
                },
            }
        )
    )
    _reconcile_trusted_account(cache)
    usage = _eligible_usage(cache)

    plan = build_user_data_cache_assisted_plan_shadow(
        cache=cache,
        symbol="BTCUSDT",
        usage_evaluation=usage,
    )

    assert plan.status == "PARTIAL"
    assert plan.reason == "CACHE_ASSISTED_PLAN_PARTIAL"
    assert plan.would_use_position_cache is False
    assert plan.would_use_account_cache is True
    assert plan.would_use_order_cache is True
    assert plan.fallback_rest_required is True


def test_cache_assisted_plan_keeps_clean_components_when_account_is_dirty():
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
    _reconcile_trusted_account(cache)
    cache.account_dirty = True
    usage = _eligible_usage(cache)

    plan = build_user_data_cache_assisted_plan_shadow(
        cache=cache,
        symbol="BTCUSDT",
        usage_evaluation=usage,
    )

    assert usage.reason == "CACHE_ACCOUNT_DIRTY"
    assert plan.status == "PARTIAL"
    assert plan.would_use_position_cache is True
    assert plan.would_use_account_cache is False
    assert plan.would_use_order_cache is True
    assert plan.details["component_fallback_reasons"] == {
        "account_info": "CACHE_ACCOUNT_DIRTY",
    }


def test_cache_assisted_plan_dirty_position_is_symbol_scoped():
    cache = UserDataShadowCache()
    cache.apply_event(
        _event(
            {
                "e": "ACCOUNT_UPDATE",
                "a": {
                    "m": "ORDER",
                    "B": [{"a": "USDT", "wb": "100"}],
                    "P": [
                        {"s": "BTCUSDT", "pa": "0.01"},
                        {"s": "ETHUSDT", "pa": "0.02"},
                    ],
                },
            }
        )
    )
    _reconcile_trusted_account(cache)
    cache.dirty_position_symbols.add("BTCUSDT")
    usage = evaluate_user_data_cache_usage_shadow(
        cache=cache,
        symbol="ETHUSDT",
        now=101.0,
        stream_health=_stream_health(),
        last_audit={
            "status": "MATCH",
            "recorded_at": 100.5,
            "position_match": True,
            "orders_match": True,
            "account_match": True,
        },
        max_cache_age_seconds=10.0,
        max_audit_age_seconds=60.0,
        require_audit_match=True,
    )

    plan = build_user_data_cache_assisted_plan_shadow(
        cache=cache,
        symbol="ETHUSDT",
        usage_evaluation=usage,
    )

    assert usage.status == "ELIGIBLE"
    assert usage.dirty_position is False
    assert plan.status == "READY"
    assert plan.would_use_position_cache is True


def test_cache_assisted_plan_keeps_untrusted_account_cache_observational():
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
    usage = _eligible_usage(cache)

    plan = build_user_data_cache_assisted_plan_shadow(
        cache=cache,
        symbol="BTCUSDT",
        usage_evaluation=usage,
    )

    assert plan.status == "PARTIAL"
    assert plan.would_use_position_cache is True
    assert plan.would_use_account_cache is False
    assert plan.details["component_fallback_reasons"] == {
        "account_info": "ACCOUNT_RUNTIME_BALANCE_FIELDS_UNTRUSTED",
    }


def test_cache_assisted_plan_ineligible_without_usage_evaluation():
    plan = build_user_data_cache_assisted_plan_shadow(
        cache=UserDataShadowCache(),
        symbol="BTCUSDT",
        usage_evaluation=None,
    )

    assert plan.status == "INELIGIBLE"
    assert plan.reason == "USAGE_EVALUATION_MISSING"
    assert plan.fallback_rest_required is True
