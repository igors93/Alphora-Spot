"""Diagnostic context using contextvars for correlation IDs."""

from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

# Individual ContextVars — one per field so partial updates are cheap.
_CV_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("obs_run_id", default=None)
_CV_CYCLE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_cycle_id", default=None
)
_CV_SYMBOL_EVAL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_symbol_eval_id", default=None
)
_CV_SYMBOL: contextvars.ContextVar[str | None] = contextvars.ContextVar("obs_symbol", default=None)
_CV_STRATEGY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_strategy", default=None
)
_CV_INTENT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_intent_id", default=None
)
_CV_CANDIDATE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_candidate_id", default=None
)
_CV_CANDIDATE_REVISION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_candidate_revision", default=None
)
_CV_TRADE_PLAN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_trade_plan_id", default=None
)
_CV_DECISION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_decision_id", default=None
)
_CV_EXECUTION_COMMAND_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_execution_command_id", default=None
)
_CV_ORDER_CLIENT_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_order_client_id", default=None
)
_CV_BACKTEST_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_backtest_run_id", default=None
)
_CV_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "obs_request_id", default=None
)

_ALL_VARS: dict[str, contextvars.ContextVar[str | None]] = {
    "run_id": _CV_RUN_ID,
    "cycle_id": _CV_CYCLE_ID,
    "symbol_eval_id": _CV_SYMBOL_EVAL_ID,
    "symbol": _CV_SYMBOL,
    "strategy": _CV_STRATEGY,
    "intent_id": _CV_INTENT_ID,
    "candidate_id": _CV_CANDIDATE_ID,
    "candidate_revision": _CV_CANDIDATE_REVISION,
    "trade_plan_id": _CV_TRADE_PLAN_ID,
    "decision_id": _CV_DECISION_ID,
    "execution_command_id": _CV_EXECUTION_COMMAND_ID,
    "order_client_id": _CV_ORDER_CLIENT_ID,
    "backtest_run_id": _CV_BACKTEST_RUN_ID,
    "request_id": _CV_REQUEST_ID,
}


@dataclass(frozen=True, slots=True)
class DiagnosticContext:
    run_id: str | None = None
    cycle_id: str | None = None
    symbol_eval_id: str | None = None
    symbol: str | None = None
    strategy: str | None = None
    intent_id: str | None = None
    candidate_id: str | None = None
    candidate_revision: str | None = None
    trade_plan_id: str | None = None
    decision_id: str | None = None
    execution_command_id: str | None = None
    order_client_id: str | None = None
    backtest_run_id: str | None = None
    request_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        """Return only non-None fields as a plain dict."""
        return {
            k: v
            for k, v in {
                "run_id": self.run_id,
                "cycle_id": self.cycle_id,
                "symbol_eval_id": self.symbol_eval_id,
                "symbol": self.symbol,
                "strategy": self.strategy,
                "intent_id": self.intent_id,
                "candidate_id": self.candidate_id,
                "candidate_revision": self.candidate_revision,
                "trade_plan_id": self.trade_plan_id,
                "decision_id": self.decision_id,
                "execution_command_id": self.execution_command_id,
                "order_client_id": self.order_client_id,
                "backtest_run_id": self.backtest_run_id,
                "request_id": self.request_id,
            }.items()
            if v is not None
        }


def get_diagnostic_context() -> DiagnosticContext:
    """Read all context vars and return a snapshot."""
    return DiagnosticContext(
        run_id=_CV_RUN_ID.get(),
        cycle_id=_CV_CYCLE_ID.get(),
        symbol_eval_id=_CV_SYMBOL_EVAL_ID.get(),
        symbol=_CV_SYMBOL.get(),
        strategy=_CV_STRATEGY.get(),
        intent_id=_CV_INTENT_ID.get(),
        candidate_id=_CV_CANDIDATE_ID.get(),
        candidate_revision=_CV_CANDIDATE_REVISION.get(),
        trade_plan_id=_CV_TRADE_PLAN_ID.get(),
        decision_id=_CV_DECISION_ID.get(),
        execution_command_id=_CV_EXECUTION_COMMAND_ID.get(),
        order_client_id=_CV_ORDER_CLIENT_ID.get(),
        backtest_run_id=_CV_BACKTEST_RUN_ID.get(),
        request_id=_CV_REQUEST_ID.get(),
    )


@contextmanager
def diagnostic_context(**fields: Any) -> Generator[None, None, None]:
    """Context manager that sets correlation IDs and restores them on exit.

    Nested calls stack correctly. Exceptions do not leak context.
    asyncio tasks inherit context from their creation point (Python default).
    New threads do NOT inherit context (ContextVar semantics).
    """
    tokens: list[tuple[contextvars.ContextVar[str | None], contextvars.Token[str | None]]] = []
    try:
        for name, value in fields.items():
            cv = _ALL_VARS.get(name)
            if cv is not None:
                token = cv.set(value)
                tokens.append((cv, token))
        yield
    finally:
        # Restore in reverse order
        for cv, token in reversed(tokens):
            cv.reset(token)


def bind_diagnostic_context(**fields: Any) -> None:
    """Set context vars without a context manager (fire-and-forget)."""
    for name, value in fields.items():
        cv = _ALL_VARS.get(name)
        if cv is not None:
            cv.set(value)


def clear_diagnostic_context() -> None:
    """Reset all context vars to None."""
    for cv in _ALL_VARS.values():
        cv.set(None)
