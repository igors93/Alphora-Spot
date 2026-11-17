from config import ExecutionMode, load_config


def _real_live_env(monkeypatch):
    values = {
        "BINANCE_FUTURES_API_KEY": "key",
        "BINANCE_FUTURES_API_SECRET": "secret",
        "ALPHORA_ARCHITECTURE_MODE": "INTELLIGENT_FLOW",
        "ALPHORA_INTELLIGENT_ACTIVATION_LEVEL": "",
        "ALPHORA_EXECUTION_MODE": "REAL_LIVE",
        "ALPHORA_ENVIRONMENT": "REAL",
        "ALPHORA_ALLOW_REAL_TRADING": "true",
        "ALPHORA_ALLOW_REAL_USER_DATA_CACHE_ASSISTED": "false",
        "ALPHORA_MARKET_WS_ENABLED": "false",
        "ALPHORA_MARKET_WS_STREAM": "bookTicker",
        "ALPHORA_LEVERAGE": "1",
        "ALPHORA_SYMBOL": "BTCUSDT",
        "ALPHORA_SYMBOLS": "BTCUSDT",
        "ALPHORA_LEARNING_ADVISORY_MODE": "OFF",
        "ALPHORA_STATE_RECONCILIATION_MODE": "ALERT",
        "ALPHORA_USER_DATA_STREAM_ENABLED": "true",
        "ALPHORA_USER_DATA_STREAM_CACHE_AUDIT_ENABLED": "true",
        "ALPHORA_USER_DATA_STREAM_CACHE_USAGE_SHADOW_ENABLED": "true",
        "ALPHORA_USER_DATA_STREAM_CACHE_ASSISTED_PLAN_SHADOW_ENABLED": "true",
        "ALPHORA_USER_DATA_STREAM_CACHE_STALE_GUARD_ENABLED": "true",
        "ALPHORA_USER_DATA_STREAM_CACHE_POSITION_ENABLED": "true",
        "ALPHORA_USER_DATA_STREAM_CACHE_ACCOUNT_ENABLED": "true",
        "ALPHORA_USER_DATA_STREAM_CACHE_OPEN_ORDERS_ENABLED": "true",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_config_blocks_real_user_data_cache_assisted_by_default(monkeypatch):
    _real_live_env(monkeypatch)

    config = load_config()

    assert config.execution_mode is ExecutionMode.REAL_LIVE
    assert config.allow_real_user_data_cache_assisted is False
    assert config.user_data_stream_cache_position_enabled is False
    assert config.user_data_stream_cache_account_enabled is False
    assert config.user_data_stream_cache_open_orders_enabled is False
    assert config.user_data_stream_cache_audit_enabled is True
    assert config.user_data_stream_cache_stale_guard_enabled is True
    assert config.user_data_stream_cache_reconciliation_enabled is True


def test_config_allows_real_user_data_cache_assisted_with_explicit_flag(monkeypatch):
    _real_live_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_ALLOW_REAL_USER_DATA_CACHE_ASSISTED", "true")

    config = load_config()

    assert config.execution_mode is ExecutionMode.REAL_LIVE
    assert config.allow_real_user_data_cache_assisted is True
    assert config.user_data_stream_cache_position_enabled is True
    assert config.user_data_stream_cache_account_enabled is True
    assert config.user_data_stream_cache_open_orders_enabled is True
