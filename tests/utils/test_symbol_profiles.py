import json
from dataclasses import replace

import pytest

from config import ExecutionMode
from config.symbol_context import (
    load_symbol_profiles,
    make_symbol_order_prefix,
    resolve_governor_symbol_configs,
    symbol_config_from_app_config,
)


def test_load_symbol_profiles_creates_separate_paths(tmp_path, config_factory):
    config = config_factory(execution_mode=ExecutionMode.DRY_RUN)
    path = tmp_path / ".alphora_symbols.local.json"
    path.write_text(
        json.dumps(
            {
                "symbol_profiles": [
                    {"symbol": "aptusdt", "enabled": True},
                    {"symbol": "btcusdt", "enabled": True, "bot_order_prefix": "ALP_BTC"},
                    {"symbol": "ethusdt", "enabled": False},
                ]
            }
        ),
        encoding="utf-8",
    )

    profiles = load_symbol_profiles(path, base_config=config)

    assert [profile.symbol for profile in profiles] == ["APTUSDT", "BTCUSDT", "ETHUSDT"]
    assert profiles[0].state_path.endswith("state_aptusdt_testnet_dry_run.json")
    assert profiles[0].journal_path.endswith("trades_aptusdt_testnet_dry_run.jsonl")
    assert profiles[0].lock_path.endswith("alphora-aptusdt-testnet_dry_run.lock")


def test_symbol_profiles_block_duplicate_symbols(tmp_path, config_factory):
    config = config_factory()
    path = tmp_path / ".alphora_symbols.local.json"
    path.write_text(
        json.dumps(
            {
                "symbol_profiles": [
                    {"symbol": "BTCUSDT", "enabled": True},
                    {"symbol": "btcusdt", "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="simbolo duplicado"):
        load_symbol_profiles(path, base_config=config)


def test_symbol_profiles_block_duplicate_prefix(tmp_path, config_factory):
    config = config_factory()
    path = tmp_path / ".alphora_symbols.local.json"
    path.write_text(
        json.dumps(
            {
                "symbol_profiles": [
                    {"symbol": "BTCUSDT", "bot_order_prefix": "ALP_DUP", "enabled": True},
                    {"symbol": "ETHUSDT", "bot_order_prefix": "ALP_DUP", "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="prefixo de ordem duplicado"):
        load_symbol_profiles(path, base_config=config)


def test_symbol_profiles_migrate_legacy_keys_matching_global(tmp_path, config_factory):
    config = config_factory()
    path = tmp_path / ".alphora_symbols.local.json"
    path.write_text(
        json.dumps(
            {
                "symbol_profiles": [
                    {
                        "symbol": "BTCUSDT",
                        "strategy_module": config.strategy_module,
                        "strategy_class": config.strategy_class,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        profiles = load_symbol_profiles(path, base_config=config)

    assert len(profiles) == 1
    assert profiles[0].symbol == "BTCUSDT"
    assert any("removido" in str(w.message) for w in caught)


def test_symbol_profiles_reject_legacy_keys_conflicting_with_global(tmp_path, config_factory):
    config = config_factory()
    path = tmp_path / ".alphora_symbols.local.json"
    path.write_text(
        json.dumps(
            {
                "symbol_profiles": [
                    {
                        "symbol": "BTCUSDT",
                        "strategy_module": "strategies.futures.other",
                        "strategy_class": "OtherStrategy",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    from config.symbol_profile_schema import SymbolProfileStrategyConflict

    with pytest.raises(SymbolProfileStrategyConflict, match="BTCUSDT"):
        load_symbol_profiles(path, base_config=config)


def test_symbol_config_from_app_config_preserves_single_symbol_compatibility(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)

    assert profile.symbol == config.symbol
    assert profile.state_path == config.state_path
    assert profile.journal_path == config.trade_journal_path


def test_symbol_config_from_app_config_preserves_strategy_params(config_factory):
    config = replace(config_factory(), strategy_params={"min_history": 2})

    profile = symbol_config_from_app_config(config)

    assert profile.strategy_params == {"min_history": 2}


def test_resolve_governor_symbol_configs_falls_back_to_single_profile_when_none_enabled(
    tmp_path,
    config_factory,
):
    config = config_factory()
    path = tmp_path / ".alphora_symbols.local.json"
    path.write_text(
        json.dumps({"symbol_profiles": [{"symbol": "ETHUSDT", "enabled": False}]}),
        encoding="utf-8",
    )
    config = replace(config, symbol_profiles_path=str(path))

    profiles = resolve_governor_symbol_configs(config)

    assert [profile.symbol for profile in profiles] == [config.symbol]


def test_make_symbol_order_prefix_is_short_and_symbol_specific():
    assert make_symbol_order_prefix("APTUSDT") == "ALP_APT"
    assert len(make_symbol_order_prefix("LONGSYMBOLUSDT")) <= 12
