"""Centralized logprivacy configuration for Alphora.

DEPRECATION NOTE: This module is a compatibility wrapper.
The canonical implementation has moved to:
  src/observability/integrations/logprivacy_adapter.py

All new code should import from there. This module re-exports the same API
for backward compatibility and will be removed in a future migration phase.

To change version or adjust policy:
  1. Update the range in requirements.txt if needed.
  2. Revise _ALPHORA_EXTRA_SENSITIVE_KEYS in logprivacy_adapter.py.
  3. Adjust _build_policy() in logprivacy_adapter.py if the CleanerPolicy API changes.
  4. No other part of the code needs to change.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any

# Re-export canonical objects from the adapter
from logprivacy import (
    get_safe_logger,  # re-exported so existing tests can patch utils.privacy.get_safe_logger
)

from utils.logprivacy_adapter import (
    ALPHORA_POLICY,
    sanitize_payload,
)
from utils.logprivacy_adapter import (
    LOGPRIVACY_VERSION as LOGPRIVACY_VERSION,
)
from utils.logprivacy_adapter import (
    install_privacy_filter as install_privacy_filter,
)
from utils.logprivacy_adapter import (
    sanitize_text as sanitize_text,
)

try:
    _logprivacy_version: str = version("logprivacy")
except PackageNotFoundError:
    _logprivacy_version = "not-installed"

# Keep LOGPRIVACY_VERSION accessible at module level (re-exported above)


class PrivacyInitializationError(RuntimeError):
    """Raised when privacy protection cannot be installed on logging handlers."""


def sanitize_for_audit(value: Any) -> Any:
    """Apply logprivacy masking to a value.

    Delegates to utils.logprivacy_adapter.sanitize_payload.
    """
    return sanitize_payload(value)


def to_safe_fallback(value: Any) -> Any:
    """Convert an unknown-type object without calling str().

    Delegates to utils.logprivacy_adapter.sanitize_payload.
    Uses logprivacy's to_safe_data which returns '[UNSUPPORTED:ClassName]'
    for objects that are not native JSON types.
    """
    from utils.logprivacy_adapter import sanitize_payload as _sp

    return _sp(value)


def apply_privacy_filter(logger: logging.Logger, *, required: bool = True) -> None:
    """Install LogPrivacyFilter on all handlers reachable by the logger.

    If required=True (default) and installation fails, raises
    PrivacyInitializationError instead of silently continuing.

    NOTE: This method uses get_safe_logger from the utils.privacy namespace
    so that tests patching utils.privacy.get_safe_logger work correctly.
    """
    try:
        name = None if logger.name == "root" else logger.name
        get_safe_logger(name, policy=ALPHORA_POLICY)
    except Exception as error:
        if required:
            raise PrivacyInitializationError(
                f"Could not initialize privacy protection: {type(error).__name__}"
            ) from error
