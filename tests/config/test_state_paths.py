from pathlib import Path

import pytest

from config import load_config
from config.symbol_context import parse_symbol_profile, symbol_config_from_app_config


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("config.loaders.load_env_file", lambda: "")
    for name in ("ALPHORA_STATE_PATH", "ALPHORA_TRADE_JOURNAL_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BINANCE_FUTURES_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_API_SECRET", "secret")
    monkeypatch.setenv("ALPHORA_ALLOW_REAL_TRADING", "true")
    monkeypatch.setenv("ALPHORA_SYMBOL", "BTCUSDT")
    monkeypatch.setenv("ALPHORA_SYMBOLS", "BTCUSDT,ETHUSDT")
    monkeypatch.setenv("ALPHORA_MAX_ACTIVE_SYMBOLS", "2")
    return monkeypatch


def test_live_environments_get_distinct_state_and_journal_paths(isolated_config) -> None:
    isolated_config.setenv("ALPHORA_EXECUTION_MODE", "TESTNET_LIVE")
    testnet = load_config()
    isolated_config.setenv("ALPHORA_EXECUTION_MODE", "REAL_LIVE")
    real = load_config()

    assert testnet.state_path == "runtime/state_btcusdt_testnet.json"
    assert real.state_path == "runtime/state_btcusdt_real.json"
    assert testnet.trade_journal_path == "data/trades_btcusdt_testnet.jsonl"
    assert real.trade_journal_path == "data/trades_btcusdt_real.jsonl"
    assert testnet.symbol_contexts[1].state_path.endswith("state_ethusdt_testnet.json")
    assert real.symbol_contexts[1].state_path.endswith("state_ethusdt_real.json")
    assert testnet.symbol_contexts[1].journal_path.endswith("trades_ethusdt_testnet.jsonl")
    assert real.symbol_contexts[1].journal_path.endswith("trades_ethusdt_real.jsonl")
    assert (
        symbol_config_from_app_config(testnet).state_path
        != symbol_config_from_app_config(real).state_path
    )

    Path(testnet.state_path).parent.mkdir(parents=True)
    Path(testnet.state_path).write_text("testnet-only", encoding="utf-8")
    assert not Path(real.state_path).exists()


def test_non_live_modes_do_not_reuse_live_financial_state(isolated_config) -> None:
    paths = set()
    for mode in ("DRY_RUN", "PAPER_LIVE", "TESTNET_LIVE"):
        isolated_config.setenv("ALPHORA_EXECUTION_MODE", mode)
        config = load_config()
        assert config.state_path not in paths
        assert config.trade_journal_path not in paths
        secondary = symbol_config_from_app_config(config, symbol="ETHUSDT")
        assert config.symbol_contexts[1].state_path == secondary.state_path
        assert config.symbol_contexts[1].lock_path == secondary.lock_path
        paths.add(config.state_path)
        paths.add(config.trade_journal_path)


def test_profile_without_explicit_paths_uses_mode_specific_defaults(isolated_config) -> None:
    isolated_config.setenv("ALPHORA_EXECUTION_MODE", "DRY_RUN")
    dry_config = load_config()
    dry_profile = parse_symbol_profile({"symbol": "ETHUSDT"}, base_config=dry_config)

    isolated_config.setenv("ALPHORA_EXECUTION_MODE", "TESTNET_LIVE")
    live_config = load_config()
    live_profile = parse_symbol_profile({"symbol": "ETHUSDT"}, base_config=live_config)

    assert Path(dry_profile.state_path) != Path(live_profile.state_path)
    assert Path(dry_profile.journal_path) != Path(live_profile.journal_path)
    assert Path(dry_profile.lock_path) != Path(live_profile.lock_path)


@pytest.mark.parametrize("legacy_path", ["runtime/state.json", "data/trades.jsonl"])
def test_legacy_shared_file_requires_explicit_migration(isolated_config, legacy_path) -> None:
    isolated_config.setenv("ALPHORA_EXECUTION_MODE", "TESTNET_LIVE")
    path = Path(legacy_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("legacy", encoding="utf-8")

    with pytest.raises(ValueError, match="Legacy shared"):
        load_config()


def test_legacy_basename_is_rejected_with_custom_runtime_dir(isolated_config) -> None:
    isolated_config.setenv("ALPHORA_EXECUTION_MODE", "REAL_LIVE")
    isolated_config.setenv("ALPHORA_RUNTIME_DIR", "private-runtime")
    isolated_config.setenv("ALPHORA_STATE_PATH", "runtime/state.json")

    with pytest.raises(ValueError, match="Legacy shared"):
        load_config()


def test_legacy_basename_check_is_case_insensitive_on_windows(isolated_config) -> None:
    isolated_config.setenv("ALPHORA_EXECUTION_MODE", "TESTNET_LIVE")
    isolated_config.setenv("ALPHORA_STATE_PATH", "runtime/STATE.JSON")

    with pytest.raises(ValueError, match="Legacy shared"):
        load_config()


def test_explicit_paths_remain_available_for_manual_migration(isolated_config) -> None:
    isolated_config.setenv("ALPHORA_EXECUTION_MODE", "TESTNET_LIVE")
    Path("runtime").mkdir()
    Path("runtime/state.json").write_text("legacy", encoding="utf-8")
    Path("data").mkdir()
    Path("data/trades.jsonl").write_text("legacy", encoding="utf-8")
    isolated_config.setenv("ALPHORA_STATE_PATH", "runtime/migrated.json")
    isolated_config.setenv("ALPHORA_TRADE_JOURNAL_PATH", "data/migrated.jsonl")

    config = load_config()

    assert config.state_path == "runtime/migrated.json"
    assert config.trade_journal_path == "data/migrated.jsonl"


@pytest.mark.parametrize(
    "variable,legacy_path",
    [
        ("ALPHORA_STATE_PATH", "runtime/state.json"),
        ("ALPHORA_TRADE_JOURNAL_PATH", "data/trades.jsonl"),
    ],
)
def test_explicit_legacy_shared_path_is_still_refused(
    isolated_config, variable, legacy_path
) -> None:
    isolated_config.setenv("ALPHORA_EXECUTION_MODE", "TESTNET_LIVE")
    isolated_config.setenv(variable, legacy_path)

    with pytest.raises(ValueError, match="Legacy shared"):
        load_config()
