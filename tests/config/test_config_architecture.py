import pytest

from config import (
    ArchitectureMode,
    ExecutionMode,
    IntelligentActivationLevel,
    load_config,
)


def _minimal_env(monkeypatch):
    values = {
        "BINANCE_FUTURES_API_KEY": "key",
        "BINANCE_FUTURES_API_SECRET": "secret",
        "ALPHORA_MARKET_WS_ENABLED": "false",
        "ALPHORA_INTELLIGENT_ACTIVATION_LEVEL": "",
        "ALPHORA_EXECUTION_MODE": "DRY_RUN",
        "ALPHORA_ENVIRONMENT": "TESTNET",
        "ALPHORA_ALLOW_REAL_TRADING": "false",
        "ALPHORA_LEARNING_ADVISORY_MODE": "OFF",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_load_config_defaults_to_intelligent_shadow_architecture(monkeypatch):
    _minimal_env(monkeypatch)

    config = load_config()

    assert config.architecture_mode is ArchitectureMode.INTELLIGENT_FLOW
    assert config.intelligent_activation_level is IntelligentActivationLevel.SHADOW_ONLY
    assert config.execution_mode is ExecutionMode.DRY_RUN


def test_intelligent_architecture_defaults_to_testnet_observe(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_ARCHITECTURE_MODE", "INTELLIGENT_FLOW")
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "TESTNET_LIVE")

    config = load_config()

    assert config.architecture_mode is ArchitectureMode.INTELLIGENT_FLOW
    assert config.intelligent_activation_level is (IntelligentActivationLevel.TESTNET_OBSERVE)
    assert config.execution_mode is ExecutionMode.TESTNET_LIVE


def test_intelligent_architecture_accepts_explicit_shadow_only(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_ARCHITECTURE_MODE", "INTELLIGENT_FLOW")
    monkeypatch.setenv("ALPHORA_INTELLIGENT_ACTIVATION_LEVEL", "SHADOW_ONLY")

    config = load_config()

    assert config.architecture_mode is ArchitectureMode.INTELLIGENT_FLOW
    assert config.intelligent_activation_level is IntelligentActivationLevel.SHADOW_ONLY


def test_legacy_architecture_is_rejected(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_ARCHITECTURE_MODE", "LEGACY_FLOW")

    with pytest.raises(ValueError, match="ALPHORA_ARCHITECTURE_MODE invalido"):
        load_config()


def test_paper_active_requires_paper_live(monkeypatch):
    _minimal_env(monkeypatch)
    monkeypatch.setenv("ALPHORA_ARCHITECTURE_MODE", "INTELLIGENT_FLOW")
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "TESTNET_LIVE")
    monkeypatch.setenv("ALPHORA_INTELLIGENT_ACTIVATION_LEVEL", "PAPER_ACTIVE")

    with pytest.raises(ValueError, match="PAPER_ACTIVE"):
        load_config()
