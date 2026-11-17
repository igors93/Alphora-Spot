from decimal import Decimal

from trading.intelligent_exit_activation import (
    INTELLIGENT_EXIT_ACTIVATION_STATUS_APPLIED,
    INTELLIGENT_EXIT_ACTIVATION_STATUS_DYNAMIC_EXIT_DISABLED,
    INTELLIGENT_EXIT_ACTIVATION_STATUS_NOT_PAPER_ACTIVE,
    INTELLIGENT_EXIT_SOURCE_ADAPTIVE,
    INTELLIGENT_EXIT_SOURCE_RECOMMENDED,
    IntelligentExitActivationPolicy,
)
from trading.intelligent_exit_builder import (
    PROTECTION_QUALITY_WEAK,
    RECOMMENDATION_MODE_ADAPT_REWARD_RISK,
    IntelligentExitBuilder,
)
from trading.plan import TradePlan


def _trade_plan(
    *,
    stop: Decimal = Decimal("99"),
    take: Decimal = Decimal("102"),
) -> TradePlan:
    return TradePlan(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        status="ALLOW_NORMAL",
        strategy_name="tests.FakeStrategy",
        signal_confidence=95,
        quality_score=80,
        entry_reference_price=Decimal("100"),
        quantity=Decimal("0.5"),
        notional=Decimal("50"),
        stop_price=stop,
        take_profit_price=take,
        estimated_entry_fee=Decimal("0.02"),
        estimated_exit_fee=Decimal("0.02"),
        estimated_spread_cost=Decimal("0.01"),
        estimated_slippage_cost=Decimal("0.01"),
        estimated_funding_cost=Decimal("0"),
        estimated_round_trip_cost=Decimal("0.06"),
        expected_net_gain=Decimal("0.94"),
        expected_net_loss=Decimal("1.06"),
        reward_risk=Decimal("2"),
        size_multiplier=Decimal("1"),
        execution_style="MARKET",
        management_style="FIXED_PROTECTION",
        reason="test",
        block_reason=None,
    )


def _exit_plan(
    *,
    stop: Decimal = Decimal("99"),
    take: Decimal = Decimal("102"),
):
    trade_plan = _trade_plan(stop=stop, take=take)
    return IntelligentExitBuilder().build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_LONG",
        planned_exit=None,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        dynamic_exit_enabled=True,
    )


def test_intelligent_exit_activation_applies_only_in_paper_active():
    decision = IntelligentExitActivationPolicy().evaluate(
        intelligent_exit_plan=_exit_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        dynamic_exit_enabled=True,
    )

    assert decision.status == INTELLIGENT_EXIT_ACTIVATION_STATUS_APPLIED
    assert decision.applied is True
    assert decision.exit_applied is True
    assert decision.can_use_for_execution is True
    assert decision.paper_only is True
    assert decision.testnet_only is False
    assert decision.can_activate_testnet is False
    assert decision.can_activate_real is False
    assert decision.final_stop_price == Decimal("99")
    assert decision.final_take_profit_price == Decimal("102")
    assert decision.exit_source == INTELLIGENT_EXIT_SOURCE_ADAPTIVE
    assert decision.used_adaptive_exit is True
    assert decision.adaptive_exit_applied is True


def test_intelligent_exit_activation_uses_adaptive_candidate_in_paper_active():
    decision = IntelligentExitActivationPolicy().evaluate(
        intelligent_exit_plan=_exit_plan(stop=Decimal("99"), take=Decimal("101")),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        dynamic_exit_enabled=True,
    )

    assert decision.status == INTELLIGENT_EXIT_ACTIVATION_STATUS_APPLIED
    assert decision.applied is True
    assert decision.exit_applied is True
    assert decision.final_stop_price == Decimal("99")
    assert decision.final_take_profit_price == Decimal("102")
    assert decision.exit_source == INTELLIGENT_EXIT_SOURCE_ADAPTIVE
    assert decision.used_adaptive_exit is True
    assert decision.adaptive_exit_applied is True
    assert decision.recommendation_mode == RECOMMENDATION_MODE_ADAPT_REWARD_RISK
    assert decision.protection_quality == PROTECTION_QUALITY_WEAK


