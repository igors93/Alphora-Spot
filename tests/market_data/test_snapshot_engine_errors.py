"""Tests for snapshot engine structured error extraction."""

from __future__ import annotations

import pytest

from market_data.snapshot_provider.engine_errors import extract_engine_error_diagnostics


class FakeEngineError(Exception):
    """Minimal engine-like error with to_dict()."""

    def __init__(self, resource: str, source: str, message: str, attempts: int = 1):
        self.resource = resource
        self.source = source
        self._message = message
        self.attempts = attempts
        super().__init__(message)

    def to_dict(self):
        return {
            "resource": self.resource,
            "source": self.source,
            "error_type": type(self).__name__,
            "message": self._message,
            "attempts": self.attempts,
        }


class FakeErrorNoToDict(Exception):
    """Error without to_dict but with key/failures attributes."""

    def __init__(self, key: str):
        self.key = key
        self.failures = []
        super().__init__(f"cannot resolve {key}")


class FakeSnapshotBuildError(Exception):
    """Minimal SnapshotBuildError-like object."""

    def __init__(self, errors: dict, snapshot=None):
        self.errors = errors
        self.snapshot = snapshot
        super().__init__("Snapshot build failed")


def test_to_dict_preserved_when_available():
    """to_dict() result is used when available."""
    err = FakeEngineError("MyResource", "primary_source", "timeout", attempts=3)
    result = extract_engine_error_diagnostics(err)
    assert result is not None
    assert result["resource"] == "MyResource"
    assert result["source"] == "primary_source"
    assert result["attempts"] == 3


def test_error_type_extracted():
    """error_type field is set."""
    err = FakeEngineError("R", "S", "msg")
    result = extract_engine_error_diagnostics(err)
    assert result is not None
    assert result.get("error_type") == "FakeEngineError"


def test_message_extracted():
    """message field is extracted and present."""
    err = FakeEngineError("R", "S", "my error message")
    result = extract_engine_error_diagnostics(err)
    assert result is not None
    assert "my error message" in result["message"]


def test_attempts_extracted():
    """attempts field is extracted."""
    err = FakeEngineError("R", "S", "msg", attempts=5)
    result = extract_engine_error_diagnostics(err)
    assert result is not None
    assert result["attempts"] == 5


def test_partial_snapshot_detected():
    """partial_snapshot_available is set when snapshot is present."""
    err = FakeSnapshotBuildError(
        errors={"key1": Exception("fail")},
        snapshot={"partial": True},
    )
    result = extract_engine_error_diagnostics(err)
    assert result is not None
    assert result.get("partial_snapshot_available") is True


def test_partial_snapshot_false_when_none():
    """partial_snapshot_available is False when snapshot is None."""
    err = FakeSnapshotBuildError(
        errors={"key1": Exception("fail")},
        snapshot=None,
    )
    result = extract_engine_error_diagnostics(err)
    assert result is not None
    assert result.get("partial_snapshot_available") is False


def test_message_length_limited():
    """Message is truncated to max length."""
    long_message = "x" * 5_000
    err = FakeEngineError("R", "S", long_message)
    result = extract_engine_error_diagnostics(err)
    assert result is not None
    assert len(result["message"]) <= 2_000


def test_fallback_when_no_to_dict():
    """Falls back to attribute inspection when to_dict is absent."""
    err = FakeErrorNoToDict("my_resource_key")
    result = extract_engine_error_diagnostics(err)
    assert result is not None
    assert "my_resource_key" in result.get("resource", "")


def test_none_returned_for_plain_exception():
    """Returns None for plain exceptions with no engine error structure."""
    err = ValueError("plain error")
    result = extract_engine_error_diagnostics(err)
    assert result is None


def test_real_engine_snapshot_build_error():
    """Tests extraction with the real engine SnapshotBuildError."""
    try:
        from market_data.snapshot_engine.core.errors import SnapshotBuildError
        from market_data.snapshot_engine.core.keys import ResourceKey
    except ImportError:
        pytest.skip("snapshot engine not available")

    key = ResourceKey("alphora", "test_resource")
    err = SnapshotBuildError({key: Exception("test failure")}, snapshot=None)
    result = extract_engine_error_diagnostics(err)
    assert result is not None


def test_real_engine_resource_resolution_error():
    """Tests extraction with the real engine ResourceResolutionError."""
    try:
        from market_data.snapshot_engine.core.errors import ResourceResolutionError
        from market_data.snapshot_engine.core.keys import ResourceKey
    except ImportError:
        pytest.skip("snapshot engine not available")

    key = ResourceKey("alphora", "exchange_info")
    err = ResourceResolutionError(key, failures=())
    result = extract_engine_error_diagnostics(err)
    assert result is not None
    assert "exchange_info" in str(result.get("resource", ""))
