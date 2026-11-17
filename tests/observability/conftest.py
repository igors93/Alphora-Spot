"""Shared fixtures for observability tests."""

from __future__ import annotations

import logging

import pytest

from observability.application.lifecycle import reset_exception_hooks_for_testing
from observability.application.service import set_service
from observability.bootstrap import reset_observability_bootstrap_for_testing
from observability.infrastructure.text_logging import reset_text_logging_registry
from utils.run_context import reset_run_context_for_testing


@pytest.fixture(autouse=True)
def reset_singleton():
    """Ensure no singleton bleeds between tests."""
    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_handlers = list(root_logger.handlers)
    original_filters = list(root_logger.filters)
    original_handler_filters = {handler: list(handler.filters) for handler in original_handlers}

    reset_observability_bootstrap_for_testing()
    reset_exception_hooks_for_testing()
    reset_run_context_for_testing()
    set_service(None)
    reset_text_logging_registry()
    yield

    reset_observability_bootstrap_for_testing()
    reset_exception_hooks_for_testing()
    reset_run_context_for_testing()
    set_service(None)
    reset_text_logging_registry()
    for handler in list(root_logger.handlers):
        if handler not in original_handlers:
            root_logger.removeHandler(handler)
            handler.close()
    root_logger.handlers[:] = original_handlers
    root_logger.filters[:] = original_filters
    root_logger.setLevel(original_level)
    for handler, filters in original_handler_filters.items():
        handler.filters[:] = filters
