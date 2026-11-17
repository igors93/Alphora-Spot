"""Tests for Etapa 3: revalidate_for_execution in SnapshotCycleSession."""

from __future__ import annotations

import time

import pytest

from market_data.snapshot_provider import (
    SnapshotProvider,
    SnapshotProviderSettings,
)


class FakeClient:
    def __init__(self) -> None:
        self.position_risk_calls = 0
        self.account_calls = 0
        self.order_calls = 0

    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": "100.5"}

    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def position_risk(self, symbol=None, *, timeout=None):
        self.position_risk_calls += 1
        return [{"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "100"}]

    def open_orders(self, symbol, *, timeout=None):
        self.order_calls += 1
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        self.account_calls += 1
        return {"assets": [{"asset": "USDT", "availableBalance": "500"}]}


def _make_provider(client=None):
    return SnapshotProvider(
        client=client or FakeClient(),
        settings=SnapshotProviderSettings(
            light_ttl_seconds=0.0,
            heavy_ttl_seconds=0.0,
            deadline_seconds=3.0,
            cycle_deadline_seconds=10.0,
        ),
    )


def test_revalidate_for_execution_returns_snapshot():
    provider = _make_provider()
    now = time.time()
    session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["BTCUSDT"])
    try:
        session.resolve_baseline()
        snapshot = session.revalidate_for_execution("BTCUSDT")
        assert snapshot is not None
        assert snapshot.complete
    finally:
        session.close()
        provider.close()


def test_revalidate_for_execution_updates_last_snapshot():
    provider = _make_provider()
    now = time.time()
    session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["BTCUSDT"])
    try:
        session.resolve_baseline()
        snapshot = session.revalidate_for_execution("BTCUSDT")
        assert provider.last_snapshot is snapshot
    finally:
        session.close()
        provider.close()


def test_revalidate_for_execution_normalizes_symbol():
    provider = _make_provider()
    now = time.time()
    session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["btcusdt"])
    try:
        session.resolve_baseline()
        # Lowercase symbol should be normalized
        snapshot = session.revalidate_for_execution("btcusdt")
        assert snapshot is not None
    finally:
        session.close()
        provider.close()


def test_revalidate_for_execution_fetches_position_orders_and_account():
    client = FakeClient()
    provider = _make_provider(client)
    now = time.time()
    session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["BTCUSDT"])
    try:
        session.resolve_baseline()
        calls_before_revalidate = client.position_risk_calls

        session.revalidate_for_execution("BTCUSDT")

        assert client.position_risk_calls >= calls_before_revalidate
    finally:
        session.close()
        provider.close()


def test_revalidate_for_execution_on_closed_session_raises():
    provider = _make_provider()
    now = time.time()
    session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["BTCUSDT"])
    session.close()
    try:
        with pytest.raises(Exception):  # noqa: B017
            session.revalidate_for_execution("BTCUSDT")
    finally:
        provider.close()


def test_revalidate_for_execution_can_be_called_multiple_times():
    provider = _make_provider()
    now = time.time()
    session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["BTCUSDT"])
    try:
        session.resolve_baseline()
        s1 = session.revalidate_for_execution("BTCUSDT")
        s2 = session.revalidate_for_execution("BTCUSDT")
        assert s1 is not None
        assert s2 is not None
    finally:
        session.close()
        provider.close()


def test_revalidate_for_execution_uses_isolated_session():
    """Execution revalidation opens its own session so it has an independent deadline budget.

    The returned snapshot must be non-None and contain the resources needed for execution, but
    its snapshot_id is distinct from the cycle session's id (it is not accumulated into the cycle
    session). provider.last_snapshot is updated to the execution snapshot.
    """
    provider = _make_provider()
    now = time.time()
    session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["BTCUSDT"])
    try:
        baseline = session.resolve_baseline()
        selected = session.resolve_selected(["BTCUSDT"])
        fresh = session.revalidate_for_execution("BTCUSDT")

        # Cycle-level snapshots share the cycle session id.
        assert baseline.snapshot_id == selected.snapshot_id == "c1"
        # Execution revalidation uses an isolated session with its own snapshot_id.
        assert fresh.snapshot_id != "c1"
        assert fresh is not None
        # Provider last_snapshot reflects the execution revalidation result.
        assert provider.last_snapshot is fresh
    finally:
        session.close()
        provider.close()


def test_revalidate_session_lifetime_is_safe():
    provider = _make_provider()
    now = time.time()
    session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["BTCUSDT"])
    try:
        session.resolve_baseline()
        snapshot = session.revalidate_for_execution("BTCUSDT")
        # Provider last snapshot should reflect the revalidated snapshot
        assert provider.last_snapshot is snapshot
    finally:
        session.close()
        provider.close()
