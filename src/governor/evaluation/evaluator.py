"""SymbolEvaluator: the lifecycle of one symbol evaluation across the light and heavy phases."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from config.symbol_context import SymbolTradingConfig
from governor.cycle_context import CycleContext
from governor.evaluation.outcomes import EventJournal
from governor.evaluation.stage import StageCallable, StageRunner
from governor.governor_observability import elapsed_ms, monotonic_now
from governor.symbol_evaluation_pipeline import (
    SymbolEvaluationContext,
    SymbolEvaluationInput,
    SymbolEvaluationResult,
)
from utils.events import EventChannel, observe_error, observe_event

StageTuple = Callable[[], Sequence[StageCallable]]
TracePayload = Callable[[], Mapping[str, str]]


class SymbolEvaluator:
    """Run one symbol through stages 1–8 (light) and, if it continues, 9–22 (heavy).

    ``prepare`` runs the light stages and parks the evaluation; the cycle prefetches
    the heavy snapshots of every parked symbol and then calls ``resume`` one symbol
    at a time. Every evaluation ends with exactly one stage-metrics event and one
    ``symbol_evaluation_finished`` audit event, also when a stage raises.
    """

    def __init__(
        self,
        *,
        runner: StageRunner,
        light_stages: StageTuple,
        heavy_stages: StageTuple,
        journal: EventJournal,
        trace_payload: TracePayload,
        strategy_name: Callable[[], str],
        cycle_id_for: Callable[[CycleContext | None], str],
        next_symbol_eval_id: Callable[..., str],
    ) -> None:
        self.runner = runner
        self.light_stages = light_stages
        self.heavy_stages = heavy_stages
        self.journal = journal
        self.trace_payload = trace_payload
        self.strategy_name = strategy_name
        self.cycle_id_for = cycle_id_for
        self.next_symbol_eval_id = next_symbol_eval_id

    def evaluate(
        self,
        symbol_config: SymbolTradingConfig,
        now: float,
        *,
        context: CycleContext | None = None,
    ) -> None:
        evaluation = self.prepare(symbol_config, now, context=context)
        if evaluation.terminal_result is None:
            self.resume(evaluation)

    def prepare(
        self,
        symbol_config: SymbolTradingConfig,
        now: float,
        *,
        context: CycleContext | None = None,
    ) -> SymbolEvaluationContext:
        cycle_id = self.cycle_id_for(context)
        symbol_eval_id = self.next_symbol_eval_id(cycle_id=cycle_id, symbol=symbol_config.symbol)
        evaluation = SymbolEvaluationContext(
            evaluation_input=SymbolEvaluationInput(
                symbol_config=symbol_config,
                now=now,
                cycle_id=cycle_id,
                symbol_eval_id=symbol_eval_id,
            ),
            cycle_context=context,
            lifecycle_started_at=monotonic_now(),
        )
        evaluation.diagnostics["stage_timings"] = []
        observe_event(
            "symbol_evaluation_started",
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            cycle_id=cycle_id,
            symbol_eval_id=symbol_eval_id,
            symbol=symbol_config.symbol,
            strategy=self.strategy_name(),
        )

        try:
            result = self.run_stages(evaluation, self.light_stages())
            if result is not None and result.terminal:
                evaluation.terminal_result = result
                self.record_stage_metrics(evaluation, terminal_stage=result.stage)
                self._finish(evaluation, status="OK")
        except Exception as error:
            self._fail(evaluation, error)
            raise
        return evaluation

    def resume(self, evaluation: SymbolEvaluationContext) -> None:
        if evaluation.lifecycle_finished or evaluation.terminal_result is not None:
            return
        try:
            result = self.run_stages(evaluation, self.heavy_stages())
            if result is not None and result.terminal:
                evaluation.terminal_result = result
                terminal_stage = result.stage
            else:
                terminal_stage = "NON_TERMINAL"
            self.record_stage_metrics(evaluation, terminal_stage=terminal_stage)
            self._finish(evaluation, status="OK")
        except Exception as error:
            self._fail(evaluation, error)
            raise

    def run_stages(
        self,
        evaluation: SymbolEvaluationContext,
        stages: Sequence[StageCallable],
    ) -> SymbolEvaluationResult | None:
        return self.runner.run(evaluation, stages)

    def record_stage_metrics(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        terminal_stage: str,
    ) -> None:
        stage_timings = evaluation.diagnostics.get("stage_timings", [])
        self.journal.append(
            "symbol_evaluation_stage_metrics",
            **self.trace_payload(),
            symbol=evaluation.evaluation_input.symbol_config.symbol,
            terminal_stage=terminal_stage,
            stages=stage_timings,
            total_stage_duration_ms=sum(
                float(item.get("duration_ms", 0.0) or 0.0)
                for item in stage_timings
                if isinstance(item, dict)
            ),
            execution_changed=False,
            order_mutation=False,
        )

    def _fail(self, evaluation: SymbolEvaluationContext, error: Exception) -> None:
        self.record_stage_metrics(evaluation, terminal_stage="ERROR")
        self._record_error(evaluation, error)
        self._finish(evaluation, status="ERROR")

    def _record_error(self, evaluation: SymbolEvaluationContext, error: Exception) -> None:
        evaluation_input = evaluation.evaluation_input
        # One call: the AUDIT channel records the error in the run audit trail and in the
        # diagnostics (ADR-006), where it was emitted a second time as symbol_evaluation_failed.
        observe_error(
            "symbol_evaluation_error",
            error=error,
            component="governor.symbol_eval",
            category="governor",
            channel=EventChannel.AUDIT,
            cycle_id=evaluation_input.cycle_id,
            symbol_eval_id=evaluation_input.symbol_eval_id,
            symbol=evaluation_input.symbol_config.symbol,
        )

    def _finish(self, evaluation: SymbolEvaluationContext, *, status: str) -> None:
        if evaluation.lifecycle_finished:
            return
        evaluation.lifecycle_finished = True
        observe_event(
            "symbol_evaluation_finished",
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            severity="ERROR" if status == "ERROR" else "INFO",
            cycle_id=evaluation.evaluation_input.cycle_id,
            symbol_eval_id=evaluation.evaluation_input.symbol_eval_id,
            symbol=evaluation.evaluation_input.symbol_config.symbol,
            status=status,
            duration_ms=elapsed_ms(evaluation.lifecycle_started_at),
        )
