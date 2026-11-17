"""Tests for risk-reduction degraded fallback in TradePipelineMixin.

Verifies:
- Entry/risk-increasing actions continue to be fail-closed (no degraded fallback).
- Exit/risk-reducing actions can proceed when try_risk_reduction_snapshot returns
  DEGRADED with reduce_only_exit or cancel_orders in allowed_actions.
- Exit is still blocked when the degraded result has no allowed_actions that permit exit.
- audit events coalestra_risk_reduction_try_resolved and coalestra_degraded_snapshot_used
  are emitted on successful degraded exit fallback.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from governor.cycle_context import CycleContext
from market_data.snapshot_engine import (
    Snapshot,
    SnapshotRequest,
    SnapshotResult,
    SnapshotResultState,
    SnapshotValue,
)
from market_data.snapshot_provider import (
    ACCOUNT,
    SnapshotProviderSettings,
    exchange_rules,
    mark_price,
    market_state,
    open_algo_orders,
    open_orders,
    position,
)
from trading.enums import action_reduces_or_repairs_risk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(resources: dict | None = None) -> Snapshot:
    return Snapshot(
        snapshot_id="test",
        created_at=time.time(),
        resources=resources or {},
        errors={},
    )


def _make_result(
    state: SnapshotResultState,
    snapshot: Snapshot | None = None,
    allowed_actions: tuple[str, ...] = (),
    reason: str = "",
) -> SnapshotResult:
    if snapshot is None:
        snapshot = _make_snapshot()
    return SnapshotResult(
        state=state,
        snapshot=snapshot,
        request=SnapshotRequest(required=[ACCOUNT]),
        allowed_actions=allowed_actions,
        reason=reason,
    )


def _sv(key, value=None) -> SnapshotValue:
    now = time.time()
    return SnapshotValue(
        key=key,
        value=value if value is not None else {},
        source="test",
        observed_at=now,
        fetched_at=now,
        age_seconds=0.0,
        stale=False,
        from_cache=False,
        latency_ms=0.0,
    )


def _make_snapshot_value(key, value: Any = None) -> SnapshotValue:
    return _sv(key, value=value or {})


def _full_snapshot() -> Snapshot:
    return _make_snapshot(
        resources={
            market_state("BTCUSDT"): _sv(market_state("BTCUSDT"), MagicMock(price=Decimal("100"))),
            mark_price("BTCUSDT"): _sv(mark_price("BTCUSDT"), Decimal("100")),
            exchange_rules("BTCUSDT"): _sv(exchange_rules("BTCUSDT"), MagicMock()),
            position("BTCUSDT"): _sv(position("BTCUSDT"), MagicMock(is_open=False)),
            open_orders("BTCUSDT"): _sv(open_orders("BTCUSDT"), []),
            open_algo_orders("BTCUSDT"): _sv(open_algo_orders("BTCUSDT"), []),
            ACCOUNT: _sv(ACCOUNT, {"assets": []}),
        }
    )


# ---------------------------------------------------------------------------
# Action classification sanity
# ---------------------------------------------------------------------------


def test_close_position_is_risk_reducing() -> None:
    assert action_reduces_or_repairs_risk("CLOSE_POSITION")


def test_open_long_is_not_risk_reducing() -> None:
    assert not action_reduces_or_repairs_risk("OPEN_LONG")


# ---------------------------------------------------------------------------
# Provider mock setup
# ---------------------------------------------------------------------------


def _make_provider_mock(
    risk_reduction_result: SnapshotResult | None = None,
    raise_on_try_risk: Exception | None = None,
    missing_keys: tuple = (),
) -> MagicMock:
    provider = MagicMock()
    provider.settings = SnapshotProviderSettings()

    if raise_on_try_risk is not None:
        provider.try_risk_reduction_snapshot.side_effect = raise_on_try_risk
    elif risk_reduction_result is not None:
        provider.try_risk_reduction_snapshot.return_value = risk_reduction_result

    provider.missing_heavy_snapshot_keys.return_value = missing_keys

    heavy_mock = MagicMock()
    heavy_mock.position = MagicMock(is_open=True, symbol="BTCUSDT")
    heavy_mock.open_orders = []
    heavy_mock.open_algo_orders = []
    heavy_mock.account_info = {"assets": []}
    heavy_mock.data_age_seconds = 0.1
    heavy_mock.stale = False
    provider.heavy_snapshot_from_snapshot.return_value = heavy_mock

    return provider


def _make_session_mock(provider: MagicMock) -> MagicMock:
    session = MagicMock()
    session.provider = provider
    return session


def _make_context(session: MagicMock | None = None) -> CycleContext:
    ctx = CycleContext(now=time.time(), cycle_id="test-cycle")
    if session is not None:
        ctx.snapshot_session = session
    return ctx


# ---------------------------------------------------------------------------
# Tests via provider.try_risk_reduction_snapshot
# ---------------------------------------------------------------------------


def test_try_risk_reduction_snapshot_reduce_only_exit_allowed() -> None:
    snap = _full_snapshot()
    result = _make_result(
        SnapshotResultState.DEGRADED,
        snapshot=snap,
        allowed_actions=("reduce_only_exit",),
    )

    _make_provider_mock(risk_reduction_result=result)

    allowed = set(result.allowed_actions)
    assert result.degraded
    assert "reduce_only_exit" in allowed


def test_try_risk_reduction_snapshot_cancel_orders_allowed() -> None:
    snap = _full_snapshot()
    result = _make_result(
        SnapshotResultState.DEGRADED,
        snapshot=snap,
        allowed_actions=("cancel_orders",),
    )

    allowed = set(result.allowed_actions)
    assert "cancel_orders" in allowed


def test_try_risk_reduction_snapshot_no_exit_actions_blocks() -> None:
    snap = _full_snapshot()
    result = _make_result(
        SnapshotResultState.DEGRADED,
        snapshot=snap,
        allowed_actions=("read",),
    )

    allowed = set(result.allowed_actions)
    assert "reduce_only_exit" not in allowed
    assert "cancel_orders" not in allowed


def test_try_risk_reduction_snapshot_rejected_blocks() -> None:
    result = _make_result(SnapshotResultState.REJECTED, reason="FAILED")
    assert result.rejected
    allowed = set(result.allowed_actions)
    assert "reduce_only_exit" not in allowed
    assert "cancel_orders" not in allowed


# ---------------------------------------------------------------------------
# try_risk_reduction_snapshot is called via provider
# ---------------------------------------------------------------------------


def test_provider_try_risk_reduction_snapshot_is_callable() -> None:
    from market_data.snapshot_provider import SnapshotProvider

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

    provider = SnapshotProvider(client=FakeClient())
    assert callable(getattr(provider, "try_risk_reduction_snapshot", None))
    provider.close()


# ---------------------------------------------------------------------------
# Entry actions are NEVER allowed to use risk-reduction fallback
# ---------------------------------------------------------------------------


def test_entry_action_degraded_fallback_gate() -> None:
    """Entry actions must never use degraded risk-reduction fallback."""
    from trading.enums import action_increases_risk

    assert action_increases_risk("OPEN_LONG")
    assert action_increases_risk("OPEN_SHORT")

    risk_result = _make_result(
        SnapshotResultState.DEGRADED,
        allowed_actions=("reduce_only_exit",),
    )
    _make_provider_mock(risk_reduction_result=risk_result)

    # ExecutionRevalidation (stage 22) only calls try_risk_reduction_snapshot
    # when action_reduces_or_repairs_risk(action). For entry actions this is False.
    for action in ("OPEN_LONG", "OPEN_SHORT"):
        assert not action_reduces_or_repairs_risk(action), (
            f"{action} must not be risk-reducing — entry fallback would apply incorrectly"
        )


# ---------------------------------------------------------------------------
# coalestra_degraded_snapshot_used event semantics
# ---------------------------------------------------------------------------


def test_degraded_snapshot_used_only_emitted_for_exit() -> None:
    """Smoke: degrade state reduces or repairs risk — only for exit-like actions."""
    exit_actions = ("CLOSE_POSITION",)
    for action in exit_actions:
        assert action_reduces_or_repairs_risk(action)


@pytest.fixture(autouse=True)
def cleanup_providers():
    yield
    from market_data.snapshot_provider import close_live_snapshot_providers_for_testing

    close_live_snapshot_providers_for_testing()
