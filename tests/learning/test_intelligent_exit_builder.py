from decimal import Decimal

from trading.intelligent_exit_builder import (
    INTELLIGENT_EXIT_STATUS_EXIT_INVALID,
    INTELLIGENT_EXIT_STATUS_NO_TRADE_PLAN,
    INTELLIGENT_EXIT_STATUS_SHADOW_READY,
    PROTECTION_QUALITY_ACCEPTABLE,
    PROTECTION_QUALITY_TOO_TIGHT,
    PROTECTION_QUALITY_WEAK,
    RECOMMENDATION_MODE_ADAPT_REWARD_RISK,
    RECOMMENDATION_MODE_ADAPT_STOP_AND_REWARD,
    RECOMMENDATION_MODE_KEEP_EXISTING,
    IntelligentExitBuilder,
)
from trading.plan import TradePlan


def _trade_plan(
    *,
    action="OPEN_LONG",
    entry=Decimal("100"),
    stop=Decimal("99"),
    take=Decimal("102"),
) -> TradePlan:
    return TradePlan(
        symbol="BTCUSDT",
        action=action,
        status="ALLOW_NORMAL",
        strategy_name="tests.FakeStrategy",
        signal_confidence=95,
        quality_score=80,
        entry_reference_price=entry,
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


def test_intelligent_exit_builder_handles_missing_trade_plan():
    plan = IntelligentExitBuilder().build_shadow_plan(
        trade_plan=None,
        action="OPEN_LONG",
        planned_exit=None,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="SHADOW_ONLY",
        dynamic_exit_enabled=False,
    )

    assert plan.status == INTELLIGENT_EXIT_STATUS_NO_TRADE_PLAN
    assert plan.shadow_only is True
    assert plan.can_use_for_execution is False
    assert plan.protection_complete is False


def test_intelligent_exit_builder_creates_shadow_plan_for_long():
    trade_plan = _trade_plan()

    plan = IntelligentExitBuilder().build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_LONG",
        planned_exit=None,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        dynamic_exit_enabled=True,
    )

    assert plan.status == INTELLIGENT_EXIT_STATUS_SHADOW_READY
    assert plan.plan_id.startswith("iexit_")
    assert plan.trade_plan_id == trade_plan.plan_id
    assert plan.recommended_stop_price == Decimal("99")
    assert plan.recommended_take_profit_price == Decimal("102")
    assert plan.stop_distance_pct == Decimal("0.01")
    assert plan.take_profit_distance_pct == Decimal("0.02")
    assert plan.risk_reward == Decimal("2")
    assert plan.stop_valid is True
    assert plan.take_profit_valid is True
    assert plan.protection_complete is True
    assert plan.shadow_only is True
    assert plan.can_use_for_execution is False
    assert plan.adaptive_candidate_available is True
    assert plan.adaptive_changed is False
    assert plan.adaptive_stop_price == Decimal("99")
    assert plan.adaptive_take_profit_price == Decimal("102")
    assert plan.adaptive_risk_reward == Decimal("2")
    assert plan.recommendation_mode == RECOMMENDATION_MODE_KEEP_EXISTING
    assert plan.protection_quality == PROTECTION_QUALITY_ACCEPTABLE


def test_intelligent_exit_builder_creates_shadow_plan_for_short():
    trade_plan = _trade_plan(
        action="OPEN_SHORT",
        entry=Decimal("100"),
        stop=Decimal("101"),
        take=Decimal("98"),
    )

    plan = IntelligentExitBuilder().build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_SHORT",
        planned_exit=None,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="TESTNET_BLOCK",
        dynamic_exit_enabled=True,
    )

    assert plan.status == INTELLIGENT_EXIT_STATUS_SHADOW_READY
    assert plan.recommended_stop_price == Decimal("101")
    assert plan.recommended_take_profit_price == Decimal("98")
    assert plan.stop_distance_pct == Decimal("0.01")
    assert plan.take_profit_distance_pct == Decimal("0.02")
    assert plan.risk_reward == Decimal("2")
    assert plan.protection_complete is True


def test_intelligent_exit_builder_detects_invalid_exit_direction():
    trade_plan = _trade_plan(
        action="OPEN_LONG",
        entry=Decimal("100"),
        stop=Decimal("101"),
        take=Decimal("99"),
    )

    plan = IntelligentExitBuilder().build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_LONG",
        planned_exit=None,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="PAPER_ACTIVE",
        dynamic_exit_enabled=True,
    )

    assert plan.status == INTELLIGENT_EXIT_STATUS_EXIT_INVALID
    assert plan.stop_valid is False
    assert plan.take_profit_valid is False
    assert plan.protection_complete is False
    assert plan.shadow_only is True
    assert plan.can_use_for_execution is False


def test_intelligent_exit_builder_creates_adaptive_reward_shadow_candidate():
    trade_plan = _trade_plan(
        action="OPEN_LONG",
        entry=Decimal("100"),
        stop=Decimal("99"),
        take=Decimal("101"),
    )

    plan = IntelligentExitBuilder().build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_LONG",
        planned_exit=None,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="SHADOW_ONLY",
        dynamic_exit_enabled=False,
    )

    assert plan.status == INTELLIGENT_EXIT_STATUS_SHADOW_READY

    # Campos antigos continuam iguais para nao alterar execucao nesta fase.
    assert plan.recommended_stop_price == Decimal("99")
    assert plan.recommended_take_profit_price == Decimal("101")
    assert plan.risk_reward == Decimal("1")

    # Novo candidato adaptativo fica separado em shadow.
    assert plan.adaptive_candidate_available is True
    assert plan.adaptive_changed is True
    assert plan.adaptive_stop_price == Decimal("99")
    assert plan.adaptive_take_profit_price == Decimal("102")
    assert plan.adaptive_risk_reward == Decimal("2")
    assert plan.recommendation_mode == RECOMMENDATION_MODE_ADAPT_REWARD_RISK
    assert plan.protection_quality == PROTECTION_QUALITY_WEAK
    assert plan.shadow_only is True
    assert plan.can_use_for_execution is False


def test_intelligent_exit_builder_creates_adaptive_stop_and_reward_candidate():
    trade_plan = _trade_plan(
        action="OPEN_LONG",
        entry=Decimal("100"),
        stop=Decimal("99.9"),
        take=Decimal("100.2"),
    )

    plan = IntelligentExitBuilder().build_shadow_plan(
        trade_plan=trade_plan,
        action="OPEN_LONG",
        planned_exit=None,
        architecture_mode="INTELLIGENT_FLOW",
        intelligent_activation_level="SHADOW_ONLY",
        dynamic_exit_enabled=False,
    )

    assert plan.status == INTELLIGENT_EXIT_STATUS_SHADOW_READY

    # Campos antigos continuam preservados.
    assert plan.recommended_stop_price == Decimal("99.9")
    assert plan.recommended_take_profit_price == Decimal("100.2")

    # Candidato adaptativo afasta stop para 0.5% e recalcula take para RR 2.
    assert plan.adaptive_candidate_available is True
    assert plan.adaptive_changed is True
    assert plan.adaptive_stop_price == Decimal("99.500")
    assert plan.adaptive_take_profit_price == Decimal("101.000")
    assert plan.adaptive_risk_reward == Decimal("2")
    assert plan.recommendation_mode == RECOMMENDATION_MODE_ADAPT_STOP_AND_REWARD
    assert plan.protection_quality == PROTECTION_QUALITY_TOO_TIGHT
    assert plan.shadow_only is True
    assert plan.can_use_for_execution is False
