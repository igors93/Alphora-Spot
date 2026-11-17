"""Symbol evaluation building blocks: stage protocol, runner and shared outcomes.

The Governor evaluates each symbol by running a tuple of stages over a shared
``SymbolEvaluationContext``. A stage is any callable ``(context) -> result | None``:
a bound Governor method (legacy) or a small object with declared dependencies.
"""

from governor.evaluation.outcomes import StageOutcomes
from governor.evaluation.runtime_board import SymbolRuntimeBoard
from governor.evaluation.stage import (
    EvaluationStage,
    StageCallable,
    StageRunner,
    stage_metric_name,
)

__all__ = [
    "EvaluationStage",
    "StageCallable",
    "StageOutcomes",
    "StageRunner",
    "SymbolRuntimeBoard",
    "stage_metric_name",
]
