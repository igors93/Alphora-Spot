"""Compatibility alias of ``utils.run_context`` (migration M7).

The run context moved to the base layer so ``utils`` stops importing ``observability``.
Removed when ``desktop.bridge.handlers`` imports ``utils.run_context``.
"""

from utils.run_context import (  # noqa: F401
    RunContext,
    get_run_context,
    get_run_id,
    initialize_run_context,
    reset_run_context_for_testing,
)
