from decimal import Decimal

from domain.models import FuturesPosition, PositionDirection
from futures.planning.economic_gate import EconomicGateDecision
from futures.planning.trade_plan_execution_validator import (
    TradePlanExecutionValidation,
)
from futures.protection import ExpectedProtection
from governor.models import GovernorDecision, SymbolRuntimeStatus
from governor.pre_execution_guard import PreExecutionGuard


def _economic_validation(*, allowed: bool = True) -> TradePlanExecutionValidation:
    return TradePlanExecutionValidation(
        decision=EconomicGateDecision(
            allowed=allowed,
            reason=("ECONOMIC_GATE_APPROVED" if allowed else "EXPECTED_NET_GAIN_NOT_POSITIVE"),
            plan_id="tp_test",
            symbol="BTCUSDT",
            action="OPEN_LONG",
            expected_net_gain=Decimal("1"),
            expected_net_gain_bps=Decimal("10"),
            expected_net_loss=Decimal("0.5"),
            reward_risk=Decimal("2"),
            round_trip_cost=Decimal("0.1"),
            round_trip_cost_bps=Decimal("1"),
            minimum_profitable_move=Decimal("0.1"),
            diagnostics={},
        ),
        effective_plan=None,
        original_quantity=Decimal("0.5"),
        final_quantity=Decimal("0.5"),
        original_stop_price=Decimal("99"),
        final_stop_price=Decimal("99"),
        original_take_profit_price=Decimal("102"),
        final_take_profit_price=Decimal("102"),
        diagnostics={},
    )


def _portfolio_decision(*, allowed: bool = True) -> GovernorDecision:
    return GovernorDecision(
        allowed=allowed,
        symbol="BTCUSDT",
        action="OPEN_LONG",
        reason="APPROVED" if allowed else "DENIED",
    )


def _flat_position() -> FuturesPosition:
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def _complete_exit() -> dict[str, Decimal]:
    return {
        "stop_price": Decimal("99"),
        "take_profit_price": Decimal("102"),
    }


def _expected_protection(
    *,
    stop: str = "99",
    take: str = "102",
    working_type: str = "MARK_PRICE",
) -> ExpectedProtection:
    return ExpectedProtection(
        close_side="SELL",
        stop_price=Decimal(stop),
        take_profit_price=Decimal(take),
        working_type=working_type,
    )


def test_open_long_without_planned_exit_is_blocked_when_dynamic_exit_enabled():
    decision = PreExecutionGuard().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        trade_plan_id="tp_test",
        planned_quantity=Decimal("0.5"),
        planned_exit=None,
        final_economic_validation=_economic_validation(),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.READY,
        dynamic_exit_enabled=True,
    )

    assert decision.allowed is False
    assert decision.reason == "PLANNED_EXIT_REQUIRED"
    assert decision.motivo_pt == "A operacao de entrada nao possui plano de saida."
    assert decision.como_corrigir == (
        "Verifique se o TradePlan gerou stop e take antes da execucao."
    )
    assert decision.severity == "WARNING"


def test_open_long_with_denied_final_economic_gate_is_blocked():
    decision = PreExecutionGuard().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        trade_plan_id="tp_test",
        planned_quantity=Decimal("0.5"),
        planned_exit=_complete_exit(),
        final_economic_validation=_economic_validation(allowed=False),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.READY,
        dynamic_exit_enabled=True,
    )

    assert decision.allowed is False
    assert decision.reason == "FINAL_ECONOMIC_GATE_NOT_APPROVED"
    assert decision.motivo_pt == "O plano final nao foi aprovado pelo Economic Gate."
    assert decision.como_corrigir == (
        "Verifique reward/risk liquido, custos, stop, take e diagnostico economico."
    )


def test_open_long_with_coherent_payload_passes():
    decision = PreExecutionGuard().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        trade_plan_id="tp_test",
        planned_quantity=Decimal("0.5"),
        planned_exit=_complete_exit(),
        final_economic_validation=_economic_validation(),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.READY,
        dynamic_exit_enabled=True,
        dynamic_sizing_enabled=True,
        market_price=Decimal("100"),
        mark_price=Decimal("100"),
        protection_working_type="MARK_PRICE",
        expected_protection=_expected_protection(),
    )

    assert decision.allowed is True
    assert decision.reason == "PRE_EXECUTION_GUARD_APPROVED"
    assert decision.motivo_pt == "Payload final aprovado para execucao."
    assert decision.como_corrigir == ""
    assert decision.severity == "INFO"


def test_close_position_passes_without_economic_gate_or_planned_exit():
    decision = PreExecutionGuard().evaluate(
        symbol="BTCUSDT",
        action="CLOSE_POSITION",
        trade_plan_id=None,
        planned_quantity=None,
        planned_exit=None,
        final_economic_validation=None,
        portfolio_decision=None,
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.IN_POSITION,
        dynamic_exit_enabled=True,
        dynamic_sizing_enabled=True,
    )

    assert decision.allowed is True
    assert decision.reason == "CLOSE_POSITION_BYPASSES_PRE_EXECUTION_OPEN_GUARDS"


