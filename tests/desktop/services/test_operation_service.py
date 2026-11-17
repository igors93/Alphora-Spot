from copy import deepcopy
from dataclasses import dataclass, field, replace

from desktop.services.operation_models import (
    ConfirmationKind,
    MessageSeverity,
    OperationMode,
    OperationPreparationStatus,
)
from desktop.services.operation_service import (
    REAL_LIVE_CHECKLIST,
    OperationDependencies,
    OperationPreparationService,
)


@dataclass
class AIResult:
    can_start: bool = True
    warnings: list[str] = field(default_factory=list)

    def message(self) -> str:
        return "Validação IA concluída."


@dataclass
class ReviewResult:
    can_start: bool = True
    requires_confirmation: bool = False


class ValidStrategy:
    def score_symbol(self):
        return None

    def decide_symbol(self):
        return None


class InvalidContractStrategy:
    pass


def make_dependencies(
    *,
    ai_result=None,
    review_result=None,
    credentials=("key", "secret"),
    strategy=None,
):
    ai_result = ai_result or AIResult()
    review_result = review_result or ReviewResult()
    strategy = strategy or ValidStrategy()

    def build_environment(settings, **kwargs):
        result = {
            "ALPHORA_MULTI_SYMBOL_ENABLED": (
                "true" if settings.get("operation_mode") == "MULTI_SYMBOL" else "false"
            ),
            "ALPHORA_RUNTIME_DIR": "runtime",
        }
        if kwargs.get("symbol_profiles_path"):
            result["ALPHORA_SYMBOL_PROFILES_PATH"] = kwargs["symbol_profiles_path"]
        return result

    return OperationDependencies(
        normalize_environment=lambda settings: str(settings.get("environment", "TESTNET")).upper(),
        validate_ai=lambda settings, profiles: ai_result,
        build_intelligent_review=lambda **kwargs: review_result,
        format_intelligent_review=lambda review: "Revisão operacional concluída.",
        select_credentials=lambda settings: credentials,
        load_strategy=lambda module, class_name, params: strategy,
        build_governor_environment=build_environment,
        build_robot_command=lambda: ["python", "main.py"],
        single_symbol_profiles_path="config/alphora_single_symbol.local.json",
        sanitize=lambda value: value,
        apply_architecture_preset=lambda s: dict(s),
    )


def base_settings(execution_mode="DRY_RUN"):
    return {
        "execution_mode": execution_mode,
        "environment": "TESTNET",
        "strategy_module": "strategies.example",
        "strategy_class": "ExampleStrategy",
        "strategy_params": {},
    }


def profile(symbol="BTCUSDT", *, include_legacy_keys: bool = False):
    p: dict = {
        "enabled": True,
        "symbol": symbol,
        "strategy_params": {},
    }
    if include_legacy_keys:
        p["strategy_module"] = "strategies.example"
        p["strategy_class"] = "ExampleStrategy"
    return p


def test_prepares_single_symbol_plan_without_mutating_inputs():
    settings = base_settings()
    profiles = [profile("btcusdt")]
    original_settings = deepcopy(settings)
    original_profiles = deepcopy(profiles)

    result = OperationPreparationService(make_dependencies()).prepare(settings, profiles)

    assert result.status is OperationPreparationStatus.READY
    assert result.plan is not None
    assert result.plan.mode is OperationMode.GOVERNOR_SINGLE_SYMBOL
    assert result.plan.settings["symbol"] == "BTCUSDT"
    assert (
        result.plan.runtime_profiles[0]["state_path"]
        == "runtime/state_btcusdt_testnet_dry_run.json"
    )
    assert (
        result.plan.runtime_profiles[0]["journal_path"]
        == "data/trades_btcusdt_testnet_dry_run.jsonl"
    )
    assert (
        result.plan.runtime_profiles[0]["lock_path"]
        == "runtime/alphora-btcusdt-testnet_dry_run.lock"
    )
    assert result.plan.symbol_profiles_path == "config/alphora_single_symbol.local.json"
    assert settings == original_settings
    assert profiles == original_profiles


def test_prepares_multi_real_live_with_all_required_confirmations():
    settings = base_settings("REAL_LIVE")
    profiles = [profile("BTCUSDT"), profile("ETHUSDT")]

    result = OperationPreparationService(make_dependencies()).prepare(settings, profiles)

    assert result.status is OperationPreparationStatus.REQUIRES_CONFIRMATION
    assert result.plan is not None
    assert result.plan.mode is OperationMode.GOVERNOR_MULTI_SYMBOL
    requests = {request.key: request for request in result.plan.confirmations}
    assert requests["real_live_checklist"].kind is ConfirmationKind.CHECKLIST
    assert requests["real_live_checklist"].checklist_items == REAL_LIVE_CHECKLIST
    assert requests["real_live_typed_confirmation"].required_tokens == ("REAL_LIVE",)
    assert requests["multi_real_live_typed_confirmation"].required_tokens == ("MULTI_REAL_LIVE",)


