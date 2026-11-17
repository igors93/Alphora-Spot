"""Module paths saved with the former ``alphora`` namespace keep working."""

import json

import pytest

from backtesting.domain.requests import BacktestStrategyRequest
from config import load_config
from config.symbol_profile_schema import canonicalize_symbol_profile_payload
from desktop.settings_store import SettingsStore
from module_paths import qualify_module_path
from strategies.futures.fixed.random_timed_signal import RandomTimedSignalStrategy
from strategies.loader import load_strategy

LEGACY = "alphora.strategies.futures.fixed.random_timed_signal"
CURRENT = "strategies.futures.fixed.random_timed_signal"


@pytest.mark.parametrize(
    "path,expected",
    [
        (LEGACY, CURRENT),
        (CURRENT, CURRENT),
        ("alphora.ops.preflight", "ops.preflight"),
        ("alphora.engine", "engine"),
        ("  alphora.strategies.example  ", "strategies.example"),
        ("third_party.strategy", "third_party.strategy"),
        ("", ""),
    ],
)
def test_qualify_module_path(path, expected):
    assert qualify_module_path(path) == expected


def test_strategy_loader_accepts_a_legacy_module_path():
    strategy = load_strategy(LEGACY, "RandomTimedSignalStrategy")

    assert isinstance(strategy, RandomTimedSignalStrategy)


def test_environment_strategy_module_is_qualified(monkeypatch):
    monkeypatch.setattr("config.loaders.load_env_file", lambda: "")
    monkeypatch.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    monkeypatch.setenv("ALPHORA_ENVIRONMENT", "TESTNET")
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_STRATEGY_MODULE", LEGACY)

    assert load_config().strategy_module == CURRENT


def test_saved_gui_settings_are_qualified(tmp_path):
    path = tmp_path / "alphora_gui.local.json"
    path.write_text(json.dumps({"strategy_module": LEGACY}), encoding="utf-8")

    assert SettingsStore(path).load()["strategy_module"] == CURRENT


def test_legacy_profile_strategy_matches_the_qualified_global_strategy():
    result = canonicalize_symbol_profile_payload(
        {"symbol": "BTCUSDT", "strategy_module": LEGACY},
        global_strategy_module=CURRENT,
        global_strategy_class="RandomTimedSignalStrategy",
    )

    assert result.removed_keys == ("strategy_module",)
    assert "strategy_module" not in result.payload


def test_saved_backtest_request_module_is_qualified():
    assert BacktestStrategyRequest(module=LEGACY).module == CURRENT
