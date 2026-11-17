import json
from copy import deepcopy

from desktop.services.symbol_service import (
    build_symbol_rows,
    duplicate_symbol_profile,
    load_governor_state,
    normalize_portfolio_risk,
    normalize_symbol_profile,
    validate_portfolio_risk,
    validate_symbol_profile,
)
from desktop.settings_store import (
    DEFAULT_PORTFOLIO_RISK_SETTINGS,
    DEFAULT_SYMBOL_PROFILE,
)


def valid_profile(symbol="BTCUSDT"):
    profile = deepcopy(DEFAULT_SYMBOL_PROFILE)
    profile["symbol"] = symbol
    return profile


def test_load_governor_state_and_build_rows(tmp_path):
    state_path = tmp_path / "governor_state.json"
    state_path.write_text(
        json.dumps(
            {
                "symbols": {
                    "BTCUSDT": {
                        "status": "READY",
                        "priority": {"value": "HOT"},
                        "score": "0.91",
                        "last_price": "68000",
                        "position_direction": "LONG",
                        "position_quantity": "0.01",
                        "unrealized_pnl": "12.4",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    snapshot = load_governor_state(state_path, sanitizer=lambda value: value)
    rows = build_symbol_rows(
        [valid_profile()],
        snapshot,
        {"ai_global_model_path": "", "ai_fallback_to_global_model": True},
        model_validator=lambda path: {},
    )

    assert snapshot.available is True
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["status"] == "READY"
    assert rows[0]["priority"] == "HOT"
    assert rows[0]["position"] == "LONG"
    assert rows[0]["tone"] == "success"


def test_missing_and_invalid_governor_state_fail_safely(tmp_path):
    missing = load_governor_state(tmp_path / "missing.json", sanitizer=lambda value: value)
    assert missing.available is False
    assert missing.error == ""

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("not-json", encoding="utf-8")
    invalid = load_governor_state(invalid_path, sanitizer=lambda value: value)
    assert invalid.available is False
    assert invalid.error


def test_symbol_profile_validation_checks_duplicates_and_numbers():
    profile = valid_profile("eth-usdt")
    profile["leverage"] = "200"
    profile["position_notional_usdt"] = "0"

    errors = validate_symbol_profile(
        profile,
        [valid_profile("BTCUSDT")],
        model_validator=lambda path: {},
    )

    assert any("letras e números" in error for error in errors)
    assert any("menor ou igual a 125" in error for error in errors)
    assert any("maior que 0" in error for error in errors)

    duplicate_errors = validate_symbol_profile(
        valid_profile("btcusdt"),
        [valid_profile("BTCUSDT")],
        model_validator=lambda path: {},
    )
    assert any("já existe" in error for error in duplicate_errors)


def test_refined_model_must_match_symbol():
    profile = valid_profile("BTCUSDT")
    profile["ai_refined_model_path"] = "model.json"

    errors = validate_symbol_profile(
        profile,
        [],
        model_validator=lambda path: {
            "model_type": "probabilistic_linear_ai_v1",
            "scope": "SYMBOL_REFINED",
            "symbol": "ETHUSDT",
        },
    )

    assert any("pertence a ETHUSDT" in error for error in errors)


def test_normalization_and_duplication_do_not_mutate_source():
    source = {"symbol": "btcusdt", "enabled": "yes"}
    normalized = normalize_symbol_profile(source)
    duplicated = duplicate_symbol_profile(normalized)

    assert source == {"symbol": "btcusdt", "enabled": "yes"}
    assert normalized["symbol"] == "BTCUSDT"
    assert normalized["enabled"] is True
    assert duplicated["symbol"] == ""
    assert duplicated["enabled"] is False


def test_portfolio_risk_validation():
    valid = normalize_portfolio_risk(DEFAULT_PORTFOLIO_RISK_SETTINGS)
    assert validate_portfolio_risk(valid) == ()

    invalid = dict(valid)
    invalid["max_open_positions"] = "0"
    invalid["max_total_exposure_usdt"] = "-1"
    errors = validate_portfolio_risk(invalid)

    assert any("max_open_positions" in error for error in errors)
    assert any("max_total_exposure_usdt" in error for error in errors)
