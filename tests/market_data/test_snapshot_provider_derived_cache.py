"""Tests for Etapa 6: derived resource cache — no manual invalidation of exchange rules."""

from __future__ import annotations

import time

from market_data.snapshot_provider import (
    EXCHANGE_INFO,
    SnapshotProvider,
    SnapshotProviderSettings,
)


class FakeClient:
    def __init__(self) -> None:
        self.exchange_info_calls = 0
        self.position_risk_calls = 0

    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": "100.5"}

    def exchange_info(self, *, timeout=None):
        self.exchange_info_calls += 1
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
        return [{"symbol": "BTCUSDT", "positionAmt": "0"}]

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        return {"assets": [{"asset": "USDT", "availableBalance": "100"}]}


def test_exchange_rules_are_not_refetched_on_every_resolve_selected():
    client = FakeClient()
    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(
            exchange_info_ttl_seconds=300.0,
            deadline_seconds=3.0,
            cycle_deadline_seconds=10.0,
        ),
    )
    now = time.time()
    try:
        session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["BTCUSDT"])
        session.resolve_baseline()
        session.resolve_selected(["BTCUSDT"])
        calls_after_first = client.exchange_info_calls

        # Second resolve_selected should NOT refetch exchangeInfo (derived cache)
        session.resolve_selected(["BTCUSDT"])
        assert client.exchange_info_calls == calls_after_first, (
            "exchange_info should NOT be refetched; derived cache should reuse existing value"
        )
        session.close()
    finally:
        provider.close()


def test_exchange_rules_cached_after_heavy_snapshot():
    client = FakeClient()
    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(
            exchange_info_ttl_seconds=300.0,
            deadline_seconds=3.0,
            cycle_deadline_seconds=10.0,
        ),
    )
    now = time.time()
    try:
        provider.get_heavy_snapshot("BTCUSDT", now=now)
        calls_after_first = client.exchange_info_calls

        provider.get_heavy_snapshot("BTCUSDT", now=now)
        assert client.exchange_info_calls == calls_after_first, (
            "exchange_info should be served from cache on second heavy snapshot"
        )
    finally:
        provider.close()


def test_invalidate_exchange_rules_method_does_not_exist():
    provider = SnapshotProvider(client=FakeClient())
    try:
        assert not hasattr(provider, "invalidate_exchange_rules"), (
            "invalidate_exchange_rules should have been removed (Etapa 6)"
        )
    finally:
        provider.close()


def test_exchange_rules_derived_from_exchange_info_dependency():
    client = FakeClient()
    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(
            exchange_info_ttl_seconds=300.0,
            deadline_seconds=3.0,
            cycle_deadline_seconds=10.0,
        ),
    )
    now = time.time()
    try:
        heavy = provider.get_heavy_snapshot("BTCUSDT", now=now)
        assert heavy.exchange_rules is not None, (
            "exchange_rules should be derived from EXCHANGE_INFO"
        )
    finally:
        provider.close()


def test_exchange_info_invalidation_triggers_rules_refresh():
    client = FakeClient()
    provider = SnapshotProvider(
        client=client,
        settings=SnapshotProviderSettings(
            exchange_info_ttl_seconds=300.0,
            deadline_seconds=3.0,
            cycle_deadline_seconds=10.0,
        ),
    )
    now = time.time()
    try:
        provider.get_heavy_snapshot("BTCUSDT", now=now)
        calls_before = client.exchange_info_calls

        # Explicitly invalidate EXCHANGE_INFO (the correct way to force a rules refresh)
        provider.publisher.invalidate(EXCHANGE_INFO, reason="test-invalidation")
        provider.get_heavy_snapshot("BTCUSDT", now=now)

        assert client.exchange_info_calls > calls_before, (
            "After explicit EXCHANGE_INFO invalidation, exchange_info should be re-fetched"
        )
    finally:
        provider.close()


def test_resolve_selected_does_not_call_invalidate_exchange_rules(monkeypatch):
    class TrackingClient(FakeClient):
        pass

    client = TrackingClient()
    provider = SnapshotProvider(client=client)
    now = time.time()
    try:
        session = provider.open_cycle_session(cycle_id="c1", now=now, symbols=["BTCUSDT"])
        session.resolve_baseline()

        # Monkey-patch publisher.invalidate_many to track calls
        original = provider.publisher.invalidate_many
        calls = []

        def tracking_invalidate_many(keys, *, reason=""):
            calls.append((list(keys), reason))
            return original(keys, reason=reason)

        provider.publisher.invalidate_many = tracking_invalidate_many

        session.resolve_selected(["BTCUSDT"])

        # None of the invalidations should be exchange.rules keys
        exchange_rule_keys = [
            k for call_keys, _ in calls for k in call_keys if k.namespace == "exchange"
        ]
        assert exchange_rule_keys == [], (
            f"resolve_selected must not invalidate exchange.rules; got: {exchange_rule_keys}"
        )
        session.close()
    finally:
        provider.close()
