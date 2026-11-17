"""Stage protocol and the runner that executes a tuple of evaluation stages."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Protocol, runtime_checkable

from governor.evaluation.stage_telemetry import STAGE_COMPONENT, STAGE_EVENT
from governor.governor_observability import elapsed_ms, monotonic_now
from governor.symbol_evaluation_pipeline import (
    SymbolEvaluationContext,
    SymbolEvaluationResult,
)
from observability import diagnostic_context
from utils.events import EventChannel, observe_event

StageCallable = Callable[[SymbolEvaluationContext], "SymbolEvaluationResult | None"]


@runtime_checkable
class EvaluationStage(Protocol):
    """One step of the symbol evaluation pipeline.

    ``name`` is the metric name recorded in ``stage_timings``. Returning a terminal
    ``SymbolEvaluationResult`` stops the pipeline; returning ``None`` continues.
    """

    name: str

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None: ...


def stage_metric_name(stage: Callable[..., Any]) -> str:
    """Return the telemetry name of a stage: its declared ``name`` or its function name.

    Every Governor stage is an object with a ``name`` (the metric names of the former
    Governor methods, so the stage trail never changed); plain functions are still
    accepted, for tests and ad-hoc stages.
    """

    declared = getattr(stage, "name", None)
    if isinstance(declared, str) and declared:
        return declared
    return str(getattr(stage, "__name__", type(stage).__name__)).removeprefix("_")


TraceScope = Callable[[SymbolEvaluationContext], AbstractContextManager[Any]]


def _no_trace_scope(_evaluation: SymbolEvaluationContext) -> AbstractContextManager[Any]:
    return nullcontext()


class StageRunner:
    """Run stages in order, time each one and stop at the first terminal result.

    ``trace_scope`` lets the host publish the cycle and evaluation identifiers
    while the stages run (the Governor uses it for its trace payload).
    """

    def __init__(self, *, trace_scope: TraceScope = _no_trace_scope) -> None:
        self._trace_scope = trace_scope

    def run(
        self,
        evaluation: SymbolEvaluationContext,
        stages: Sequence[StageCallable],
    ) -> SymbolEvaluationResult | None:
        evaluation_input = evaluation.evaluation_input
        stage_timings = evaluation.diagnostics.setdefault("stage_timings", [])
        with self._trace_scope(evaluation):
            with diagnostic_context(
                cycle_id=evaluation_input.cycle_id,
                symbol_eval_id=evaluation_input.symbol_eval_id,
                symbol=evaluation_input.symbol_config.symbol,
            ):
                for stage in stages:
                    stage_started = monotonic_now()
                    result: SymbolEvaluationResult | None = None
                    status = "OK"
                    try:
                        result = stage(evaluation)
                    except Exception:
                        status = "ERROR"
                        raise
                    finally:
                        timing = {
                            "stage": stage_metric_name(stage),
                            "duration_ms": elapsed_ms(stage_started),
                            "status": status,
                            "terminal": bool(result is not None and result.terminal),
                        }
                        stage_timings.append(timing)
                        _emit_stage_event(evaluation, timing, result)
                    if result is not None and result.terminal:
                        return result
        return None


def _emit_stage_event(
    evaluation: SymbolEvaluationContext,
    timing: dict[str, Any],
    result: SymbolEvaluationResult | None,
) -> None:
    """G9: one event per stage on the single event API (``TELEMETRY`` channel)."""

    evaluation_input = evaluation.evaluation_input
    observe_event(
        STAGE_EVENT,
        component=STAGE_COMPONENT,
        category="governor",
        channel=EventChannel.TELEMETRY,
        symbol=evaluation_input.symbol_config.symbol,
        cycle_id=evaluation_input.cycle_id,
        symbol_eval_id=evaluation_input.symbol_eval_id,
        **timing,
        classification=result.classification if result is not None else "",
        reason=result.reason if result is not None else "",
    )
