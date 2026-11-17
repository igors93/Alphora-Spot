"""The deterministic decision stages shared by live and historical planning.

Data gathering and exchange dispatch stay outside this pair. Its ports supply
the environment-specific snapshots and record observational effects.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.smart_execution import SmartExecutionEngine, SmartExecutionInput
from governor.smart_sizing import SmartSizingEngine
from governor.stages.execution_planning import SmartExecutionStage
from governor.stages.ports import StateTransitionRecorder, TracePayload
from governor.stages.sizing import SmartSizingStage
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext
from utils.events import observe_event

PURE_DECISION_STAGES = (SmartSizingStage, SmartExecutionStage)


def build_decision_kernel_stages(
    *,
    outcomes: StageOutcomes,
    module_modes: Mapping[str, Any],
    sizing_engine: Callable[[], SmartSizingEngine],
    execution_engine: Callable[[], SmartExecutionEngine],
    config: Any,
    journal: EventJournal,
    trace_payload: TracePayload,
    record_transition: StateTransitionRecorder,
    execution_input_factory: Callable[[SymbolEvaluationContext], SmartExecutionInput] | None = None,
    limit_entry_supported: bool = False,
    audit_category: str = "governor",
    audit: Callable[..., None] = observe_event,
) -> tuple[SmartSizingStage, SmartExecutionStage]:
    return (
        SmartSizingStage(
            outcomes=outcomes,
            module_modes=module_modes,
            engine=sizing_engine,
            journal=journal,
            trace_payload=trace_payload,
            record_transition=record_transition,
            audit_category=audit_category,
            audit=audit,
        ),
        SmartExecutionStage(
            outcomes=outcomes,
            module_modes=module_modes,
            engine=execution_engine,
            config=config,
            journal=journal,
            trace_payload=trace_payload,
            input_factory=execution_input_factory,
            limit_entry_supported=limit_entry_supported,
            audit_category=audit_category,
            audit=audit,
        ),
    )
