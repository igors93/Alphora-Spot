"""Error diagnostics registered by the owner of an error type reach every error event."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from observability import register_error_diagnostics, unregister_error_diagnostics
from observability.application.error_diagnostics import collect_error_diagnostics


class _OwnedError(Exception):
    pass


@pytest.fixture
def registered_key() -> Iterator[str]:
    key = "test_owner"
    yield key
    unregister_error_diagnostics(key)


def test_registered_extractor_result_is_collected_under_its_key(registered_key: str) -> None:
    register_error_diagnostics(
        registered_key,
        lambda error: {"kind": type(error).__name__} if isinstance(error, _OwnedError) else None,
    )

    assert collect_error_diagnostics(_OwnedError("boom")) == {
        registered_key: {"kind": "_OwnedError"}
    }
    assert registered_key not in collect_error_diagnostics(ValueError("other"))


def test_failing_extractor_is_skipped(registered_key: str) -> None:
    def broken(_error: BaseException) -> dict[str, str]:
        raise RuntimeError("extractor bug")

    register_error_diagnostics(registered_key, broken)

    assert registered_key not in collect_error_diagnostics(_OwnedError("boom"))


def test_unregistered_extractor_is_no_longer_called(registered_key: str) -> None:
    register_error_diagnostics(registered_key, lambda _error: {"seen": True})
    unregister_error_diagnostics(registered_key)

    assert registered_key not in collect_error_diagnostics(_OwnedError("boom"))


def test_snapshot_provider_registers_engine_error_diagnostics() -> None:
    from market_data.snapshot_engine import ResourceKey, ResourceResolutionError
    from market_data.snapshot_provider import wire_names

    error = ResourceResolutionError(ResourceKey("account", "summary"), ())

    collected = collect_error_diagnostics(error)

    assert wire_names.PAYLOAD_KEY in collected
    assert collected[wire_names.PAYLOAD_KEY]["error_type"]
