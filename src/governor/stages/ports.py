"""Small ports that evaluation stages declare in their constructors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, TypeVar

from governor.symbol_evaluation_pipeline import SymbolEvaluationContext
from governor.symbol_state_machine import SymbolOperationalState

T = TypeVar("T")

TracePayload = Callable[[], Mapping[str, str]]


class StateTransitionRecorder(Protocol):
    """Records an observational symbol state transition (``SymbolStateTracker``)."""

    def __call__(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        to_state: SymbolOperationalState | str,
        reason: str,
        stage: str,
        action: str | None = None,
        trade_plan_id: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None: ...


def required(value: T | None, field: str) -> T:
    """Return a context field an earlier stage must have filled."""

    if value is None:
        raise RuntimeError(f"Evaluation field '{field}' is not set by an earlier stage")
    return value
