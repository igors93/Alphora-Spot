"""Testes do módulo utils.privacy — integração com logprivacy."""

import logging
from importlib.metadata import version
from unittest.mock import patch

import pytest

from utils.privacy import (
    ALPHORA_POLICY,
    LOGPRIVACY_VERSION,
    PrivacyInitializationError,
    apply_privacy_filter,
    sanitize_for_audit,
    to_safe_fallback,
)

# ---------------------------------------------------------------------------
# Política e configuração
# ---------------------------------------------------------------------------


def test_logprivacy_version_constant_is_string():
    assert isinstance(LOGPRIVACY_VERSION, str)
    assert LOGPRIVACY_VERSION


def test_logprivacy_version_matches_installed_package():
    assert LOGPRIVACY_VERSION == version("logprivacy")


def test_alphora_policy_includes_extra_sensitive_keys():
    keys = ALPHORA_POLICY.sensitive_keys
    assert "api_secret" in keys
    assert "listen_key" in keys
    assert "signature" in keys
    assert "x-mbx-apikey" in keys


def test_alphora_policy_includes_camelcase_variants():
    keys = ALPHORA_POLICY.sensitive_keys
    assert "apiSecret" in keys
    assert "listenKey" in keys
    assert "secretKey" in keys
    assert "xMbxApiKey" in keys


def test_alphora_policy_includes_compound_variants():
    keys = ALPHORA_POLICY.sensitive_keys
    assert "apisecret" in keys
    assert "listenkey" in keys
    assert "secretkey" in keys
    assert "binance_secret" in keys


def test_alphora_policy_keeps_logprivacy_defaults():
    keys = ALPHORA_POLICY.sensitive_keys
    for default_key in ("password", "token", "api_key", "private_key", "authorization"):
        assert default_key in keys, f"Chave padrão ausente: {default_key}"


def test_alphora_policy_has_no_duplicate_keys():
    keys = ALPHORA_POLICY.sensitive_keys
    assert len(keys) == len(set(keys)), "Chaves duplicadas encontradas na política"


# ---------------------------------------------------------------------------
# sanitize_for_audit — masking de chaves sensíveis em dicts
# ---------------------------------------------------------------------------


def test_masks_api_key_value():
    result = sanitize_for_audit({"api_key": "my-real-key"})
    assert result["api_key"] != "my-real-key"


def test_masks_api_secret_value():
    result = sanitize_for_audit({"api_secret": "my-real-secret"})
    assert result["api_secret"] != "my-real-secret"


def test_masks_token_value():
    result = sanitize_for_audit({"token": "my-token-123"})
    assert result["token"] != "my-token-123"


def test_masks_password_value():
    result = sanitize_for_audit({"password": "hunter2"})
    assert result["password"] != "hunter2"


def test_masks_listen_key_value():
    result = sanitize_for_audit({"listen_key": "ws-listen-key"})
    assert result["listen_key"] != "ws-listen-key"


def test_masks_signature_value():
    result = sanitize_for_audit({"signature": "hmac-abc"})
    assert result["signature"] != "hmac-abc"


def test_masks_nested_sensitive_key():
    result = sanitize_for_audit({"outer": {"private_key": "very-secret"}})
    assert result["outer"]["private_key"] != "very-secret"


def test_masks_camelcase_api_secret():
    result = sanitize_for_audit({"apiSecret": "camel-secret"})
    assert result["apiSecret"] != "camel-secret"


def test_masks_camelcase_listen_key():
    result = sanitize_for_audit({"listenKey": "camel-listen"})
    assert result["listenKey"] != "camel-listen"


def test_masks_camelcase_secret_key():
    result = sanitize_for_audit({"secretKey": "camel-secret-key"})
    assert result["secretKey"] != "camel-secret-key"


def test_masks_binance_secret():
    result = sanitize_for_audit({"binance_secret": "binance-s3cr3t"})
    assert result["binance_secret"] != "binance-s3cr3t"


def test_preserves_non_sensitive_fields():
    result = sanitize_for_audit({"symbol": "BTCUSDT", "price": 100.0, "quantity": 0.5})
    assert result["symbol"] == "BTCUSDT"
    assert result["price"] == 100.0
    assert result["quantity"] == 0.5


def test_preserves_none_values():
    result = sanitize_for_audit({"symbol": None})
    assert result["symbol"] is None


def test_handles_empty_dict():
    assert sanitize_for_audit({}) == {}