def test_blocks_when_no_symbol_is_enabled():
    result = OperationPreparationService(make_dependencies()).prepare(
        base_settings(),
        [{**profile(), "enabled": False}],
    )

    assert result.status is OperationPreparationStatus.BLOCKED
    assert result.plan is None
    assert result.messages[-1].code == "symbols_missing"


def test_blocks_when_ai_validation_fails():
    dependencies = make_dependencies(ai_result=AIResult(can_start=False))

    result = OperationPreparationService(dependencies).prepare(base_settings(), [profile()])

    assert result.status is OperationPreparationStatus.BLOCKED
    assert result.messages[-1].code == "ai_validation_blocked"


def test_blocks_when_credentials_are_missing():
    dependencies = make_dependencies(credentials=("", ""))

    result = OperationPreparationService(dependencies).prepare(base_settings(), [profile()])

    assert result.status is OperationPreparationStatus.BLOCKED
    assert result.messages[-1].code == "credentials_missing"


def test_converts_review_and_strategy_warnings_to_confirmations():
    dependencies = make_dependencies(
        ai_result=AIResult(warnings=["warning"]),
        review_result=ReviewResult(requires_confirmation=True),
        strategy=InvalidContractStrategy(),
    )
    custom_profile = profile()
    custom_profile["strategy_params"] = {"period": 10}

    result = OperationPreparationService(dependencies).prepare(
        base_settings(),
        [custom_profile],
    )

    assert result.status is OperationPreparationStatus.REQUIRES_CONFIRMATION
    assert result.plan is not None
    keys = {request.key for request in result.plan.confirmations}
    assert {
        "ai_validation_warnings",
        "intelligent_operation_warnings",
        "strategy_warnings",
    }.issubset(keys)
    assert any(message.severity is MessageSeverity.WARNING for message in result.messages)


def test_blocks_when_strategy_loader_raises():
    dependencies = make_dependencies()
    dependencies = replace(
        dependencies,
        load_strategy=lambda module, class_name, params: (_ for _ in ()).throw(
            ValueError("estratégia inválida")
        ),
    )

    result = OperationPreparationService(dependencies).prepare(base_settings(), [profile()])

    assert result.status is OperationPreparationStatus.BLOCKED
    assert result.messages[-1].code == "strategy_invalid"


def test_blocks_when_intelligent_review_rejects_configuration():
    dependencies = make_dependencies(review_result=ReviewResult(can_start=False))

    result = OperationPreparationService(dependencies).prepare(base_settings(), [profile()])

    assert result.status is OperationPreparationStatus.BLOCKED
    assert result.messages[-1].code == "intelligent_operation_blocked"


def test_strategy_error_is_sanitized_before_becoming_a_message():
    dependencies = replace(
        make_dependencies(),
        load_strategy=lambda module, class_name, params: (_ for _ in ()).throw(
            ValueError("api_secret=private-value")
        ),
        sanitize=lambda value: str(value).replace("private-value", "***"),
    )

    result = OperationPreparationService(dependencies).prepare(base_settings(), [profile()])

    assert result.status is OperationPreparationStatus.BLOCKED
    assert "private-value" not in result.messages[-1].message
    assert "***" in result.messages[-1].message


def test_unexpected_dependency_error_fails_closed_and_is_sanitized():
    dependencies = replace(
        make_dependencies(),
        normalize_environment=lambda settings: (_ for _ in ()).throw(
            RuntimeError("api_secret=private-value")
        ),
        sanitize=lambda value: str(value).replace("private-value", "***"),
    )

    result = OperationPreparationService(dependencies).prepare(base_settings(), [profile()])

    assert result.status is OperationPreparationStatus.BLOCKED
    assert result.messages[-1].code == "operation_preparation_error"
    assert "private-value" not in result.messages[-1].message
    assert "***" in result.messages[-1].message


def test_single_symbol_passes_multi_symbol_false_to_build_environment():
    received_kwargs: dict = {}

    def capturing_build_environment(settings, **kwargs):
        received_kwargs.update(kwargs)
        return {"ALPHORA_MULTI_SYMBOL_ENABLED": "false", "ALPHORA_RUNTIME_DIR": "runtime"}

    dependencies = replace(
        make_dependencies(),
        build_governor_environment=capturing_build_environment,
    )
    result = OperationPreparationService(dependencies).prepare(base_settings(), [profile()])

    assert result.status is OperationPreparationStatus.READY
    assert received_kwargs.get("multi_symbol") is False


