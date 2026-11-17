from decimal import Decimal

from trading.learning_execution_shadow import LearningExecutionShadowDecision
from trading.learning_testnet_observation import (
    TESTNET_OBSERVATION_STATUS_NOT_TESTNET_MODE,
    TESTNET_OBSERVATION_STATUS_OBSERVED_BLOCK_SIGNAL,
    TESTNET_OBSERVATION_STATUS_OBSERVED_REDUCTION_SIGNAL,
    TESTNET_OBSERVATION_STATUS_STORE_DISABLED,
    LearningTestnetObservationPolicy,
    LearningTestnetObservationSettings,
)


def _shadow_decision(*, status="WOULD_REDUCE_SIZE", action="OPEN_LONG"):
    return LearningExecutionShadowDecision(
        symbol="BTCUSDT",
        action=action,
        status=status,
        suggested_action=(
            "BLOCK_EXECUTION_SHADOW" if status == "WOULD_BLOCK" else "REDUCE_SIZE_SHADOW"
        ),
        reason="test",
        plan_id="tp_test",
        original_quantity=Decimal("0.2"),
        shadow_quantity=Decimal("0") if status == "WOULD_BLOCK" else Decimal("0.1"),
        suggested_size_multiplier=(Decimal("0") if status == "WOULD_BLOCK" else Decimal("0.5")),
        would_block_trade=status == "WOULD_BLOCK",
        would_reduce_size=status == "WOULD_REDUCE_SIZE",
    )


def _settings(*, mode="TESTNET_LIVE", enabled=True):
    return LearningTestnetObservationSettings(
        execution_mode=mode,
        learning_store_enabled=True,
        testnet_observation_enabled=enabled,
    )


def test_testnet_observation_stays_disabled_by_default():
    evaluation = LearningTestnetObservationPolicy().evaluate(
        shadow_decision=_shadow_decision(),
        activation_report_payload={"can_apply_reduction_in_paper": True},
    )

    assert evaluation.status == TESTNET_OBSERVATION_STATUS_STORE_DISABLED
    assert evaluation.can_use_for_execution is False
    assert evaluation.can_activate_testnet is False
    assert evaluation.can_activate_real is False


def test_testnet_observation_requires_testnet_live():
    evaluation = LearningTestnetObservationPolicy(
        settings=_settings(mode="PAPER_LIVE"),
    ).evaluate(
        shadow_decision=_shadow_decision(),
        activation_report_payload={"can_apply_reduction_in_paper": True},
    )

    assert evaluation.status == TESTNET_OBSERVATION_STATUS_NOT_TESTNET_MODE
    assert evaluation.can_use_for_execution is False


def test_testnet_observation_records_reduction_signal_without_applying():
    evaluation = LearningTestnetObservationPolicy(settings=_settings()).evaluate(
        shadow_decision=_shadow_decision(status="WOULD_REDUCE_SIZE"),
        activation_report_payload={"can_apply_reduction_in_paper": True},
    )

    assert evaluation.status == TESTNET_OBSERVATION_STATUS_OBSERVED_REDUCTION_SIGNAL
    assert evaluation.would_observe_reduction_signal is True
    assert evaluation.would_observe_block_signal is False
    assert evaluation.original_quantity == Decimal("0.2")
    assert evaluation.shadow_quantity == Decimal("0.1")
    assert evaluation.can_use_for_execution is False
    assert evaluation.observation_only is True


def test_testnet_observation_records_block_signal_without_applying():
    evaluation = LearningTestnetObservationPolicy(settings=_settings()).evaluate(
        shadow_decision=_shadow_decision(status="WOULD_BLOCK"),
        activation_report_payload={"can_apply_block_in_paper": True},
    )

    assert evaluation.status == TESTNET_OBSERVATION_STATUS_OBSERVED_BLOCK_SIGNAL
    assert evaluation.would_observe_block_signal is True
    assert evaluation.would_observe_reduction_signal is False
    assert evaluation.shadow_quantity == Decimal("0")
    assert evaluation.can_use_for_execution is False
