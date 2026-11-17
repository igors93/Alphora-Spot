"""G5: smart sizing (17), smart execution (20) and the execution planner (21)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from governor.stages.execution_planning import ExecutionPlannerStage, SmartExecutionStage
from governor.stages.sizing import SmartSizingStage
from governor.symbol_state_machine import SymbolOperationalState
from operational_module_modes import (
    MODULE_SMART_EXECUTION,
    MODULE_SMART_SIZING,
    OperationalModuleMode,
    SmartSizingMode,
)
from tests.governor.stages.fakes import make_evaluation, make_score
from trading.intelligent_execution_planner import IntelligentExecutionPlan
from trading.intent import TradeIntent


def _intent(action: str = "OPEN_LONG") -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT", action=action, reason="signal", market_price=Decimal("100")
    )


class _Rules:
    def minimum_required_notional(self, price):
        return Decimal("5")

    def round_quantity(self, quantity):
        return quantity.quantize(Decimal("0.001"))

    def validate_quantity(self, *, quantity, price):
        if quantity <= 0:
            raise ValueError("quantity")


def _sizing_decision(*, blocked=False, affected=True, final="50", mode=SmartSizingMode.ACTIVE):
    return SimpleNamespace(
        blocked=blocked,
        affected_result=affected,
        mode=mode,
        base_notional_usdt=Decimal("100"),
        final_notional_usdt=Decimal(final),
        reason="SIZED",
        to_dict=lambda: {"reason": "SIZED"},
    )


def _sizing_stage(outcomes, journal, transitions, decision, mode=SmartSizingMode.ACTIVE):
    inputs: list = []
    engine = SimpleNamespace(evaluate=lambda value: inputs.append(value) or decision)
    stage = SmartSizingStage(
        outcomes=outcomes,
        module_modes={MODULE_SMART_SIZING: mode},
        engine=lambda: engine,
        journal=journal,
        trace_payload=lambda: {},
        record_transition=transitions,
    )
    return stage, inputs


def _sizing_evaluation():
    evaluation = make_evaluation(
        intent=_intent(), score=make_score(), heavy=SimpleNamespace(exchange_rules=_Rules())
    )
    evaluation.evaluation_input.symbol_config.position_notional_usdt = Decimal("100")
    evaluation.evaluation_input.symbol_config.max_exposure_usdt = Decimal("500")
    evaluation.evaluation_input.symbol_config.max_daily_loss_usdt = Decimal("50")
    return evaluation


def test_smart_sizing_applies_the_sized_quantity(outcomes, journal, transitions):
    stage, inputs = _sizing_stage(outcomes, journal, transitions, _sizing_decision())
    evaluation = _sizing_evaluation()

    assert stage(evaluation) is None

    assert evaluation.planned_quantity == Decimal("0.500")
    assert evaluation.planned_quantity_reason == "SIZED"
    assert inputs[0].base_notional_usdt == Decimal("100")
    assert journal.types() == ["smart_sizing_applied"]


def test_smart_sizing_blocks_and_skips_closes(outcomes, journal, transitions):
    stage, _ = _sizing_stage(outcomes, journal, transitions, _sizing_decision(blocked=True))
    blocked = stage(_sizing_evaluation())
    close = make_evaluation(intent=_intent("CLOSE_POSITION"))

    assert blocked is not None and blocked.reason == "SMART_SIZING_BLOCKED"
    assert transitions.calls[-1]["to_state"] is SymbolOperationalState.BLOCKED
    assert stage(close) is None


def _execution_stage(outcomes, journal, order_type, *, blocked=False):
    decision = SimpleNamespace(
        entry_order_type=order_type,
        blocked=blocked,
        affected_result=False,
        mode=OperationalModuleMode.ACTIVE,
        reason="MARKET_OK",
        to_dict=lambda: {},
    )
    engine = SimpleNamespace(evaluate=lambda _value: decision)
    return SmartExecutionStage(
        outcomes=outcomes,
        module_modes={MODULE_SMART_EXECUTION: OperationalModuleMode.ACTIVE},
        engine=lambda: engine,
        config=SimpleNamespace(),
        journal=journal,
        trace_payload=lambda: {},
    )


def test_smart_execution_blocks_live_limit_entries(outcomes, journal, snapshots):
    evaluation = make_evaluation(intent=_intent(), heavy=None)

    result = _execution_stage(outcomes, journal, "LIMIT")(evaluation)

    assert result is not None and result.reason == "SMART_EXECUTION_LIMIT_NOT_IMPLEMENTED"
    assert journal.types()[0] == "smart_execution_blocked"
    assert snapshots["BTCUSDT"].reason == "SMART_EXECUTION_LIMIT_NOT_IMPLEMENTED"


def test_smart_execution_accepts_market_entries(outcomes, journal):
    evaluation = make_evaluation(intent=_intent(), heavy=None)

    assert _execution_stage(outcomes, journal, "MARKET")(evaluation) is None
    assert evaluation.smart_execution_decision is not None
    assert journal.types() == ["smart_execution_evaluated"]


def _plan(should_execute: bool) -> IntelligentExecutionPlan:
    return IntelligentExecutionPlan(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        status="READY" if should_execute else "WAIT",
        planner_action="EXECUTE" if should_execute else "WAIT",
        reason="PRICE_DRIFT_TOO_HIGH" if not should_execute else "OK",
        execution_style="MARKET",
        should_execute=should_execute,
        execution_will_run=should_execute,
    )


def _planner(outcomes, journal, transitions, plan, finalized):
    return ExecutionPlannerStage(
        outcomes=outcomes,
        evaluate_plan=lambda **_kwargs: plan,
        record_finalized=lambda **kwargs: finalized.append(kwargs),
        journal=journal,
        record_transition=transitions,
    )


def test_execution_planner_makes_the_entry_wait(outcomes, journal, transitions):
    finalized: list[dict] = []
    evaluation = make_evaluation(intent=_intent(), score=make_score())

    result = _planner(outcomes, journal, transitions, _plan(False), finalized)(evaluation)

    assert result is not None and result.stage == "EXECUTION_PLANNER"
    assert result.classification == "degradacao"
    assert finalized[0]["execution_will_run"] is False
    assert journal.types()[0] == "trade_waiting_by_execution_planner"
    assert transitions.calls[-1]["to_state"] is SymbolOperationalState.EXECUTION_WAITING


def test_execution_planner_lets_ready_plans_through(outcomes, journal, transitions):
    finalized: list[dict] = []
    evaluation = make_evaluation(intent=_intent(), score=make_score())

    assert _planner(outcomes, journal, transitions, _plan(True), finalized)(evaluation) is None
    assert evaluation.intelligent_execution_plan is not None
    assert finalized == [] and journal.events == []
