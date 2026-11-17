"""Observability subsystem exception hierarchy."""

from __future__ import annotations

# Raised by the privacy policy, which lives in the base layer (utils.logprivacy_adapter).
from utils.logprivacy_adapter import PrivacyBootFailure as PrivacyBootFailure


class ObservabilityError(Exception):
    """Base class for all observability errors."""


class ObservabilityShutdownError(ObservabilityError):
    """Raised when shutdown encounters an unrecoverable problem."""
