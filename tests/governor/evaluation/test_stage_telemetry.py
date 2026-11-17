"""G9: one event per evaluation stage, aggregated into the cycle metrics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from governor.cycle.telemetry import CycleMetricsState, CycleTelemetry
from governor.evaluation import StageRunner
from governor.evaluation.stage_telemetry import STAGE_EVENT, StageTelemetry
from governor.events import GOVERNOR_CYCLE_METRICS
from governor.symbol_evaluation_pipeline import (
    SymbolEvaluationContext,
    SymbolEvaluationInput,
    SymbolEvaluationResult,
)
from utils.events import EventChannel, EventRecord, register_event_sink, unregister_event_sink


def _evaluation() -> SymbolEvaluationContext:
    return SymbolEvaluationContext(
        evaluation_input=SymbolEvaluationInput(
            symbol_config=SimpleNamespace(symbol="BTCUSDT"),
            now=100.0,
            cycle_id="cycle-1",
            symbol_eval_id="eval-1",
        )
    )


def _pass(evaluation: SymbolEvaluationContext) -> None:
    return None


def _block(evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult:
    return SymbolEvaluationResult(
        symbol="BTCUSDT",
        action="NONE",
        status="BLOCKED",
        terminal=True,
        execution_will_run=False,
        reason="PORTFOLIO_LIMIT",
        stage="RISK",
        classification="bloqueio",
    )


def _boom(evaluation: SymbolEvaluationContext) -> None:
    raise RuntimeError("stage failed")


@pytest.fixture
def telemetry():
    sink = StageTelemetry()
    register_event_sink(sink)
    yield sink
    unregister_event_sink(sink)


def test_runner_emits_one_telemetry_event_per_stage():
    records: list[EventRecord] = []

    class _Capture:
        def accepts(self, record: EventRecord) -> bool:
            return record.channel is EventChannel.TELEMETRY

        def write(self, record: EventRecord) -> None:
            records.append(record)

    capture = _Capture()
    register_event_sink(capture)
    try:
        StageRunner().run(_evaluation(), (_pass, _block, _pass))
    finally:
        unregister_event_sink(capture)

    assert [record.event_type for record in records] == [STAGE_EVENT, STAGE_EVENT]
    first, last = (record.payload for record in records)
    assert first["stage"] == "pass" and first["terminal"] is False
    assert first["classification"] == "" and first["status"] == "OK"
    assert last["stage"] == "block" and last["terminal"] is True
    assert (last["classification"], last["reason"]) == ("bloqueio", "PORTFOLIO_LIMIT")
    assert last["symbol"] == "BTCUSDT" and last["cycle_id"] == "cycle-1"
    assert last["duration_ms"] >= 0


def test_stage_telemetry_summarizes_and_resets(telemetry):
    runner = StageRunner()
    runner.run(_evaluation(), (_pass, _block))
    runner.run(_evaluation(), (_pass, _pass))
    with pytest.raises(RuntimeError):
        runner.run(_evaluation(), (_boom,))

    summary = telemetry.summary_and_reset()

    assert summary["pass"]["count"] == 3
    assert summary["block"]["terminal"] == 1
    assert summary["block"]["terminal_classifications"] == {"bloqueio": 1}
    assert summary["boom"]["errors"] == 1
    assert set(summary["pass"]) >= {"total_ms", "max_ms", "p50_ms", "p95_ms"}
    assert telemetry.summary_and_reset() == {}


def test_cycle_metrics_carry_the_stage_summary(telemetry):
    StageRunner().run(_evaluation(), (_pass, _block))
    journal_events: list[tuple[str, dict]] = []
    cycle_telemetry = CycleTelemetry(
        config=SimpleNamespace(
            governor_observability_enabled=True,
            governor_cycle_metrics_interval_seconds=0.0,
            governor_slow_cycle_ms=1500.0,
        ),
        journal=SimpleNamespace(
            append=lambda event_type, **data: journal_events.append((event_type, data))
        ),
        logger=SimpleNamespace(warning=lambda *args: None),
        market_data_hub=None,
        state=CycleMetricsState(),
        stage_telemetry=telemetry,
    )

    cycle_telemetry.record_cycle_metrics(
        current_time=100.0,
        cycle_duration_ms=12.0,
        due_symbols=["BTCUSDT"],
        dirty_symbols=[],
        open_symbols=set(),
        shock_events=[],
        symbol_timings=[{"symbol": "BTCUSDT", "duration_ms": 12.0}],
    )

    ((event_type, payload),) = journal_events
    assert event_type == GOVERNOR_CYCLE_METRICS
    assert payload["stage_telemetry"]["block"]["terminal"] == 1
