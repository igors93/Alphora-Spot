"""Canonical logprivacy integration: privacy policy and sanitization of every log and event.

It lives in the base layer so logging, the audit trail and observability share one policy;
src/utils/privacy.py delegates to this module for backward compatibility.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from logprivacy import CleanerPolicy, clean, get_safe_logger, to_safe_data


class PrivacyBootFailure(Exception):
    """Raised when privacy initialization fails at boot time.

    This exception MUST prevent the bot from starting.
    """


try:
    LOGPRIVACY_VERSION: str = version("logprivacy")
except PackageNotFoundError:
    LOGPRIVACY_VERSION = "not-installed"

# Alphora-specific sensitive keys not covered by logprivacy defaults.
# Already covered by defaults: password, passwd, pwd, secret, token, api_key, apikey,
# access_key, access_token, refresh_token, client_secret, private_key,
# authorization, cookie, set-cookie.
_ALPHORA_EXTRA_SENSITIVE_KEYS: tuple[str, ...] = (
    # api_secret variants
    "api_secret",
    "apisecret",
    "apiSecret",
    # listen_key variants
    "listen_key",
    "listenkey",
    "listenKey",
    # secret_key variants
    "secret_key",
    "secretkey",
    "secretKey",
    # signature variants
    "signature",
    # Binance-specific headers and fields
    "x-mbx-apikey",
    "x_mbx_apikey",
    "xmbxapikey",
    "xMbxApiKey",
    "binance_secret",
    "binancesecret",
    "binanceSecret",
)

_MAX_SANITIZE_DEPTH = 32
_MAX_SANITIZE_BREADTH = 256
_MASK = "[REDACTED]"
_TEXT_SECRET_PATTERN = re.compile(
    r"(?P<prefix>\b(?:"
    + "|".join(re.escape(key) for key in _ALPHORA_EXTRA_SENSITIVE_KEYS)
    + r"|api_key|apikey|token|secret|password|authorization|cookie"
    + r")\b\s*(?:=|:)\s*)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)


def _build_policy() -> CleanerPolicy:
    base = CleanerPolicy.default()
    merged = tuple(dict.fromkeys(base.sensitive_keys + _ALPHORA_EXTRA_SENSITIVE_KEYS))
    policy = replace(base, sensitive_keys=merged)
    # Trade/order ids embed epoch-ms timestamps (e.g. ALP_BTC_ENTRY_1783099072085)
    # that can pass the Luhn check and get masked as [CREDIT_CARD], corrupting the
    # audit trail. Card numbers never legitimately appear in this system.
    return policy.without_categories("credit_card")


try:
    ALPHORA_POLICY: CleanerPolicy = _build_policy()
except Exception as _policy_err:
    raise PrivacyBootFailure(
        f"Failed to build Alphora privacy policy: {_policy_err}"
    ) from _policy_err


def sanitize_payload(value: Any) -> Any:
    """Sanitize any value using logprivacy.

    Handles unknown types safely — never calls str() on unknown objects.
    On sanitization failure returns a safe placeholder.
    """
    try:
        safe = to_safe_data(value, policy=ALPHORA_POLICY)
        return clean(safe, policy=ALPHORA_POLICY)
    except Exception:
        type_name = type(value).__name__
        return f"[SANITIZATION_FAILED:{type_name}]"


def sanitize_text(value: str) -> str:
    """Sanitize a text string."""
    try:
        masked = _TEXT_SECRET_PATTERN.sub(
            lambda match: f"{match.group('prefix')}{_MASK}", str(value)
        )
        result = clean(masked, policy=ALPHORA_POLICY)
        return str(result)
    except Exception:
        return "[SANITIZATION_FAILED:str]"


class _LogPrivacyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = sanitize_text(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = "[SANITIZATION_FAILED:LogRecord]"
            record.args = ()
        return True


def install_privacy_filter(logger: logging.Logger, *, required: bool = True) -> None:
    """Install logprivacy filter on all reachable handlers.

    Raises PrivacyBootFailure if required=True and installation fails.
    """
    try:
        name = None if logger.name == "root" else logger.name
        get_safe_logger(name, policy=ALPHORA_POLICY)
        _install_record_filter(logger)
    except Exception as error:
        if required:
            raise PrivacyBootFailure(
                f"Could not initialize privacy protection: {type(error).__name__}"
            ) from error


def _install_record_filter(logger: logging.Logger) -> None:
    targets: list[logging.Logger] = [logger]
    if logger is not logging.getLogger():
        targets.append(logging.getLogger())
    for target in targets:
        if not any(isinstance(item, _LogPrivacyFilter) for item in target.filters):
            target.addFilter(_LogPrivacyFilter())
        for handler in target.handlers:
            if not any(isinstance(item, _LogPrivacyFilter) for item in handler.filters):
                handler.addFilter(_LogPrivacyFilter())
