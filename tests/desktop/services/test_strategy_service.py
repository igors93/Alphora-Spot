from dataclasses import dataclass

import pytest

from desktop.services.strategy_service import (
    StrategyDescriptor,
    allowed_activation_levels,
    build_intelligence_diagnostics,
    discover_strategy_descriptors,
    normalize_intelligence_settings,
    parse_strategy_params,
    prepare_strategy_save,
    validate_global_ai_model,
    validate_intelligence_settings,
    validate_strategy_configuration,
)


@dataclass
class Info:
    qualified_name: str
    module_name: str
    class_name: str
    import_error: str = ""
    has_score_symbol: bool = True
    has_intent_symbol: bool = True
    has_decide_symbol: bool = True
    has_from_env: bool = True
    has_from_params: bool = True
    required_history: int = 100
    supports_short: bool = True
    required_timeframes: tuple[str, ...] = ("1m",)
    required_indicators: tuple[str, ...] = ("rsi",)
    docstring: str = "Descrição"


def descriptor() -> StrategyDescriptor:
    return discover_strategy_descriptors(
        lambda: [
            Info(
                qualified_name="strategies.example.ExampleStrategy",
                module_name="strategies.example",
                class_name="ExampleStrategy",
            )
        ]
    )[0]


def test_discovery_normalizes_and_sorts_strategies():
    items = discover_strategy_descriptors(
        lambda: [
            Info("z.Z", "z", "Z"),
            Info("a.A", "a", "A", has_intent_symbol=False, has_decide_symbol=False),
        ]
    )

    assert [item.qualified_name for item in items] == ["a.A", "z.Z"]
    assert items[0].valid is False
    assert items[1].contract_label == "Contrato completo"


def test_parse_strategy_params_requires_json_object():
    assert parse_strategy_params('{"period": 14}') == {"period": 14}

    with pytest.raises(ValueError, match="objeto JSON"):
        parse_strategy_params("[1, 2]")

    with pytest.raises(ValueError, match="JSON inválidos"):
        parse_strategy_params("{")


def test_prepare_strategy_save_can_apply_strategy_params_to_profiles():
    result = prepare_strategy_save(
        current_settings={"strategy_module": "old", "strategy_class": "Old"},
        profiles=[{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}],
        descriptor=descriptor(),
        selection_mode="FIXED",
        params='{"period": 14}',
        apply_to_profiles=True,
    )

    assert result.settings["strategy_module"] == "strategies.example"
    assert result.settings["strategy_params"] == {"period": 14}
    assert result.applied_to_profiles == 2
    assert all(item["strategy_params"] == {"period": 14} for item in result.profiles)
    assert all("strategy_class" not in item for item in result.profiles)


def test_strategy_validation_rejects_incomplete_contract():
    invalid = StrategyDescriptor(
        qualified_name="x.Invalid",
        module_name="x",
        class_name="Invalid",
        valid=False,
        import_error="",
        has_score_symbol=True,
        has_intent_symbol=False,
        has_decide_symbol=False,
        has_from_env=False,
        has_from_params=False,
        required_history="n/a",
        supports_short="n/a",
        required_timeframes=(),
        required_indicators=(),
        docstring="",
    )

    messages = validate_strategy_configuration(invalid, "FIXED", "{}")

    assert messages
    assert messages[0].field == "strategy"


def test_intelligence_validation_checks_probability_range():
    messages = validate_intelligence_settings(
        {
            "execution_mode": "DRY_RUN",
            "intelligent_activation_level": "SHADOW_ONLY",
            "ai_min_long_probability": "1.5",
            "ai_min_short_probability": "0.5",
            "ai_min_direction_gap": "0.2",
            "ai_close_long_min_short_probability": "0.6",
            "ai_close_short_min_long_probability": "0.6",
            "ai_min_expected_value_bps": "3",
        }
    )

    assert any(item.field == "ai_min_long_probability" for item in messages)


def test_normalize_intelligence_settings_preserves_custom_modes():
    prepared = normalize_intelligence_settings(
        {
            "execution_mode": "DRY_RUN",
            "intelligent_activation_level": "SHADOW_ONLY",
            "ai_min_long_probability": "0.6",
            "ai_min_short_probability": "0.6",
            "ai_min_direction_gap": "0.2",
            "ai_close_long_min_short_probability": "0.6",
            "ai_close_short_min_long_probability": "0.6",
            "ai_min_expected_value_bps": "3",
        },
        {
            "operational_module_modes_customized": True,
            "dynamic_exit_mode": "shadow",
        },
    )

    assert prepared["architecture_mode"] == "INTELLIGENT_FLOW"
    assert prepared["dynamic_exit_mode"] == "SHADOW"


def test_global_model_validation_checks_scope_and_approval(tmp_path):
    model = tmp_path / "model.json"
    model.write_text("{}", encoding="utf-8")

    metadata = validate_global_ai_model(
        model,
        require_approved=True,
        validator=lambda _path: {
            "model_type": "probabilistic_linear_ai_v1",
            "scope": "GLOBAL",
            "approved": True,
        },
    )

    assert metadata["scope"] == "GLOBAL"

    with pytest.raises(ValueError, match="aprovado"):
        validate_global_ai_model(
            model,
            require_approved=True,
            validator=lambda _path: {
                "model_type": "probabilistic_linear_ai_v1",
                "scope": "GLOBAL",
            },
        )


def test_diagnostics_fail_closed_when_optional_modules_are_unavailable():
    diagnostics = build_intelligence_diagnostics({}, [])

    assert diagnostics.layers
    assert diagnostics.operation_review
    assert diagnostics.process_review
    assert diagnostics.cache_health


def test_activation_levels_have_safe_dry_run_default():
    assert allowed_activation_levels("DRY_RUN") == ("SHADOW_ONLY",)