def test_intelligent_exit_activation_uses_adaptive_candidate_in_testnet_block():
    decision = IntelligentExitActivationPolicy().evaluate(
        intelligent_exit_plan=_exit_plan(stop=Decimal("99"), take=Decimal("101")),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="TESTNET_BLOCK",
        execution_mode="TESTNET_LIVE",
        dynamic_exit_enabled=True,
    )

    assert decision.status == INTELLIGENT_EXIT_ACTIVATION_STATUS_APPLIED
    assert decision.applied is True
    assert decision.exit_applied is True
    assert decision.can_use_for_execution is True
    assert decision.paper_only is False
    assert decision.testnet_only is True
    assert decision.can_activate_testnet is True
    assert decision.can_activate_real is False
    assert decision.final_stop_price == Decimal("99")
    assert decision.final_take_profit_price == Decimal("102")
    assert decision.exit_source == INTELLIGENT_EXIT_SOURCE_ADAPTIVE
    assert decision.used_adaptive_exit is True
    assert decision.adaptive_exit_applied is True


def test_intelligent_exit_activation_falls_back_to_recommended_when_no_adaptive_candidate():
    exit_plan = _exit_plan()
    exit_plan = type(exit_plan)(
        **{
            **exit_plan.__dict__,
            "adaptive_candidate_available": False,
            "adaptive_stop_price": None,
            "adaptive_take_profit_price": None,
        }
    )

    decision = IntelligentExitActivationPolicy().evaluate(
        intelligent_exit_plan=exit_plan,
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        dynamic_exit_enabled=True,
    )

    assert decision.status == INTELLIGENT_EXIT_ACTIVATION_STATUS_APPLIED
    assert decision.final_stop_price == Decimal("99")
    assert decision.final_take_profit_price == Decimal("102")
    assert decision.exit_source == INTELLIGENT_EXIT_SOURCE_RECOMMENDED
    assert decision.used_adaptive_exit is False
    assert decision.adaptive_exit_applied is False


def test_intelligent_exit_activation_does_not_apply_when_dynamic_exit_disabled():
    decision = IntelligentExitActivationPolicy().evaluate(
        intelligent_exit_plan=_exit_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        execution_mode="PAPER_LIVE",
        dynamic_exit_enabled=False,
    )

    assert decision.status == INTELLIGENT_EXIT_ACTIVATION_STATUS_DYNAMIC_EXIT_DISABLED
    assert decision.applied is False
    assert decision.exit_applied is False
    assert decision.can_use_for_execution is False
    assert decision.final_stop_price is None
    assert decision.final_take_profit_price is None


def test_intelligent_exit_activation_applies_in_testnet_block():
    decision = IntelligentExitActivationPolicy().evaluate(
        intelligent_exit_plan=_exit_plan(),
        action="OPEN_LONG",
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="TESTNET_BLOCK",
        execution_mode="TESTNET_LIVE",
        dynamic_exit_enabled=True,
    )

    assert decision.status == INTELLIGENT_EXIT_ACTIVATION_STATUS_APPLIED
    assert decision.applied is True
    assert decision.exit_applied is True
    assert decision.can_use_for_execution is True
    assert decision.paper_only is False
    assert decision.testnet_only is True
    assert decision.can_activate_testnet is True
    assert decision.can_activate_real is False
    assert decision.final_stop_price == Decimal("99")
    assert decision.final_take_profit_price == Decimal("102")
    assert decision.exit_source == INTELLIGENT_EXIT_SOURCE_ADAPTIVE


def test_intelligent_exit_activation_does_not_apply_in_testnet_reduce_or_real():
    for level, execution_mode in (
        ("TESTNET_REDUCE", "TESTNET_LIVE"),
        ("REAL_BLOCK", "REAL_LIVE"),
    ):
        decision = IntelligentExitActivationPolicy().evaluate(
            intelligent_exit_plan=_exit_plan(),
            action="OPEN_LONG",
            architecture_mode="INTELLIGENT_FLOW",
            intelligent_activation_level=level,
            execution_mode=execution_mode,
            dynamic_exit_enabled=True,
        )

        assert decision.status == INTELLIGENT_EXIT_ACTIVATION_STATUS_NOT_PAPER_ACTIVE
        assert decision.applied is False
        assert decision.exit_applied is False
        assert decision.can_use_for_execution is False
        assert decision.can_activate_testnet is False
        assert decision.can_activate_real is False