def test_multi_symbol_passes_multi_symbol_true_to_build_environment():
    received_kwargs: dict = {}

    def capturing_build_environment(settings, **kwargs):
        received_kwargs.update(kwargs)
        return {"ALPHORA_MULTI_SYMBOL_ENABLED": "true", "ALPHORA_RUNTIME_DIR": "runtime"}

    dependencies = replace(
        make_dependencies(),
        build_governor_environment=capturing_build_environment,
    )
    result = OperationPreparationService(dependencies).prepare(
        base_settings(), [profile("BTCUSDT"), profile("ETHUSDT")]
    )

    assert result.status is OperationPreparationStatus.READY
    assert received_kwargs.get("multi_symbol") is True


def test_single_symbol_legacy_profile_migrated_and_retired_keys_absent():
    legacy = profile("BTCUSDT", include_legacy_keys=True)
    result = OperationPreparationService(make_dependencies()).prepare(base_settings(), [legacy])

    assert result.status is OperationPreparationStatus.READY
    assert result.plan is not None
    runtime = result.plan.runtime_profiles[0]
    assert "strategy_module" not in runtime
    assert "strategy_class" not in runtime
    assert runtime.get("strategy_params") == {}


def test_multi_symbol_legacy_profiles_migrated():
    legacy_btc = profile("BTCUSDT", include_legacy_keys=True)
    legacy_eth = profile("ETHUSDT", include_legacy_keys=True)
    result = OperationPreparationService(make_dependencies()).prepare(
        base_settings(), [legacy_btc, legacy_eth]
    )

    assert result.status is OperationPreparationStatus.READY
    assert result.plan is not None
    for runtime in result.plan.runtime_profiles:
        assert "strategy_module" not in runtime
        assert "strategy_class" not in runtime


def test_strategy_conflict_in_profile_blocks_preparation():
    conflicting = {
        "enabled": True,
        "symbol": "BTCUSDT",
        "strategy_module": "strategies.futures.other",
        "strategy_class": "OtherStrategy",
        "strategy_params": {},
    }
    result = OperationPreparationService(make_dependencies()).prepare(
        base_settings(), [conflicting]
    )

    assert result.status is OperationPreparationStatus.BLOCKED
    assert any(m.code == "profile_strategy_conflict" for m in result.messages)


def test_inputs_not_mutated_by_legacy_migration():
    legacy = profile("BTCUSDT", include_legacy_keys=True)
    original = deepcopy(legacy)
    OperationPreparationService(make_dependencies()).prepare(base_settings(), [legacy])
    assert legacy == original


def test_strategy_params_preserved_through_migration():
    p = {
        "enabled": True,
        "symbol": "BTCUSDT",
        "strategy_module": "strategies.example",
        "strategy_class": "ExampleStrategy",
        "strategy_params": {"rsi_period": 14},
    }
    result = OperationPreparationService(make_dependencies()).prepare(base_settings(), [p])

    assert result.plan is not None
    runtime = result.plan.runtime_profiles[0]
    assert "strategy_module" not in runtime
    assert "strategy_class" not in runtime
    assert runtime.get("strategy_params") == {"rsi_period": 14}


def test_migration_warning_message_is_emitted():
    legacy = profile("BTCUSDT", include_legacy_keys=True)
    result = OperationPreparationService(make_dependencies()).prepare(base_settings(), [legacy])

    assert result.status is OperationPreparationStatus.READY
    assert any(m.code == "profile_legacy_migration" for m in result.messages)


def test_regression_real_error_scenario():
    """Reproduces the exact ValueError that caused the reported startup failure."""
    settings = {
        "strategy_module": "strategies.futures.fixed.example",
        "strategy_class": "ExampleStrategy",
        "execution_mode": "DRY_RUN",
        "intelligent_activation_level": "",
        "environment": "TESTNET",
        "strategy_params": {},
    }
    p = {
        "enabled": True,
        "symbol": "BTCUSDT",
        "strategy_module": "strategies.futures.fixed.example",
        "strategy_class": "ExampleStrategy",
        "strategy_params": {},
    }

    result = OperationPreparationService(make_dependencies()).prepare(settings, [p])

    assert result.status is OperationPreparationStatus.READY
    assert result.plan is not None
    runtime = result.plan.runtime_profiles[0]
    assert "strategy_module" not in runtime
    assert "strategy_class" not in runtime
    assert len(result.plan.runtime_profiles) == 1
