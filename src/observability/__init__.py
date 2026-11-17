"""Alphora Internal Observability subsystem.

Public API — import from here, not from submodules.
"""

from observability.application.error_diagnostics import (
    register_error_diagnostics,
    unregister_error_diagnostics,
)
from observability.application.invariants import observe_invariant, require_invariant
from observability.application.service import observe_error, observe_event, observe_health
from observability.bootstrap import initialize_observability, shutdown_observability
from observability.domain.context import (
    diagnostic_context,
    get_diagnostic_context,
)
from observability.domain.events import EventChannel, EventSeverity
from utils.run_context import (
    get_run_context,
    get_run_id,
    initialize_run_context,
)

__all__ = [
    "initialize_observability",
    "shutdown_observability",
    "diagnostic_context",
    "get_diagnostic_context",
    "initialize_run_context",
    "get_run_context",
    "get_run_id",
    "observe_event",
    "observe_error",
    "observe_health",
    "observe_invariant",
    "require_invariant",
    "register_error_diagnostics",
    "unregister_error_diagnostics",
    "EventChannel",
    "EventSeverity",
]
