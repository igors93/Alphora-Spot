from dataclasses import replace

import pytest

from operational_module_modes import (
    MODULE_DYNAMIC_EXIT,
    MODULE_SMART_SIZING,
    ModuleModeSource,
    OperationalModuleMode,
    SmartSizingMode,
    load_module_modes_from_env,
    module_runtime_flags_from_modes,
    parse_module_mode,
)


def test_parse_operational_module_mode_normalizes_common_spellings():
    assert parse_module_mode("OFF", OperationalModuleMode, name="mode") is OperationalModuleMode.OFF
    assert parse_module_mode("off", OperationalModuleMode, name="mode") is OperationalModuleMode.OFF
    assert (
        parse_module_mode("Observe", OperationalModuleMode, name="mode")
        is OperationalModuleMode.OBSERVE
    )
    assert (
        parse_module_mode("reduce-only", SmartSizingMode, name="mode")
        is SmartSizingMode.REDUCE_ONLY
    )
    assert (
        parse_module_mode("REDUCE_ONLY", SmartSizingMode, name="mode")
        is SmartSizingMode.REDUCE_ONLY
    )


def test_parse_operational_module_mode_rejects_invalid_value():
    with pytest.raises(ValueError, match="Valores aceitos"):
        parse_module_mode("turbo", OperationalModuleMode, name="ALPHORA_DYNAMIC_EXIT_MODE")


def test_retired_boolean_module_flag_is_rejected(monkeypatch):
    monkeypatch.setenv("ALPHORA_DYNAMIC_EXIT_ENABLED", "true")
    monkeypatch.setenv("ALPHORA_DYNAMIC_EXIT_MODE", "OFF")

    with pytest.raises(ValueError, match="Flags booleanas de modulos operacionais"):
        load_module_modes_from_env(execution_mode="PAPER_LIVE")


def test_retired_user_data_reconciliation_flag_is_rejected(monkeypatch):
    monkeypatch.setenv("ALPHORA_USER_DATA_STREAM_CACHE_RECONCILIATION_ENABLED", "true")

    with pytest.raises(
        ValueError,
        match="ALPHORA_USER_DATA_STREAM_CACHE_RECONCILIATION_ENABLED",
    ):
        load_module_modes_from_env(execution_mode="PAPER_LIVE")


def test_new_env_mode_derives_runtime_flag(monkeypatch):
    monkeypatch.delenv("ALPHORA_DYNAMIC_EXIT_MODE", raising=False)
    monkeypatch.setenv("ALPHORA_DYNAMIC_EXIT_MODE", "ACTIVE")

    resolution = load_module_modes_from_env(execution_mode="PAPER_LIVE")
    flags = module_runtime_flags_from_modes(
        resolution.modes,
        execution_mode="PAPER_LIVE",
    )

    assert resolution.modes[MODULE_DYNAMIC_EXIT] is OperationalModuleMode.ACTIVE
    assert resolution.sources[MODULE_DYNAMIC_EXIT] == ModuleModeSource.ENV.value
    assert flags["dynamic_exit_enabled"] is True


def test_app_config_modes_are_source_of_truth(config_factory):
    config = replace(
        config_factory(),
        dynamic_exit_mode=OperationalModuleMode.ACTIVE,
        smart_sizing_mode=SmartSizingMode.ACTIVE,
    )

    assert config.dynamic_exit_mode is OperationalModuleMode.ACTIVE
    assert config.smart_sizing_mode is SmartSizingMode.ACTIVE
    assert config.dynamic_exit_enabled is True
    assert config.dynamic_sizing_enabled is True

    replaced = replace(config_factory(), dynamic_exit_enabled=True)

    assert replaced.dynamic_exit_mode is OperationalModuleMode.OFF
    assert replaced.dynamic_exit_enabled is False


def test_explicit_mode_is_source_of_truth_over_boolean_field(config_factory):
    config = replace(
        config_factory(dynamic_exit_enabled=True),
        dynamic_exit_mode=OperationalModuleMode.OFF,
        operational_module_mode_sources={
            MODULE_DYNAMIC_EXIT: ModuleModeSource.DESKTOP.value,
        },
    )

    assert config.dynamic_exit_mode is OperationalModuleMode.OFF
    assert config.dynamic_exit_enabled is False


def test_smart_sizing_reduce_only_derives_legacy_enabled(config_factory):
    config = replace(
        config_factory(dynamic_sizing_enabled=False),
        smart_sizing_mode=SmartSizingMode.REDUCE_ONLY,
        operational_module_mode_sources={
            MODULE_SMART_SIZING: ModuleModeSource.DESKTOP.value,
        },
    )

    assert config.smart_sizing_mode is SmartSizingMode.REDUCE_ONLY
    assert config.dynamic_sizing_enabled is True
