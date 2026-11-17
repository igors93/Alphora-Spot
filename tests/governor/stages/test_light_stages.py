"""G4: light-phase stages (2, 3, 7, 8) built from ports only."""

from __future__ import annotations

from types import SimpleNamespace

from governor.models import SymbolRuntimeStatus
from governor.request_budget import RequestBudgetDecision
from governor.stages.light import (
    LightOnlyStage,
    ManualControlStage,
    RequestBudgetStage,
    WarmupStage,
)
from governor.symbol_state_machine import SymbolOperationalState
from tests.governor.stages.fakes import make_evaluation, make_position, make_score


def test_manual_control_stops_on_manual_reconcile(outcomes, transitions, journal):
    stage = ManualControlStage(outcomes=outcomes, record_transition=transitions)
    evaluation = make_evaluation(status=SymbolRuntimeStatus.MANUAL_RECONCILE_REQUIRED)

    result = stage(evaluation)

    assert stage.name == "handle_manual_or_protection_status"
    assert result is not None and result.stage == "MANUAL_RECONCILE"
    assert result.classification == "absoluto"
    assert transitions.calls[0]["to_state"] is SymbolOperationalState.MANUAL_RECONCILE_REQUIRED
    assert journal.types() == ["governor_evaluate_symbol_flow_decision"]


def test_manual_control_stops_on_pending_protection_and_passes_otherwise(outcomes, transitions):
    stage = ManualControlStage(outcomes=outcomes, record_transition=transitions)

    pending = stage(make_evaluation(status=SymbolRuntimeStatus.PROTECTIONS_PENDING))
    ready = stage(make_evaluation(status=SymbolRuntimeStatus.READY))

    assert pending is not None and pending.stage == "PROTECTION_STATUS"
    assert pending.reason == "PROTECTIONS_PENDING"
    assert ready is None


def test_warmup_stops_flat_symbols_only(outcomes):
    stage = WarmupStage(outcomes=outcomes)
    score = make_score(reason="WARMUP_IN_PROGRESS")

    flat = stage(make_evaluation(warmup=True, position=make_position(), score=score))
    in_position = stage(
        make_evaluation(warmup=True, position=make_position(open_position=True), score=score)
    )
    ready = stage(make_evaluation(warmup=False, position=make_position(), score=score))

    assert flat is not None and flat.stage == "WARMUP" and flat.reason == "WARMUP_IN_PROGRESS"
    assert in_position is None and ready is None


def _light_only(outcomes, journal, transitions, *, needs_heavy: bool) -> LightOnlyStage:
    return LightOnlyStage(
        outcomes=outcomes,
        needs_heavy_check=lambda _score, _position: needs_heavy,
        journal=journal,
        trace_payload=lambda: {"cycle_id": "cycle-1"},
        strategy=SimpleNamespace(global_strategy_name="test_strategy"),
        record_transition=transitions,
    )


def test_light_only_stops_when_heavy_check_is_not_needed(outcomes, journal, transitions):
    evaluation = make_evaluation(
        score=make_score(action="NO_ACTION", score=10, reason="quiet"),
        position=make_position(),
        scheduled=SimpleNamespace(next_check_at=1_030.0),
    )

    result = _light_only(outcomes, journal, transitions, needs_heavy=False)(evaluation)

    assert result is not None and result.stage == "LIGHT_ONLY" and result.reason == "quiet"
    event_type, payload = journal.events[0]
    assert event_type == "attention_light_only"
    assert payload["next_check_at"] == 1_030.0 and payload["cycle_id"] == "cycle-1"
    assert transitions.calls == []


def test_light_only_requests_the_heavy_check(outcomes, journal, transitions):
    evaluation = make_evaluation(score=make_score(), position=make_position())

    result = _light_only(outcomes, journal, transitions, needs_heavy=True)(evaluation)

    assert result is None
    assert transitions.calls[0]["to_state"] is SymbolOperationalState.HEAVY_REQUIRED
    assert journal.events == []


def _budget(allowed: bool) -> RequestBudgetDecision:
    return RequestBudgetDecision(
        allowed=allowed,
        reason="OK" if allowed else "HEAVY_CHECK_BUDGET_EXHAUSTED",
        symbol="BTCUSDT",
        priority="COLD",
        action="OPEN_LONG",
        position_open=False,
        fast_lane=False,
        used_heavy_checks=3,
        max_heavy_checks=3,
    )


def test_request_budget_degrades_to_no_action_when_exhausted(outcomes, snapshots):
    from governor.models import SymbolRuntimeSnapshot

    snapshots["BTCUSDT"] = SymbolRuntimeSnapshot(
        symbol="BTCUSDT", enabled=True, status=SymbolRuntimeStatus.COLD
    )
    stage = RequestBudgetStage(outcomes=outcomes, consume_budget=lambda **_: _budget(False))
    evaluation = make_evaluation(score=make_score(), position=make_position())

    result = stage(evaluation)

    assert result is not None and result.stage == "REQUEST_BUDGET"
    assert result.classification == "degradacao"
    assert evaluation.budget_decision is not None and not evaluation.budget_decision.allowed
    assert snapshots["BTCUSDT"].reason == "HEAVY_CHECK_BUDGET_EXHAUSTED"
    assert snapshots["BTCUSDT"].status is SymbolRuntimeStatus.COLD


def test_request_budget_continues_when_allowed_or_disabled(outcomes):
    allowed = RequestBudgetStage(outcomes=outcomes, consume_budget=lambda **_: _budget(True))
    disabled = RequestBudgetStage(outcomes=outcomes, consume_budget=lambda **_: None)

    assert allowed(make_evaluation(score=make_score(), position=make_position())) is None
    assert disabled(make_evaluation(score=make_score(), position=make_position())) is None
