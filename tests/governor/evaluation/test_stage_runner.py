"""G3: the runner executes legacy methods and stage objects side by side."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from governor.evaluation import (
    EvaluationStage,
    StageOutcomes,
    StageRunner,
    SymbolRuntimeBoard,
    stage_metric_name,
)
from governor.models import SymbolRuntimeSnapshot, SymbolRuntimeStatus
from governor.symbol_evaluation_pipeline import (
    SymbolEvaluationContext,
    SymbolEvaluationInput,
    SymbolEvaluationResult,
)
from scanner.signal_score import SignalPriority


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, event_type: str, **data) -> None:
        self.events.append((event_type, data))


def _evaluation(symbol: str = "BTCUSDT") -> SymbolEvaluationContext:
    return SymbolEvaluationContext(
        evaluation_input=SymbolEvaluationInput(
            symbol_config=SimpleNamespace(symbol=symbol),
            now=100.0,
            cycle_id="cycle-1",
            symbol_eval_id="eval-1",
        )
    )


def _outcomes(journal: FakeJournal, snapshots: dict | None = None) -> StageOutcomes:
    return StageOutcomes(
        journal=journal,
        trace_payload=lambda: {"cycle_id": "cycle-1"},
        board=SymbolRuntimeBoard(
            {} if snapshots is None else snapshots,
            strategy=SimpleNamespace(global_strategy_name="test_strategy"),
        ),
    )


class _ObjectStage:
    name = "object_stage"

    def __init__(self, outcomes: StageOutcomes, *, stop: bool) -> None:
        self.outcomes = outcomes
        self.stop = stop
        self.calls = 0

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        self.calls += 1
        if not self.stop:
            return None
        return self.outcomes.terminal(
            evaluation, stage="OBJECT", classification="observacao", reason="DONE"
        )


def test_stage_metric_name_reads_declared_names_and_function_names() -> None:
    def _handle_warmup(_evaluation):
        return None

    stage = _ObjectStage(_outcomes(FakeJournal()), stop=False)

    assert stage_metric_name(_handle_warmup) == "handle_warmup"
    assert stage_metric_name(stage) == "object_stage"
    assert isinstance(stage, EvaluationStage)


def test_runner_runs_a_mixed_tuple_and_stops_at_the_first_terminal_result() -> None:
    journal = FakeJournal()
    outcomes = _outcomes(journal)
    evaluation = _evaluation()

    def _legacy_method(_evaluation):
        return None

    terminal = _ObjectStage(outcomes, stop=True)
    never_runs = _ObjectStage(outcomes, stop=False)

    result = StageRunner().run(evaluation, (_legacy_method, terminal, never_runs))

    assert result is not None and result.stage == "OBJECT" and result.terminal
    assert never_runs.calls == 0
    trace = [
        (item["stage"], item["status"], item["terminal"])
        for item in evaluation.diagnostics["stage_timings"]
    ]
    assert trace == [("legacy_method", "OK", False), ("object_stage", "OK", True)]
    assert journal.events[0][0] == "governor_evaluate_symbol_flow_decision"
    assert journal.events[0][1]["cycle_id"] == "cycle-1"


def test_runner_records_the_failing_stage_and_restores_the_trace_scope() -> None:
    scope_events: list[str] = []

    @contextmanager
    def trace_scope(evaluation):
        scope_events.append(f"enter:{evaluation.evaluation_input.symbol_eval_id}")
        try:
            yield
        finally:
            scope_events.append("exit")

    def _explodes(_evaluation):
        raise RuntimeError("boom")

    evaluation = _evaluation()
    with pytest.raises(RuntimeError):
        StageRunner(trace_scope=trace_scope).run(evaluation, (_explodes,))

    assert evaluation.diagnostics["stage_timings"][0]["status"] == "ERROR"
    assert scope_events == ["enter:eval-1", "exit"]


def test_blocked_outcome_updates_the_board_before_returning() -> None:
    journal = FakeJournal()
    snapshots = {
        "BTCUSDT": SymbolRuntimeSnapshot(
            symbol="BTCUSDT",
            enabled=True,
            status=SymbolRuntimeStatus.READY,
            score=80,
            priority=SignalPriority.HOT,
            last_price="100",
        )
    }
    evaluation = _evaluation()

    result = _outcomes(journal, snapshots).blocked(
        evaluation, stage="GUARD", classification="absoluto", reason="BLOCKED_BY_TEST"
    )

    board_entry = snapshots["BTCUSDT"]
    assert board_entry.status is SymbolRuntimeStatus.BLOCKED
    assert board_entry.score == 80 and board_entry.last_price == "100"
    assert board_entry.strategy == "test_strategy"
    assert result.reason == "BLOCKED_BY_TEST" and result.classification == "absoluto"


def test_board_no_action_only_updates_known_symbols() -> None:
    snapshots: dict[str, SymbolRuntimeSnapshot] = {}
    board = SymbolRuntimeBoard(snapshots, strategy=SimpleNamespace(global_strategy_name="s"))

    board.mark_no_action("BTCUSDT", "IGNORED")
    assert snapshots == {}

    board.publish(
        SymbolRuntimeSnapshot(symbol="BTCUSDT", enabled=True, status=SymbolRuntimeStatus.COLD)
    )
    board.mark_no_action("BTCUSDT", "NO_SIGNAL")
    assert snapshots["BTCUSDT"].reason == "NO_SIGNAL"
    assert snapshots["BTCUSDT"].status is SymbolRuntimeStatus.COLD
