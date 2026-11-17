"""Tests: exchange_rules age must not drive heavy snapshot staleness.

Critério obrigatório (problema #1):
    exchange_rules com 127 segundos de idade
    + TTL válido de seis horas
    + dados operacionais atuais
    → heavy_snapshot.stale == False
"""

from __future__ import annotations

import time
from decimal import Decimal

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection
from domain.symbol_rules import SymbolRules
from market_data.snapshot_engine import Snapshot
from market_data.snapshot_engine.core.models import SnapshotValue
from market_data.snapshot_provider import (
    ACCOUNT,
    SnapshotProvider,
    SnapshotProviderSettings,
    exchange_rules,
    mark_price,
    market_state,
    open_algo_orders,
    open_orders,
    position,
)


class FakeClient:
    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": "50000"}

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": "50001"}

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
        if symbol is None:
            return [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        return [{"symbol": symbol, "positionAmt": "0"}]

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def account_info(self, *, timeout=None):
        return {"assets": [{"asset": "USDT", "availableBalance": "1000"}]}


def test_heavy_snapshot_not_stale_when_only_exchange_rules_are_old():
    """exchange_rules 127s old + 6h TTL valid + fresh operational data → stale=False."""
    now = time.time()
    # bootstrap exchange info so exchange_rules is in cache
    provider = SnapshotProvider(
        client=FakeClient(),
        settings=SnapshotProviderSettings(
            max_data_age_seconds=30.0,
            exchange_info_ttl_seconds=21_600.0,
        ),
    )
    try:
        provider.bootstrap_exchange_info(now=now - 127.0)

        snapshot_now = time.time()
        heavy = provider.get_heavy_snapshot("BTCUSDT", now=snapshot_now)

        assert heavy.stale is False, (
            f"stale={heavy.stale} mas exchange_rules tem apenas 127s "
            f"(TTL=6h) e dados operacionais são frescos. "
            f"data_age_seconds={heavy.data_age_seconds:.2f}s"
        )
        assert heavy.data_age_seconds < 30.0, (
            f"data_age_seconds={heavy.data_age_seconds:.2f}s deve refletir apenas "
            "dados operacionais, não exchange_rules"
        )
        assert heavy.exchange_rules is not None, "exchange_rules deve ser válido (não None)"
    finally:
        provider.close()


def test_heavy_snapshot_stale_when_operational_data_is_old():
    """Snapshot com dados operacionais antigos → stale=True independente de exchange_rules."""
    now = time.time()
    provider = SnapshotProvider(
        client=FakeClient(),
        settings=SnapshotProviderSettings(
            max_data_age_seconds=30.0,
            exchange_info_ttl_seconds=21_600.0,
        ),
    )
    try:
        provider.bootstrap_exchange_info(now=now)
        # fetch a heavy snapshot and then ask for it "much later" to simulate stale age
        _ = provider.get_heavy_snapshot("BTCUSDT", now=now)
        # Re-evaluate from far in the future (40s after actual fetch)
        heavy_from_future = provider.get_heavy_snapshot("BTCUSDT", now=now + 40.0)
        # The operational data (fetched ~now) is ~40s old → exceeds max_data_age_seconds=30s
        assert heavy_from_future.stale is True, (
            "Dados operacionais de 40s devem causar stale=True (max=30s)"
        )
    finally:
        provider.close()


def test_heavy_snapshot_second_signal_exchange_rules_still_valid_not_stale():
    """Segundo sinal com exchange_rules velha mas TTL válido — stale deve ser False."""
    now = time.time()
    provider = SnapshotProvider(
        client=FakeClient(),
        settings=SnapshotProviderSettings(
            max_data_age_seconds=30.0,
            exchange_info_ttl_seconds=21_600.0,
        ),
    )
    try:
        # Bootstrap exchange info 120s ago (still valid given 6h TTL)
        provider.bootstrap_exchange_info(now=now - 120.0)

        # Second signal moment: operational data fetched right now
        second_signal_now = time.time()
        heavy = provider.get_heavy_snapshot("BTCUSDT", now=second_signal_now)

        assert heavy.stale is False, (
            f"Segundo sinal: exchange_rules tem 120s (TTL 6h) mas stale={heavy.stale}. "
            "exchange_rules não deve causar STALE_DATA nos dados operacionais."
        )
        assert heavy.exchange_rules is not None
    finally:
        provider.close()


def _make_snapshot_value(key, value, *, stale: bool = False, now: float) -> SnapshotValue:
    """Construct a SnapshotValue with controlled staleness for unit testing."""
    return SnapshotValue(
        key=key,
        value=value,
        source="test_source",
        observed_at=now,
        fetched_at=now,
        age_seconds=0.0,
        stale=stale,
        from_cache=False,
        latency_ms=0.0,
    )


def _make_controlled_snapshot(
    *, rules_stale: bool, operational_stale: bool = False
) -> tuple[Snapshot, float]:
    """Build a Snapshot where staleness of individual items can be controlled.

    Returns (snapshot, now) so callers can pass now= to heavy_snapshot_from_snapshot.
    """
    now = time.time()
    sym = "BTCUSDT"
    market_val = MarketSnapshot(symbol=sym, price=Decimal("50000"), prices=[Decimal("50000")])
    pos_val = FuturesPosition(
        symbol=sym,
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
    rules_val = SymbolRules(
        symbol=sym,
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_tick=Decimal("0.10"),
    )

    resources = {
        market_state(sym): _make_snapshot_value(
            market_state(sym), market_val, stale=operational_stale, now=now
        ),
        mark_price(sym): _make_snapshot_value(
            mark_price(sym), Decimal("50001"), stale=operational_stale, now=now
        ),
        position(sym): _make_snapshot_value(
            position(sym), pos_val, stale=operational_stale, now=now
        ),
        open_orders(sym): _make_snapshot_value(
            open_orders(sym), [], stale=operational_stale, now=now
        ),
        open_algo_orders(sym): _make_snapshot_value(
            open_algo_orders(sym), [], stale=operational_stale, now=now
        ),
        ACCOUNT: _make_snapshot_value(ACCOUNT, {"assets": []}, stale=operational_stale, now=now),
        exchange_rules(sym): _make_snapshot_value(
            exchange_rules(sym), rules_val, stale=rules_stale, now=now
        ),
    }
    snap = Snapshot(snapshot_id="test", created_at=now, resources=resources)
    return snap, now


def test_heavy_snapshot_stale_when_exchange_rules_item_is_stale():
    """exchange_rules.stale=True → heavy.stale=True even with fresh operational data.

    Unit test: directly controls snapshot item staleness via SnapshotValue.stale,
    bypassing Coalestra TTL machinery. This verifies that heavy_snapshot_from_snapshot
    correctly propagates rules_item.stale to the returned HeavyMarketSnapshot.
    """
    provider = SnapshotProvider(
        client=FakeClient(),
        settings=SnapshotProviderSettings(max_data_age_seconds=30.0),
    )
    try:
        snap, now = _make_controlled_snapshot(rules_stale=True, operational_stale=False)
        heavy = provider.heavy_snapshot_from_snapshot(snap, "BTCUSDT", now=now)
        assert heavy.stale is True, (
            f"exchange_rules.stale=True deve propagar para heavy.stale. "
            f"data_age_seconds={heavy.data_age_seconds:.2f}s"
        )
        assert heavy.data_age_seconds < 1.0, (
            "dados operacionais são frescos — data_age_seconds deve ser próximo de zero"
        )
    finally:
        provider.close()


def test_heavy_snapshot_fresh_when_exchange_rules_item_is_not_stale():
    """exchange_rules.stale=False + operational fresh → heavy.stale=False."""
    provider = SnapshotProvider(
        client=FakeClient(),
        settings=SnapshotProviderSettings(max_data_age_seconds=30.0),
    )
    try:
        snap, now = _make_controlled_snapshot(rules_stale=False, operational_stale=False)
        heavy = provider.heavy_snapshot_from_snapshot(snap, "BTCUSDT", now=now)
        assert heavy.stale is False
    finally:
        provider.close()
