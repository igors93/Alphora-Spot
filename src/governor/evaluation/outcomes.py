"""Shared terminal outcomes for evaluation stages.

Every stage that stops the pipeline produces the same artefacts: a
``SymbolEvaluationResult``, an optional flow-decision journal event and, for
blocks, an update of the runtime board. ``StageOutcomes`` owns that translation
so stages do not need the Governor to build them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from domain.decisions import GateDecision
from governor.evaluation.runtime_board import SymbolRuntimeBoard
from governor.events import GOVERNOR_EVALUATE_SYMBOL_FLOW_DECISION
from governor.safe_journal import safe_append
from governor.symbol_evaluation_pipeline import (
    SymbolEvaluationContext,
    SymbolEvaluationResult,
    SymbolEvaluationStageResult,
)


class EventJournal(Protocol):
    def append(self, event_type: str, **data: Any) -> None: ...


TracePayload = Callable[[], Mapping[str, str]]

_FLOW_DECISION_SAFETY_NOTE = (
    "Auditoria de integracao: classifica o ponto de retorno do "
    "_evaluate_symbol sem alterar decisao ou execucao."
)


class StageOutcomes:
    """Build terminal results and record the block side effects of a stage."""

    def __init__(
        self,
        *,
        journal: EventJournal,
        trace_payload: TracePayload,
        board: SymbolRuntimeBoard,
        logger: Any = None,
    ) -> None:
        self.journal = journal
        self.trace_payload = trace_payload
        self.board = board
        self.logger = logger

    def terminal(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        stage: str,
        classification: str,
        reason: str,
        execution_will_run: bool = False,
        action: str | None = None,
        record_flow_decision: bool = True,
    ) -> SymbolEvaluationResult:
        stage_result = SymbolEvaluationStageResult(
            stage=stage,
            classification=classification,
            reason=reason,
            terminal=True,
            execution_will_run=execution_will_run,
            action=action,
        )
        if record_flow_decision:
            self.record_flow_decision(
                symbol=evaluation.evaluation_input.symbol_config.symbol,
                stage=stage_result.stage,
                classification=stage_result.classification,
                reason=stage_result.reason,
                terminal=stage_result.terminal,
                execution_will_run=stage_result.execution_will_run,
                action=stage_result.action,
            )
        return self.result(evaluation, stage_result)

    def blocked(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        stage: str,
        classification: str,
        reason: str,
        action: str | None = None,
    ) -> SymbolEvaluationResult:
        """Mark the symbol blocked on the board, then stop with a terminal result."""

        self.board.mark_blocked(evaluation.evaluation_input.symbol_config.symbol, reason)
        return self.terminal(
            evaluation,
            stage=stage,
            classification=classification,
            reason=reason,
            action=action,
        )

    def gate_blocked(
        self,
        evaluation: SymbolEvaluationContext,
        gate: GateDecision,
        *,
        stage: str,
        classification: str,
        action: str | None = None,
    ) -> SymbolEvaluationResult:
        """Translate a blocking ``GateDecision`` into the terminal result of ``stage``."""

        if not gate.blocked:
            raise ValueError(f"Gate {gate.gate} did not block: {gate.verdict.value}")
        return self.blocked(
            evaluation,
            stage=stage,
            classification=classification,
            reason=gate.message,
            action=action,
        )

    @staticmethod
    def result(
        evaluation: SymbolEvaluationContext,
        stage_result: SymbolEvaluationStageResult,
    ) -> SymbolEvaluationResult:
        status = getattr(evaluation.status, "value", evaluation.status or "")
        return SymbolEvaluationResult(
            symbol=evaluation.evaluation_input.symbol_config.symbol,
            action=stage_result.action or "",
            status=str(status),
            terminal=stage_result.terminal,
            execution_will_run=stage_result.execution_will_run,
            reason=stage_result.reason,
            stage=stage_result.stage,
            classification=stage_result.classification,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            trade_plan_id=evaluation.trade_plan_id,
            diagnostics={
                **evaluation.diagnostics,
                **stage_result.diagnostics,
            },
        )

    def record_flow_decision(
        self,
        *,
        symbol: str,
        stage: str,
        classification: str,
        reason: str,
        terminal: bool,
        execution_will_run: bool,
        action: str | None = None,
    ) -> None:
        safe_append(
            self.journal,
            GOVERNOR_EVALUATE_SYMBOL_FLOW_DECISION,
            logger=self.logger,
            **self.trace_payload(),
            symbol=symbol,
            stage=stage,
            classification=classification,
            reason=reason,
            action=action or "",
            terminal=terminal,
            execution_will_run=execution_will_run,
            execution_changed=False,
            order_mutation=False,
            diagnostics={"safety_note": _FLOW_DECISION_SAFETY_NOTE},
        )
