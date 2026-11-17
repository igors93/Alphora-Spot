"""G5: the trade plan / economic gate (15) and intelligent layers (16) stages."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from governor.stages.planning import (
    IntelligentLayersPorts,
    IntelligentLayersStage,
    TradePlanPorts,
    TradePlanStage,
)
from governor.symbol_state_machine import SymbolOperationalState
from tests.governor.stages.fakes import make_evaluation, make_score
from trading.intent import TradeIntent


def _intent() -> TradeIntent:
    return TradeIntent(symbol="BTCUSDT", action="OPEN_LONG", reason="signal")


def _trade_plan_ports(journal, transitions, *, allowed, strategy_exit=None, warnings=None):
    plan = SimpleNamespace(plan=SimpleNamespace(plan_id="plan-1"))
    gate = SimpleNamespace(allowed=allowed, reason="ECONOMIC_OK" if allowed else "NET_GAIN_LOW")
    return TradePlanPorts(
        portfolio_account_snapshot=lambda *, context: {"equity": "1000"},
        record_shadow_trade_plan=lambda **_kwargs: plan,
        evaluate_economic_gate=lambda **_kwargs: gate,
        economic_gate_event_payload=lambda decision: {"reason": decision.reason},
        planned_quantity_for_execution=lambda result, *, action: (Decimal("0.5"), "PLAN"),
        record_learning_execution_shadow=lambda **_kwargs: None,
        planned_exit_for_execution=lambda result, *, action: ({"stop_price": "90"}, "PLAN_EXIT"),
        strategy_planned_exit_for_execution=lambda **_kwargs: (strategy_exit, "STRATEGY_EXIT"),
        record_planned_exit_checkpoint=lambda **kwargs: journal.append(kwargs["event_type"]),
        dynamic_exit_enabled=lambda: strategy_exit is not None,
        journal=journal,
        trace_payload=lambda: {},
        log_warning=lambda *args: (warnings if warnings is not None else []).append(args),
        record_transition=transitions,
    )


def test_trade_plan_stops_at_the_initial_economic_gate(outcomes, journal, transitions, snapshots):
    warnings: list = []
    stage = TradePlanStage(
        outcomes=outcomes,
        ports=_trade_plan_ports(journal, transitions, allowed=False, warnings=warnings),
    )
    evaluation = make_evaluation(intent=_intent(), score=make_score())

    result = stage(evaluation)

    assert stage.name == "planning_economic_gate_initial"
    assert result is not None and result.stage == "ECONOMIC_GATE"
    assert result.classification == "economico" and result.reason == "NET_GAIN_LOW"
    assert evaluation.trade_plan_id == "plan-1"
    assert journal.types()[0] == "trade_blocked_by_economic_gate"
    assert warnings and snapshots["BTCUSDT"].reason == "NET_GAIN_LOW"
    assert [call["to_state"] for call in transitions.calls] == [
        SymbolOperationalState.TRADE_PLANNED,
        SymbolOperationalState.BLOCKED,
    ]
    assert evaluation.planned_quantity is None


def test_trade_plan_plans_quantity_and_prefers_the_strategy_exit(outcomes, journal, transitions):
    strategy_exit = {"stop_price": "95", "take_profit_price": "120"}
    stage = TradePlanStage(
        outcomes=outcomes,
        ports=_trade_plan_ports(journal, transitions, allowed=True, strategy_exit=strategy_exit),
    )
    evaluation = make_evaluation(intent=_intent(), score=make_score())

    assert stage(evaluation) is None

    assert evaluation.planned_quantity == Decimal("0.5")
    assert evaluation.planned_exit == strategy_exit
    assert evaluation.planned_exit_reason == "STRATEGY_EXIT"
    assert journal.types() == ["planned_exit_before_intelligent_layers"]
    assert transitions.calls[-1]["stage"] == "PLANNED_EXIT"


def _activation(**overrides):
    values = {
        "applied": True,
        "quantity_applied": True,
        "exit_applied": True,
        "final_quantity": Decimal("0.25"),
        "final_stop_price": Decimal("97"),
        "final_take_profit_price": Decimal("130"),
        "reason": "INTELLIGENT_ORDER_APPLIED",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_intelligent_layers_apply_quantity_and_economic_exit(journal, transitions):
    applied_exits: list[dict] = []

    def apply_exit(**kwargs):
        applied_exits.append(kwargs)
        return kwargs["candidate_exit"], kwargs["candidate_reason"]

    stage = IntelligentLayersStage(
        IntelligentLayersPorts(
            record_order_shadow=lambda **_kwargs: "order-plan",
            record_order_comparison=lambda **_kwargs: None,
            record_exit_shadow=lambda **_kwargs: "exit-plan",
            record_exit_comparison=lambda **_kwargs: None,
            evaluate_order_activation=lambda **_kwargs: _activation(),
            record_order_activation_result=lambda **_kwargs: None,
            evaluate_exit_activation=lambda **_kwargs: None,
            record_exit_activation_result=lambda **_kwargs: None,
            apply_exit_candidate_if_economic=apply_exit,
            record_planned_exit_checkpoint=lambda **kwargs: journal.append(kwargs["event_type"]),
            record_transition=transitions,
        )
    )
    evaluation = make_evaluation(
        intent=_intent(),
        planned_quantity=Decimal("0.5"),
        planned_exit={"stop_price": "90", "take_profit_price": "120"},
    )

    assert stage(evaluation) is None

    assert stage.name == "apply_intelligent_layers"
    assert evaluation.planned_quantity == Decimal("0.25")
    assert evaluation.planned_quantity_reason == "INTELLIGENT_ORDER_APPLIED"
    assert applied_exits[0]["candidate_source"] == "INTELLIGENT_ORDER"
    assert evaluation.planned_exit is not None
    assert Decimal(str(evaluation.planned_exit["stop_price"])) == Decimal("97")
    assert journal.types() == ["planned_exit_after_intelligent_layers"]
    assert transitions.calls[-1]["to_state"] is SymbolOperationalState.INTELLIGENT_LAYERS_APPLIED
