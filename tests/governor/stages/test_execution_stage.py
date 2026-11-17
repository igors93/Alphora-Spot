"""M3 G8: stage 22 as objects (revalidation, pre-dispatch price check, dispatch steps)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from domain.models import MarketSnapshot, PositionDirection
from governor.cycle_context import CycleContext
from governor.execution_engine import ExecutionResult, ExecutionStatus
from governor.stages.execution import (
    ExecutionPorts,
    ExecutionRevalidation,
    ExecutionStage,
    PreDispatchPriceCheck,
    execution_event_type,
    pre_dispatch_price_drift_limits,
    preserve_opened_at_from_previous_position,
)
from governor.symbol_state_machine import SymbolOperationalState
from tests.governor.stages.fakes import make_evaluation, make_position
from trading.intent import TradeIntent


def _intent(action: str = "OPEN_LONG", price: str = "100") -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT", action=action, reason="signal", market_price=Decimal(price)
    )


def _heavy(price: str) -> SimpleNamespace:
    return SimpleNamespace(
        market=MarketSnapshot(symbol="BTCUSDT", price=Decimal(price), prices=[]),
        mark_price=Decimal(price),
        position=make_position(),
    )


def _ports(journal, transitions, **overrides) -> ExecutionPorts:
    values = {
        "config": SimpleNamespace(
            pre_dispatch_soft_price_drift_bps=10.0,
            pre_dispatch_hard_price_drift_bps=30.0,
            mutations_enabled=True,
        ),
        "journal": journal,
        "trace_payload": lambda: {"cycle_id": "cycle-1", "symbol_eval_id": "eval-1"},
        "logger": SimpleNamespace(warning=lambda *_args: None),
        "record_transition": transitions,
        "portfolio_account_snapshot": lambda **_kwargs: {},
        "portfolio_risk_engine": None,
        "symbol_configs": [],
        "state_reconciliation": lambda _evaluation: None,
        "plan_stages": lambda: (),
        "trade_events": None,
        "record_finalized": lambda **_kwargs: None,
        "pre_execution_guard": None,
        "execution_engine": None,
        "execution_coordinator": None,
        "invalidate_private_state": lambda *_args, **_kwargs: None,
        "count_new_position": lambda: None,
    }
    values.update(overrides)
    return ExecutionPorts(**values)


# --- revalidation ---------------------------------------------------------------------------


def test_new_entry_without_snapshot_session_is_blocked_without_touching_the_board(
    outcomes, journal, transitions, snapshots
):
    revalidation = ExecutionRevalidation(outcomes, _ports(journal, transitions))
    evaluation = make_evaluation(intent=_intent("OPEN_LONG"))

    result = revalidation.revalidate(evaluation)

    assert result is not None and result.terminal
    assert result.stage == "COALESTRA_ENTRY_GUARD"
    assert result.reason == "COALESTRA_SESSION_UNAVAILABLE"
    assert snapshots == {}
    assert transitions.calls[-1]["to_state"] is SymbolOperationalState.BLOCKED


def test_exit_without_snapshot_session_continues_and_is_journaled(outcomes, journal, transitions):
    revalidation = ExecutionRevalidation(outcomes, _ports(journal, transitions))
    evaluation = make_evaluation(intent=_intent("CLOSE_POSITION"))

    assert revalidation.revalidate(evaluation) is None
    assert journal.types() == ["coalestra_exit_degraded_without_session"]
    assert transitions.calls == []


def test_revalidation_failure_blocks_entries_fail_closed(outcomes, journal, transitions):
    class _FailingSession:
        provider = None

        def revalidate_for_execution(self, _symbol, *, exit_only):
            raise RuntimeError("rest timeout")

    context = CycleContext(now=1_000.0, cycle_id="cycle-1")
    context.snapshot_session = _FailingSession()
    revalidation = ExecutionRevalidation(outcomes, _ports(journal, transitions))
    evaluation = make_evaluation(intent=_intent("OPEN_LONG"), cycle_context=context)

    result = revalidation.revalidate(evaluation)

    assert result is not None and result.terminal
    assert result.reason == "COALESTRA_PRE_EXECUTION_REVALIDATION_FAILED"
    assert "coalestra_pre_execution_revalidation_failed" in journal.types()


# --- pre-dispatch price check ---------------------------------------------------------------


def test_price_drift_above_the_hard_limit_blocks_the_entry(
    outcomes, journal, transitions, snapshots
):
    check = PreDispatchPriceCheck(outcomes, _ports(journal, transitions))
    evaluation = make_evaluation(intent=_intent(price="100"), heavy=_heavy("100.50"))

    result = check.check(evaluation)

    assert result is not None and result.terminal
    assert result.reason == "PRE_DISPATCH_PRICE_DRIFT_TOO_HIGH"
    assert evaluation.pre_dispatch_price_decision["blocked"] is True
    assert snapshots["BTCUSDT"].reason == "PRE_DISPATCH_PRICE_DRIFT_TOO_HIGH"


def test_small_price_drift_reprices_the_intent_without_replanning(outcomes, journal, transitions):
    replans = []
    check = PreDispatchPriceCheck(
        outcomes, _ports(journal, transitions, plan_stages=lambda: replans.append(1) or ())
    )
    evaluation = make_evaluation(intent=_intent(price="100"), heavy=_heavy("100.05"))

    assert check.check(evaluation) is None

    assert replans == []
    assert evaluation.intent.market_price == Decimal("100.05")
    assert evaluation.pre_dispatch_price_decision["reason"] == "PRE_DISPATCH_PRICE_ACCEPTED"


def test_drift_between_the_limits_replans_stages_15_to_21(outcomes, journal, transitions):
    calls = []

    def plan_stage(evaluation):
        calls.append(evaluation.trade_plan_id)
        evaluation.trade_plan_id = "plan-2"

    check = PreDispatchPriceCheck(
        outcomes, _ports(journal, transitions, plan_stages=lambda: (plan_stage,))
    )
    evaluation = make_evaluation(
        intent=_intent(price="100"), heavy=_heavy("100.20"), trade_plan_id="plan-1"
    )

    assert check.check(evaluation) is None

    assert calls == ["plan-1"]
    assert journal.types()[-2:] == ["pre_dispatch_replan_started", "pre_dispatch_replan_completed"]
    completed = journal.events[-1][1]
    assert completed["supersedes_trade_plan_id"] == "plan-1"
    assert completed["trade_plan_id"] == "plan-2"


def test_price_check_ignores_exits(outcomes, journal, transitions):
    check = PreDispatchPriceCheck(outcomes, _ports(journal, transitions))
    evaluation = make_evaluation(intent=_intent("CLOSE_POSITION"), heavy=_heavy("120"))

    assert check.check(evaluation) is None
    assert journal.events == []


def test_soft_limit_never_exceeds_the_hard_limit():
    soft, hard = pre_dispatch_price_drift_limits(
        SimpleNamespace(pre_dispatch_soft_price_drift_bps=40, pre_dispatch_hard_price_drift_bps=5)
    )
    assert (soft, hard) == (Decimal("40"), Decimal("40"))


# --- dispatch readiness and result ----------------------------------------------------------


def test_entry_without_execution_readiness_is_blocked_at_dispatch(outcomes, journal, transitions):
    stage = ExecutionStage(outcomes=outcomes, ports=_ports(journal, transitions))
    evaluation = make_evaluation(
        intent=_intent(), cycle_context=CycleContext(now=1_000.0, cycle_id="cycle-1")
    )

    result = stage._dispatch_readiness_block(evaluation, "OPEN_LONG")

    assert result is not None and result.reason == "COALESTRA_ENTRY_NOT_SAFE_AT_DISPATCH"
    assert transitions.calls[-1]["diagnostics"] == {}


def test_exit_without_execution_readiness_is_not_blocked_at_dispatch(
    outcomes, journal, transitions
):
    stage = ExecutionStage(outcomes=outcomes, ports=_ports(journal, transitions))
    evaluation = make_evaluation(intent=_intent("CLOSE_POSITION"))

    assert stage._dispatch_readiness_block(evaluation, "CLOSE_POSITION") is None


def _result(status: ExecutionStatus, *, changed: bool, recovery: str = "NONE") -> ExecutionResult:
    return ExecutionResult(
        command_id="cmd-1",
        status=status,
        symbol="BTCUSDT",
        action="OPEN_LONG",
        order_ids=("1",) if changed else (),
        position_changed=changed,
        reason="test",
        recovery_status=recovery,
    )


@pytest.mark.parametrize(
    ("result", "state", "counted"),
    [
        (_result(ExecutionStatus.CONFIRMED, changed=True), "POSITION_OPEN", 1),
        (_result(ExecutionStatus.BLOCKED, changed=False), "BLOCKED", 0),
        (_result(ExecutionStatus.FAILED, changed=False), "ERROR", 0),
        (
            _result(ExecutionStatus.SKIPPED, changed=False, recovery="ROLLED_BACK"),
            "FLAT_CONFIRMED",
            0,
        ),
    ],
)
def test_result_transition_follows_the_execution_status(
    outcomes, journal, transitions, result, state, counted
):
    new_positions = []
    stage = ExecutionStage(
        outcomes=outcomes,
        ports=_ports(journal, transitions, count_new_position=lambda: new_positions.append(1)),
    )
    command = SimpleNamespace(command_id="cmd-1")

    stage._record_result_transition(make_evaluation(), "OPEN_LONG", command, result)

    assert transitions.calls[-1]["to_state"].value == state
    assert len(new_positions) == counted


def test_rolled_back_entry_has_its_own_journal_event():
    rolled_back = _result(ExecutionStatus.SKIPPED, changed=False, recovery="ROLLED_BACK")
    assert execution_event_type(rolled_back) == "execution_rolled_back"


def test_refreshed_position_keeps_the_local_opened_at():
    previous = SimpleNamespace(
        opened_at=123.0, is_open=True, symbol="BTCUSDT", direction=PositionDirection.LONG
    )
    refreshed = make_position(open_position=True)

    preserved = preserve_opened_at_from_previous_position(refreshed, previous)

    assert preserved.opened_at == 123.0


def test_stage_22_keeps_the_metric_name_of_the_former_governor_method():
    # The golden replay and the stage telemetry key on this name.
    assert ExecutionStage.name == "pre_execution_execution"
