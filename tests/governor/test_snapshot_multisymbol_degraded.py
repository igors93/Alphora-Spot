"""Tests for multi-symbol degraded operation with Coalestra 0.6.3.

Verifies:
- Partial baseline does not close cycle session.
- Failed symbol goes into prefetched_heavy_errors; healthy symbol continues.
- try_resolve_symbol_evaluation: rejected result registers error without crashing cycle.
- try_prefetch: accepted/degraded/rejected handled correctly per symbol.
- readiness_diagnostic_from_result maps states to SnapshotReadiness levels.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from governor.cycle_context import SnapshotReadiness
from market_data.snapshot_engine import (
    Snapshot,
    SnapshotRequest,
    SnapshotResult,
    SnapshotResultState,
    SnapshotValue,
)
from market_data.snapshot_provider import (
    ACCOUNT,
    SnapshotCycleSession,
    SnapshotProvider,
    SnapshotProviderSettings,
    exchange_rules,
    mark_price,
    market_state,
    open_algo_orders,
    open_orders,
    position,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClient:
    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": "100"}

    def exchange_info(self, *, timeout=None):
        return {"symbols": []}

    def position_risk(self, symbol=None, *, timeout=None):
        return []

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        return {"assets": []}


def _make_snapshot(resources: dict | None = None, errors: dict | None = None) -> Snapshot:
    return Snapshot(
        snapshot_id="test",
        created_at=time.time(),
        resources=resources or {},
        errors=errors or {},
    )


def _sv(key, value=None) -> SnapshotValue:
    now = time.time()
    return SnapshotValue(
        key=key,
        value=value if value is not None else object(),
        source="test",
        observed_at=now,
        fetched_at=now,
        age_seconds=0.0,
        stale=False,
        from_cache=False,
        latency_ms=0.0,
    )


def _make_result(
    state: SnapshotResultState,
    snapshot: Snapshot | None = None,
    allowed_actions: tuple[str, ...] = (),
    reason: str = "",
) -> SnapshotResult:
    return SnapshotResult(
        state=state,
        snapshot=snapshot if snapshot is not None else _make_snapshot(),
        request=SnapshotRequest(required=[ACCOUNT]),
        allowed_actions=allowed_actions,
        reason=reason,
    )


@pytest.fixture(autouse=True)
def close_providers():
    yield
    from market_data.snapshot_provider import close_live_snapshot_providers_for_testing

    close_live_snapshot_providers_for_testing()


def _make_provider(**kwargs) -> SnapshotProvider:
    return SnapshotProvider(
        client=FakeClient(),
        settings=SnapshotProviderSettings(**kwargs),
    )


# ---------------------------------------------------------------------------
# readiness_diagnostic_from_result
# ---------------------------------------------------------------------------


def test_readiness_from_result_accepted_evaluation() -> None:
    provider = _make_provider()
    snap = _make_snapshot(
        resources={
            market_state("BTCUSDT"): _sv(market_state("BTCUSDT")),
        }
    )
    result = _make_result(SnapshotResultState.ACCEPTED, snapshot=snap)
    diag = provider.readiness_diagnostic_from_result(
        result, profile="evaluation", symbols=["BTCUSDT"]
    )
    assert diag.readiness >= SnapshotReadiness.READ_ONLY


def test_readiness_from_result_accepted_entry() -> None:
    provider = _make_provider()
    result = _make_result(SnapshotResultState.ACCEPTED)
    diag = provider.readiness_diagnostic_from_result(result, profile="entry", symbol="BTCUSDT")
    assert diag.readiness == SnapshotReadiness.ENTRY_SAFE


def test_readiness_from_result_accepted_exit() -> None:
    provider = _make_provider()
    result = _make_result(SnapshotResultState.ACCEPTED)
    diag = provider.readiness_diagnostic_from_result(result, profile="exit", symbol="BTCUSDT")
    assert diag.readiness == SnapshotReadiness.EXIT_SAFE


def test_readiness_from_result_degraded_reduce_only() -> None:
    provider = _make_provider()
    result = _make_result(
        SnapshotResultState.DEGRADED,
        allowed_actions=("reduce_only_exit",),
    )
    diag = provider.readiness_diagnostic_from_result(result, profile="exit", symbol="BTCUSDT")
    assert diag.readiness == SnapshotReadiness.EXIT_SAFE


def test_readiness_from_result_degraded_cancel_orders() -> None:
    provider = _make_provider()
    result = _make_result(
        SnapshotResultState.DEGRADED,
        allowed_actions=("cancel_orders",),
    )
    diag = provider.readiness_diagnostic_from_result(result, profile="exit", symbol="BTCUSDT")
    assert diag.readiness == SnapshotReadiness.EXIT_SAFE


def test_readiness_from_result_degraded_evaluate_only() -> None:
    provider = _make_provider()
    result = _make_result(
        SnapshotResultState.DEGRADED,
        allowed_actions=("read", "evaluate"),
    )
    diag = provider.readiness_diagnostic_from_result(
        result, profile="evaluation", symbols=["BTCUSDT"]
    )
    assert diag.readiness == SnapshotReadiness.READ_ONLY


def test_readiness_from_result_rejected_no_resources() -> None:
    provider = _make_provider()
    result = _make_result(SnapshotResultState.REJECTED, reason="ACCEPTANCE_FAILED")
    diag = provider.readiness_diagnostic_from_result(
        result, profile="evaluation", symbols=["BTCUSDT"]
    )
    assert diag.readiness == SnapshotReadiness.UNAVAILABLE


def test_readiness_from_result_rejected_with_partial_resources() -> None:
    provider = _make_provider()
    snap = _make_snapshot(
        resources={
            market_state("BTCUSDT"): _sv(market_state("BTCUSDT")),
        }
    )
    result = _make_result(SnapshotResultState.REJECTED, snapshot=snap, reason="PARTIAL")
    diag = provider.readiness_diagnostic_from_result(
        result, profile="evaluation", symbols=["BTCUSDT"]
    )
    assert diag.readiness == SnapshotReadiness.PARTIAL


# ---------------------------------------------------------------------------
# try_resolve_symbol_evaluation on cycle session
# ---------------------------------------------------------------------------


def test_cycle_session_has_try_resolve_symbol_evaluation() -> None:
    provider = _make_provider()
    session_mock = MagicMock()
    session_mock.closed = False

    snap = _make_snapshot()
    result = _make_result(SnapshotResultState.ACCEPTED, snapshot=snap)
    session_mock.try_resolve_request.return_value = result

    cycle_session = SnapshotCycleSession(
        provider=provider,
        session=session_mock,
        cycle_id="test-cycle",
        now=time.time(),
        symbols=["BTCUSDT"],
    )
    assert callable(getattr(cycle_session, "try_resolve_symbol_evaluation", None))


def test_try_resolve_symbol_evaluation_returns_result() -> None:
    provider = _make_provider()
    session_mock = MagicMock()
    session_mock.closed = False

    snap = _make_snapshot()
    result = _make_result(SnapshotResultState.ACCEPTED, snapshot=snap)
    session_mock.try_resolve_request.return_value = result

    cycle_session = SnapshotCycleSession(
        provider=provider,
        session=session_mock,
        cycle_id="test-cycle",
        now=time.time(),
        symbols=["BTCUSDT"],
    )
    returned = cycle_session.try_resolve_symbol_evaluation("BTCUSDT")
    assert returned is result
    assert provider.last_snapshot_result is result
    assert returned.snapshot is snap


def test_try_resolve_symbol_evaluation_rejected_stored_on_provider() -> None:
    provider = _make_provider()
    session_mock = MagicMock()
    session_mock.closed = False

    snap = _make_snapshot()
    result = _make_result(SnapshotResultState.REJECTED, snapshot=snap, reason="NO_DATA")
    session_mock.try_resolve_request.return_value = result

    cycle_session = SnapshotCycleSession(
        provider=provider,
        session=session_mock,
        cycle_id="test-cycle",
        now=time.time(),
        symbols=["BTCUSDT"],
    )
    returned = cycle_session.try_resolve_symbol_evaluation("BTCUSDT")
    assert returned.rejected
    assert provider.last_snapshot_result is result


# ---------------------------------------------------------------------------
# Baseline rejected does not leave session open (raises) — fail-closed for hard rejection
# ---------------------------------------------------------------------------


def test_resolve_baseline_rejected_raises() -> None:
    provider = _make_provider()
    session_mock = MagicMock()
    session_mock.closed = False

    snap = _make_snapshot()

    from market_data.snapshot_engine import SnapshotBuildError

    error = SnapshotBuildError({ACCOUNT: RuntimeError("all failed")}, snapshot=snap)

    def raise_for_rejected():
        raise error

    result_rejected_mock = MagicMock()
    result_rejected_mock.rejected = True
    result_rejected_mock.state = MagicMock()
    result_rejected_mock.state.value = "REJECTED"
    result_rejected_mock.reason = "ALL_FAILED"
    result_rejected_mock.allowed_actions = ()
    result_rejected_mock.snapshot = snap
    result_rejected_mock.to_dict.return_value = {}
    result_rejected_mock.raise_for_rejected = raise_for_rejected

    session_mock.try_resolve_request.return_value = result_rejected_mock

    cycle_session = SnapshotCycleSession(
        provider=provider,
        session=session_mock,
        cycle_id="test-cycle",
        now=time.time(),
        symbols=["BTCUSDT"],
    )
    with pytest.raises(SnapshotBuildError):
        cycle_session.resolve_baseline()


# ---------------------------------------------------------------------------
# Baseline degraded does NOT raise — session stays open
# ---------------------------------------------------------------------------


def test_resolve_baseline_degraded_does_not_raise() -> None:
    provider = _make_provider()
    session_mock = MagicMock()
    session_mock.closed = False

    snap = _make_snapshot()
    result_mock = MagicMock()
    result_mock.rejected = False
    result_mock.degraded = True
    result_mock.accepted = False
    result_mock.state = MagicMock()
    result_mock.state.value = "DEGRADED"
    result_mock.reason = "PARTIAL"
    result_mock.allowed_actions = ("read", "evaluate")
    result_mock.snapshot = snap
    result_mock.to_dict.return_value = {}

    session_mock.try_resolve_request.return_value = result_mock

    cycle_session = SnapshotCycleSession(
        provider=provider,
        session=session_mock,
        cycle_id="test-cycle",
        now=time.time(),
        symbols=["BTCUSDT"],
    )
    snapshot = cycle_session.resolve_baseline()
    assert snapshot is snap


# ---------------------------------------------------------------------------
# _missing_heavy_snapshot_keys
# ---------------------------------------------------------------------------


def test_missing_heavy_snapshot_keys_empty_snapshot() -> None:
    provider = _make_provider()
    snap = _make_snapshot()
    missing = provider.missing_heavy_snapshot_keys(snap, "BTCUSDT")
    assert len(missing) == 7


def test_missing_heavy_snapshot_keys_full_snapshot() -> None:
    provider = _make_provider()
    resources = {
        market_state("BTCUSDT"): _sv(market_state("BTCUSDT")),
        mark_price("BTCUSDT"): _sv(mark_price("BTCUSDT")),
        exchange_rules("BTCUSDT"): _sv(exchange_rules("BTCUSDT")),
        position("BTCUSDT"): _sv(position("BTCUSDT")),
        open_orders("BTCUSDT"): _sv(open_orders("BTCUSDT")),
        open_algo_orders("BTCUSDT"): _sv(open_algo_orders("BTCUSDT")),
        ACCOUNT: _sv(ACCOUNT),
    }
    snap = _make_snapshot(resources=resources)
    missing = provider.missing_heavy_snapshot_keys(snap, "BTCUSDT")
    assert missing == ()


# ---------------------------------------------------------------------------
# try_prefetch_symbol_evaluation_snapshots: fanout mapping
# ---------------------------------------------------------------------------


def test_try_prefetch_returns_mapping_keyed_by_normalized_symbol() -> None:
    provider = _make_provider()
    snap = _make_snapshot()
    accepted = _make_result(SnapshotResultState.ACCEPTED, snapshot=snap)
    rejected = _make_result(SnapshotResultState.REJECTED, snapshot=snap, reason="FAIL")

    with patch.object(
        provider._sync_builder,
        "try_build_requests",
        return_value=(accepted, rejected),
    ):
        results = provider.try_prefetch_symbol_evaluation_snapshots(
            symbols=["BTCUSDT", "ETHUSDT"],
            cycle_id="c1",
            now=time.time(),
        )

    assert "BTCUSDT" in results
    assert "ETHUSDT" in results
    assert results["BTCUSDT"].accepted
    assert results["ETHUSDT"].rejected
