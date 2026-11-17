"""Backward compatibility tests — ensures old APIs still work."""

from __future__ import annotations


def test_audit_logger_get_run_id():
    """audit_logger.get_run_id() still returns a string."""
    from utils.audit_logger import get_run_id

    run_id = get_run_id()
    assert isinstance(run_id, str)
    assert len(run_id) > 0


def test_audit_logger_reset_for_testing(tmp_path):
    """audit_logger.reset_for_testing() resets state."""
    from utils.audit_logger import get_run_id, reset_for_testing

    old_id = get_run_id()
    reset_for_testing(tmp_path)
    new_id = get_run_id()
    assert new_id != old_id


def test_audit_logger_audit_event(tmp_path):
    """audit_event() works with isolated tmp dir."""
    from utils.audit_logger import audit_event, reset_for_testing

    reset_for_testing(tmp_path)
    result = audit_event("system", "test_event", key="value")
    assert result is None or isinstance(result, dict)


def test_privacy_sanitize_for_audit():
    """privacy.sanitize_for_audit() still works."""
    from utils.privacy import sanitize_for_audit

    result = sanitize_for_audit({"api_key": "secret"})
    assert isinstance(result, dict)
    assert "secret" not in str(result)


def test_privacy_to_safe_fallback():
    """privacy.to_safe_fallback() still works."""
    from utils.privacy import to_safe_fallback

    class WeirdObj:
        pass

    result = to_safe_fallback(WeirdObj())
    # Should not raise, should return some safe representation
    assert result is not None


def test_privacy_apply_privacy_filter():
    """privacy.apply_privacy_filter() still works."""
    import logging

    from utils.privacy import apply_privacy_filter

    logger = logging.getLogger("test_compat_privacy")
    # Should not raise
    apply_privacy_filter(logger, required=False)


def test_privacy_initialization_error_exists():
    """PrivacyInitializationError is importable from utils.privacy."""
    from utils.privacy import PrivacyInitializationError

    assert issubclass(PrivacyInitializationError, RuntimeError)


def test_privacy_alphora_policy_exported():
    """ALPHORA_POLICY is accessible from utils.privacy."""
    from utils.privacy import ALPHORA_POLICY

    assert ALPHORA_POLICY is not None


def test_logger_configure_logging():
    """utils.logger.configure_logging() works."""
    from utils.logger import configure_logging

    configure_logging()  # idempotent


def test_logger_get_logger():
    """utils.logger.get_logger() returns a logger."""
    import logging

    from utils.logger import get_logger

    logger = get_logger("test_compat_logger")
    assert isinstance(logger, logging.Logger)


def test_trade_journal_append(tmp_path):
    """TradeJournal.append() writes and mirrors events."""
    from utils.audit_logger import reset_for_testing
    from utils.trade_journal import TradeJournal

    reset_for_testing(tmp_path)
    journal_path = tmp_path / "test_journal.jsonl"
    journal = TradeJournal(str(journal_path))
    journal.append("test_trade_event", symbol="BTCUSDT", qty=0.01)
    assert journal_path.exists()
    content = journal_path.read_text(encoding="utf-8")
    assert "test_trade_event" in content
