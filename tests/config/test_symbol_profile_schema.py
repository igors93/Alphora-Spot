from copy import deepcopy

import pytest

from config.symbol_profile_schema import (
    SymbolProfileStrategyConflict,
    canonicalize_profiles_for_persistence,
    canonicalize_symbol_profile_payload,
)

_MODULE = "strategies.futures.fixed.example"
_CLASS = "ExampleStrategy"


def test_profile_without_legacy_keys_is_unchanged():
    profile = {"symbol": "BTCUSDT", "leverage": "5", "strategy_params": {"k": 1}}
    result = canonicalize_symbol_profile_payload(
        profile, global_strategy_module=_MODULE, global_strategy_class=_CLASS
    )
    assert result.payload == profile
    assert result.removed_keys == ()
    assert result.warnings == ()


def test_profile_with_empty_strategy_keys_removes_them():
    profile = {"symbol": "BTCUSDT", "strategy_module": "", "strategy_class": ""}
    result = canonicalize_symbol_profile_payload(
        profile, global_strategy_module=_MODULE, global_strategy_class=_CLASS
    )
    assert "strategy_module" not in result.payload
    assert "strategy_class" not in result.payload
    assert set(result.removed_keys) == {"strategy_module", "strategy_class"}
    assert result.warnings == ()


def test_legacy_matching_global_is_migrated_with_warning():
    profile = {
        "symbol": "BTCUSDT",
        "strategy_module": _MODULE,
        "strategy_class": _CLASS,
    }
    result = canonicalize_symbol_profile_payload(
        profile, global_strategy_module=_MODULE, global_strategy_class=_CLASS
    )
    assert "strategy_module" not in result.payload
    assert "strategy_class" not in result.payload
    assert set(result.removed_keys) == {"strategy_module", "strategy_class"}
    assert len(result.warnings) == 2


def test_legacy_different_from_global_raises_conflict():
    profile = {
        "symbol": "BTCUSDT",
        "strategy_module": "strategies.futures.other",
        "strategy_class": "OtherStrategy",
    }
    with pytest.raises(SymbolProfileStrategyConflict, match="BTCUSDT"):
        canonicalize_symbol_profile_payload(
            profile,
            global_strategy_module=_MODULE,
            global_strategy_class=_CLASS,
        )


def test_only_strategy_module_present_is_handled():
    profile = {"symbol": "BTCUSDT", "strategy_module": _MODULE}
    result = canonicalize_symbol_profile_payload(
        profile, global_strategy_module=_MODULE, global_strategy_class=_CLASS
    )
    assert "strategy_module" not in result.payload
    assert result.removed_keys == ("strategy_module",)


def test_only_strategy_class_present_is_handled():
    profile = {"symbol": "BTCUSDT", "strategy_class": _CLASS}
    result = canonicalize_symbol_profile_payload(
        profile, global_strategy_module=_MODULE, global_strategy_class=_CLASS
    )
    assert "strategy_class" not in result.payload
    assert result.removed_keys == ("strategy_class",)


def test_strategy_params_is_preserved():
    profile = {
        "symbol": "BTCUSDT",
        "strategy_module": _MODULE,
        "strategy_class": _CLASS,
        "strategy_params": {"rsi_period": 14},
    }
    result = canonicalize_symbol_profile_payload(
        profile, global_strategy_module=_MODULE, global_strategy_class=_CLASS
    )
    assert result.payload["strategy_params"] == {"rsi_period": 14}


def test_input_is_not_mutated():
    profile = {
        "symbol": "BTCUSDT",
        "strategy_module": _MODULE,
        "strategy_class": _CLASS,
    }
    original = deepcopy(profile)
    canonicalize_symbol_profile_payload(
        profile, global_strategy_module=_MODULE, global_strategy_class=_CLASS
    )
    assert profile == original


def test_no_global_strategy_with_non_empty_key_raises_conflict():
    profile = {"symbol": "BTCUSDT", "strategy_module": _MODULE}
    with pytest.raises(SymbolProfileStrategyConflict):
        canonicalize_symbol_profile_payload(
            profile,
            global_strategy_module=None,
            global_strategy_class=None,
            reject_strategy_conflicts=True,
        )


def test_no_global_strategy_with_non_empty_key_removes_when_not_rejecting():
    profile = {"symbol": "BTCUSDT", "strategy_module": _MODULE}
    result = canonicalize_symbol_profile_payload(
        profile,
        global_strategy_module=None,
        global_strategy_class=None,
        reject_strategy_conflicts=False,
    )
    assert "strategy_module" not in result.payload
    assert result.removed_keys == ("strategy_module",)
    assert len(result.warnings) == 1


def test_canonicalize_profiles_for_persistence_strips_keys():
    profiles = [
        {"symbol": "BTCUSDT", "strategy_module": _MODULE, "strategy_class": _CLASS, "leverage": 5},
        {"symbol": "ETHUSDT", "leverage": 3},
    ]
    result = canonicalize_profiles_for_persistence(profiles)
    assert "strategy_module" not in result[0]
    assert "strategy_class" not in result[0]
    assert result[0]["leverage"] == 5
    assert result[1] == {"symbol": "ETHUSDT", "leverage": 3}


def test_canonicalize_profiles_for_persistence_preserves_strategy_params():
    profiles = [
        {
            "symbol": "BTCUSDT",
            "strategy_module": _MODULE,
            "strategy_params": {"rsi_period": 14},
        }
    ]
    result = canonicalize_profiles_for_persistence(profiles)
    assert result[0]["strategy_params"] == {"rsi_period": 14}
