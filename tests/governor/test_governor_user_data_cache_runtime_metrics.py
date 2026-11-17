from types import SimpleNamespace

from governor.services import UserDataCacheService


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


def _service(**config_overrides):
    config = {
        "execution_mode": "TESTNET_LIVE",
        "environment": "TESTNET",
        "user_data_cache_operational_preset": "TESTNET_FULL_CACHE",
        "user_data_cache_effective_preset": "TESTNET_FULL_CACHE",
        "user_data_stream_cache_position_enabled": True,
        "user_data_stream_cache_account_enabled": True,
        "user_data_stream_cache_open_orders_enabled": True,
    }
    config.update(config_overrides)
    return UserDataCacheService(
        config=SimpleNamespace(**config),
        journal=FakeJournal(),
    )


def test_governor_records_runtime_metrics_when_cache_saves_rest():
    service = _service()

    service.record_runtime_metrics(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=SimpleNamespace(reason="CACHE_ASSISTED_PLAN_READY", details={}),
        position_source="USER_DATA_CACHE",
        account_source="USER_DATA_CACHE",
        open_orders_source="USER_DATA_CACHE",
        heavy_snapshot_duration_ms=12.345,
    )

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_runtime_metrics"
    assert data["symbol"] == "BTCUSDT"
    assert data["position_rest_saved"] is True
    assert data["account_rest_saved"] is True
    assert data["open_orders_rest_saved"] is True
    assert data["rest_saved_count"] == 3
    assert data["fallback_rest_count"] == 0
    assert data["cache_components_used"] == {
        "position": True,
        "account_info": True,
        "open_orders": True,
    }
    assert data["component_rest_reasons"] == {
        "position": "REST_AVOIDED_BY_CACHE",
        "account_info": "REST_AVOIDED_BY_CACHE",
        "open_orders": "REST_AVOIDED_BY_CACHE",
    }
    assert data["rest_usage_counts"]["rest_avoided_by_cache"] == 3
    assert data["heavy_snapshot_rest_calls"]["mark_price"] == "REST_MANDATORY"
    assert data["heavy_snapshot_rest_calls"]["open_algo_orders"] == "REST_MANDATORY"
    assert data["position_source"] == "USER_DATA_CACHE"
    assert data["account_source"] == "USER_DATA_CACHE"
    assert data["open_orders_source"] == "USER_DATA_CACHE"
    assert data["heavy_snapshot_duration_ms"] == 12.345
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False
    assert data["observability_only"] is True


def test_governor_records_runtime_metrics_when_rest_fallback_is_used():
    service = _service(
        user_data_cache_operational_preset="SHADOW_OBSERVE",
        user_data_cache_effective_preset="SHADOW_OBSERVE",
        user_data_stream_cache_position_enabled=False,
        user_data_stream_cache_account_enabled=False,
        user_data_stream_cache_open_orders_enabled=False,
    )

    service.record_runtime_metrics(
        symbol_config=SimpleNamespace(symbol="ETHUSDT"),
        assisted_plan=SimpleNamespace(reason="CACHE_NOT_AVAILABLE", details={}),
        position_source="REST",
        account_source="REST",
        open_orders_source="REST",
        heavy_snapshot_duration_ms=8.0,
    )

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_runtime_metrics"
    assert data["symbol"] == "ETHUSDT"
    assert data["position_rest_saved"] is False
    assert data["account_rest_saved"] is False
    assert data["open_orders_rest_saved"] is False
    assert data["rest_saved_count"] == 0
    assert data["fallback_rest_count"] == 3
    assert data["cache_components_used"] == {
        "position": False,
        "account_info": False,
        "open_orders": False,
    }
    assert data["cache_components_enabled"] == {
        "position": False,
        "account_info": False,
        "open_orders": False,
    }
    assert data["component_rest_reasons"] == {
        "position": "REST_USED_CACHE_DISABLED",
        "account_info": "REST_USED_CACHE_DISABLED",
        "open_orders": "REST_USED_CACHE_DISABLED",
    }
    assert data["rest_usage_counts"]["rest_used_cache_disabled"] == 3
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_runtime_metrics_distinguish_guard_fallback_from_disabled_cache():
    service = _service()
    assisted_plan = SimpleNamespace(
        reason="CACHE_ASSISTED_PLAN_PARTIAL",
        details={
            "component_fallback_reasons": {
                "account_info": "CACHE_ACCOUNT_DIRTY",
                "open_orders": "AUDIT_ORDERS_MISMATCH",
            },
        },
    )

    service.record_runtime_metrics(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        assisted_plan=assisted_plan,
        position_source="USER_DATA_CACHE",
        account_source="REST",
        open_orders_source="REST",
        heavy_snapshot_duration_ms=10.0,
    )

    _event_type, data = service.journal.events[0]

    assert data["component_rest_reasons"] == {
        "position": "REST_AVOIDED_BY_CACHE",
        "account_info": "REST_USED_FALLBACK_GUARD_AUDIT_STALE",
        "open_orders": "REST_USED_FALLBACK_GUARD_AUDIT_STALE",
    }
    assert data["rest_saved_count"] == 1
    assert data["fallback_rest_count"] == 2
    assert data["rest_usage_counts"]["rest_avoided_by_cache"] == 1
    assert data["rest_usage_counts"]["rest_used_fallback_guard_audit_stale"] == 2