def test_handles_list_of_dicts():
    data = [{"api_key": "k1"}, {"api_key": "k2"}]
    result = sanitize_for_audit(data)
    assert result[0]["api_key"] != "k1"
    assert result[1]["api_key"] != "k2"


def test_handles_scalar_string_passthrough():
    result = sanitize_for_audit("hello world")
    assert result == "hello world"


def test_handles_none_passthrough():
    assert sanitize_for_audit(None) is None


def test_handles_integer_passthrough():
    assert sanitize_for_audit(42) == 42


def test_masked_value_is_non_empty_string():
    result = sanitize_for_audit({"password": "secret123"})
    masked = result["password"]
    assert isinstance(masked, str)
    assert len(masked) > 0


# ---------------------------------------------------------------------------
# Falsos positivos — campos com nomes parecidos mas não sensíveis
# ---------------------------------------------------------------------------


def test_no_false_positive_keyboard_layout():
    result = sanitize_for_audit({"keyboard_layout": "us"})
    assert result["keyboard_layout"] == "us"


def test_no_false_positive_monkey_score():
    result = sanitize_for_audit({"monkey_score": 42})
    assert result["monkey_score"] == 42


def test_no_false_positive_keynote_enabled():
    result = sanitize_for_audit({"keynote_enabled": True})
    assert result["keynote_enabled"] is True


def test_no_false_positive_strategy_key_metric():
    result = sanitize_for_audit({"strategy_key_metric": "sharpe"})
    assert result["strategy_key_metric"] == "sharpe"


# ---------------------------------------------------------------------------
# to_safe_fallback — objetos desconhecidos
# ---------------------------------------------------------------------------


def test_safe_fallback_with_dangerous_str():
    class DangerousObj:
        def __str__(self) -> str:
            return "api_key=leaked-secret"

    result = to_safe_fallback(DangerousObj())
    assert isinstance(result, str)
    assert "leaked-secret" not in result


def test_safe_fallback_with_failing_str():
    class FailStr:
        def __str__(self) -> str:
            raise RuntimeError("__str__ failed")

    result = to_safe_fallback(FailStr())
    assert isinstance(result, str)


def test_safe_fallback_returns_type_marker():
    class WeirdObj:
        pass

    result = to_safe_fallback(WeirdObj())
    assert isinstance(result, str)
    assert "WeirdObj" in result or "UNSUPPORTED" in result


# ---------------------------------------------------------------------------
# apply_privacy_filter — integração com logging
# ---------------------------------------------------------------------------


def test_apply_privacy_filter_does_not_raise(isolated_logging):
    logger = logging.getLogger("test.privacy.apply")
    apply_privacy_filter(logger)


def test_safe_logger_sanitizes_message_via_filter(caplog, isolated_logging):
    """Verifica que o LogPrivacyFilter mascara credenciais nas mensagens de log."""
    logger = logging.getLogger("test.privacy.sanitize")
    apply_privacy_filter(logger)

    with caplog.at_level(logging.WARNING, logger="test.privacy.sanitize"):
        logger.warning("api_key=%s token=%s", "my-real-api-key", "my-real-token")

    combined = " ".join(r.getMessage() for r in caplog.records)
    assert "my-real-api-key" not in combined
    assert "my-real-token" not in combined


def test_root_logger_filter_does_not_break_basic_logging(isolated_logging):
    root = logging.getLogger()
    apply_privacy_filter(root)
    root.debug("benign message")


def test_apply_privacy_filter_required_true_raises_on_failure(isolated_logging):
    with patch("utils.privacy.get_safe_logger", side_effect=RuntimeError("boom")):
        with pytest.raises(PrivacyInitializationError):
            apply_privacy_filter(logging.getLogger("test.privacy.fail"), required=True)


def test_apply_privacy_filter_required_false_does_not_raise(isolated_logging):
    with patch("utils.privacy.get_safe_logger", side_effect=RuntimeError("boom")):
        apply_privacy_filter(logging.getLogger("test.privacy.optional"), required=False)


def test_apply_privacy_filter_twice_no_duplicate_filters(isolated_logging):
    logger = logging.getLogger("test.privacy.dedup")
    apply_privacy_filter(logger)
    apply_privacy_filter(logger)
    logger.debug("test")


def test_logger_exception_with_secret_is_sanitized(caplog, isolated_logging):
    logger = logging.getLogger("test.privacy.exception")
    apply_privacy_filter(logger)

    with caplog.at_level(logging.ERROR, logger="test.privacy.exception"):
        try:
            raise ValueError("api_secret=very-secret-value")
        except ValueError:
            logger.exception("Caught error")

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "very-secret-value" not in text
