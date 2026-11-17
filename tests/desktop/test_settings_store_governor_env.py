"""Regression tests for settings_to_governor_env multi/single symbol handling."""

from desktop.settings_store import (
    SINGLE_SYMBOL_PROFILES_PATH,
    SYMBOL_PROFILES_PATH,
    settings_to_governor_env,
)


def _base_settings() -> dict:
    return {
        "execution_mode": "DRY_RUN",
        "environment": "TESTNET",
        "symbol": "BTCUSDT",
        "strategy_module": "strategies.example",
        "strategy_class": "ExampleStrategy",
    }


def test_governor_env_multi_symbol_enabled_by_default():
    env = settings_to_governor_env(_base_settings())
    assert env["ALPHORA_MULTI_SYMBOL_ENABLED"] == "true"


def test_governor_env_multi_symbol_explicit_true():
    env = settings_to_governor_env(_base_settings(), multi_symbol=True)
    assert env["ALPHORA_MULTI_SYMBOL_ENABLED"] == "true"


def test_governor_env_single_symbol_disabled():
    env = settings_to_governor_env(_base_settings(), multi_symbol=False)
    assert env["ALPHORA_MULTI_SYMBOL_ENABLED"] == "false"


def test_governor_env_single_symbol_uses_provided_profiles_path():
    env = settings_to_governor_env(
        _base_settings(),
        symbol_profiles_path=SINGLE_SYMBOL_PROFILES_PATH,
        multi_symbol=False,
    )
    assert env["ALPHORA_SYMBOL_PROFILES_PATH"] == SINGLE_SYMBOL_PROFILES_PATH
    assert env["ALPHORA_MULTI_SYMBOL_ENABLED"] == "false"


def test_governor_env_multi_symbol_uses_default_profiles_path():
    env = settings_to_governor_env(_base_settings(), multi_symbol=True)
    assert env["ALPHORA_SYMBOL_PROFILES_PATH"] == SYMBOL_PROFILES_PATH


def test_governor_env_contains_execution_mode():
    env = settings_to_governor_env(_base_settings())
    assert "ALPHORA_EXECUTION_MODE" in env
    assert env["ALPHORA_EXECUTION_MODE"] == "DRY_RUN"


def test_governor_env_single_symbol_does_not_pollute_multi_enabled():
    settings = _base_settings()
    single_env = settings_to_governor_env(
        settings,
        symbol_profiles_path=SINGLE_SYMBOL_PROFILES_PATH,
        multi_symbol=False,
    )
    multi_env = settings_to_governor_env(settings, multi_symbol=True)

    assert single_env["ALPHORA_MULTI_SYMBOL_ENABLED"] == "false"
    assert multi_env["ALPHORA_MULTI_SYMBOL_ENABLED"] == "true"
