"""Tests: preset DISABLED deve ignorar state_reconciliation_mode ALERT.

Critério obrigatório (problema #2):
    preset DISABLED + state_reconciliation_mode ALERT
    → nenhuma fonte privada local ativa
    → nenhuma publicação REST no UserDataShadowCache
"""

from __future__ import annotations

from config import load_config


def _base_env(monkeypatch):
    values = {
        "BINANCE_FUTURES_API_KEY": "key",
        "BINANCE_FUTURES_API_SECRET": "secret",
        "ALPHORA_EXECUTION_MODE": "TESTNET_LIVE",
        "ALPHORA_ENVIRONMENT": "TESTNET",
        "ALPHORA_MARKET_WS_ENABLED": "false",
        "ALPHORA_MARKET_WS_STREAM": "bookTicker",
        "ALPHORA_SYMBOL": "BTCUSDT",
        "ALPHORA_SYMBOLS": "BTCUSDT",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_disabled_preset_overrides_alert_reconciliation(monkeypatch):
    """preset=DISABLED + reconciliation=ALERT → reconciliation_enabled=False."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_USER_DATA_CACHE_EFFECTIVE_PRESET", "DISABLED")
    monkeypatch.setenv("ALPHORA_STATE_RECONCILIATION_MODE", "ALERT")

    config = load_config()

    assert config.user_data_cache_effective_preset == "DISABLED"
    assert config.user_data_stream_cache_reconciliation_enabled is False, (
        "DISABLED preset deve forçar reconciliation_enabled=False "
        "mesmo quando state_reconciliation_mode=ALERT"
    )


def test_disabled_preset_forces_all_cache_components_off(monkeypatch):
    """preset=DISABLED força todos os componentes de cache privado desabilitados."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_USER_DATA_CACHE_EFFECTIVE_PRESET", "DISABLED")
    # Tentar habilitar componentes explicitamente
    monkeypatch.setenv("ALPHORA_USER_DATA_STREAM_CACHE_POSITION_ENABLED", "true")
    monkeypatch.setenv("ALPHORA_USER_DATA_STREAM_CACHE_ACCOUNT_ENABLED", "true")
    monkeypatch.setenv("ALPHORA_USER_DATA_STREAM_CACHE_OPEN_ORDERS_ENABLED", "true")
    monkeypatch.setenv("ALPHORA_STATE_RECONCILIATION_MODE", "ALERT")

    config = load_config()

    assert config.user_data_stream_cache_position_enabled is False, (
        "preset DISABLED deve forçar position_enabled=False"
    )
    assert config.user_data_stream_cache_account_enabled is False, (
        "preset DISABLED deve forçar account_enabled=False"
    )
    assert config.user_data_stream_cache_open_orders_enabled is False, (
        "preset DISABLED deve forçar open_orders_enabled=False"
    )
    assert config.user_data_stream_cache_reconciliation_enabled is False, (
        "preset DISABLED deve forçar reconciliation_enabled=False"
    )


def test_non_disabled_preset_allows_reconciliation_via_alert_mode(monkeypatch):
    """Quando preset não é DISABLED, o modo ALERT habilita reconciliação normalmente."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_STATE_RECONCILIATION_MODE", "ALERT")
    # Sem preset DISABLED
    monkeypatch.delenv("ALPHORA_USER_DATA_CACHE_EFFECTIVE_PRESET", raising=False)

    config = load_config()

    assert config.user_data_stream_cache_reconciliation_enabled is True, (
        "Com modo ALERT e sem preset DISABLED, reconciliation_enabled deve ser True"
    )


def test_disabled_preset_with_off_reconciliation_still_disabled(monkeypatch):
    """preset=DISABLED + reconciliation=OFF → reconciliation_enabled=False (nenhuma mudança)."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_USER_DATA_CACHE_EFFECTIVE_PRESET", "DISABLED")
    monkeypatch.setenv("ALPHORA_STATE_RECONCILIATION_MODE", "OFF")

    config = load_config()

    assert config.user_data_stream_cache_reconciliation_enabled is False


def test_disabled_preset_via_operational_preset(monkeypatch):
    """Quando apenas operational_preset é DISABLED (sem effective override), o mesmo se aplica."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_USER_DATA_CACHE_OPERATIONAL_PRESET", "DISABLED")
    monkeypatch.delenv("ALPHORA_USER_DATA_CACHE_EFFECTIVE_PRESET", raising=False)
    monkeypatch.setenv("ALPHORA_STATE_RECONCILIATION_MODE", "ALERT")

    config = load_config()

    assert config.user_data_cache_effective_preset == "DISABLED"
    assert config.user_data_stream_cache_reconciliation_enabled is False