def test_manual_reconcile_status_blocks_new_entry():
    decision = PreExecutionGuard().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        planned_quantity=Decimal("0.5"),
        planned_exit=_complete_exit(),
        final_economic_validation=_economic_validation(),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.MANUAL_RECONCILE_REQUIRED,
        dynamic_exit_enabled=True,
    )

    assert decision.allowed is False
    assert decision.reason == "MANUAL_RECONCILE_REQUIRED"


def test_protections_pending_blocks_new_entry():
    decision = PreExecutionGuard().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        planned_quantity=Decimal("0.5"),
        planned_exit=_complete_exit(),
        final_economic_validation=_economic_validation(),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.PROTECTIONS_PENDING,
        dynamic_exit_enabled=True,
    )

    assert decision.allowed is False
    assert decision.reason == "PROTECTIONS_PENDING"


def test_learning_payload_change_requires_economic_revalidation():
    guard = PreExecutionGuard()
    decision = guard.evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        trade_plan_id="tp_test",
        planned_quantity=Decimal("0.25"),
        planned_exit=_complete_exit(),
        final_economic_validation=_economic_validation(),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.READY,
        dynamic_exit_enabled=True,
        learning_changed_execution_payload=True,
        learning_payload_revalidated=False,
    )

    assert decision.allowed is False
    assert decision.reason == "LEARNING_PAYLOAD_REVALIDATION_REQUIRED"

    revalidated = guard.evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        trade_plan_id="tp_test",
        planned_quantity=Decimal("0.25"),
        planned_exit=_complete_exit(),
        final_economic_validation=_economic_validation(),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.READY,
        dynamic_exit_enabled=True,
        learning_changed_execution_payload=True,
        learning_payload_revalidated=True,
        market_price=Decimal("100"),
        mark_price=Decimal("100"),
        protection_working_type="MARK_PRICE",
        expected_protection=_expected_protection(),
    )
    assert revalidated.allowed is True


def test_short_is_blocked_when_mark_price_already_breaches_stop():
    decision = PreExecutionGuard().evaluate(
        symbol="BTCUSDT",
        action="OPEN_SHORT",
        trade_plan_id="tp_short",
        planned_quantity=Decimal("0.5"),
        planned_exit={
            "stop_price": Decimal("100.30"),
            "take_profit_price": Decimal("99.60"),
        },
        final_economic_validation=_economic_validation(),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.READY,
        market_price=Decimal("100"),
        mark_price=Decimal("102"),
        protection_working_type="MARK_PRICE",
        expected_protection=_expected_protection(
            stop="100.30",
            take="99.60",
        ),
        max_price_drift_bps=Decimal("10"),
    )

    assert decision.allowed is False
    assert decision.reason == "PROTECTION_TRIGGER_ALREADY_BREACHED_PRE_ENTRY"
    assert decision.diagnostics["pre_entry_protection"]["breached_triggers"] == ["STOP_MARKET"]


def test_mark_contract_drift_blocks_even_when_trigger_is_not_yet_breached():
    decision = PreExecutionGuard().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        trade_plan_id="tp_long",
        planned_quantity=Decimal("0.5"),
        planned_exit=_complete_exit(),
        final_economic_validation=_economic_validation(),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.READY,
        market_price=Decimal("100"),
        mark_price=Decimal("100.20"),
        protection_working_type="MARK_PRICE",
        expected_protection=_expected_protection(),
        max_price_drift_bps=Decimal("10"),
    )

    assert decision.allowed is False
    assert decision.reason == "MARK_CONTRACT_PRICE_DRIFT_TOO_HIGH"
    assert decision.diagnostics["pre_entry_protection"]["price_drift_bps"] == Decimal("20.00")


def test_contract_price_protection_does_not_use_mark_price_as_trigger():
    decision = PreExecutionGuard().evaluate(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        trade_plan_id="tp_long",
        planned_quantity=Decimal("0.5"),
        planned_exit=_complete_exit(),
        final_economic_validation=_economic_validation(),
        portfolio_decision=_portfolio_decision(),
        position=_flat_position(),
        runtime_status=SymbolRuntimeStatus.READY,
        market_price=Decimal("100"),
        mark_price=Decimal("103"),
        protection_working_type="CONTRACT_PRICE",
        expected_protection=_expected_protection(working_type="CONTRACT_PRICE"),
        max_price_drift_bps=Decimal("10"),
    )

    assert decision.allowed is True
    assert decision.reason == "PRE_EXECUTION_GUARD_APPROVED"
