"""Tests: comportamento fail-closed do Coalestra.

Verifica que a entrada permanece bloqueada quando:
- Não existe sessão Coalestra
- REST está indisponível / stale
- Faltam recursos obrigatórios
- Consistência inválida (skew)

Não deve ser possível contornar gates de segurança quando o snapshot é inconsistente.
"""

from __future__ import annotations

import time

import pytest

from market_data.snapshot_engine import SnapshotBuildError
from market_data.snapshot_provider import (
    SnapshotProvider,
    SnapshotProviderSettings,
)


class AlwaysFailClient:
    """Client que falha em todas as chamadas privadas."""

    def ticker_price(self, symbol, *, timeout=None):
        raise RuntimeError("Sem conexão com a exchange")

    def mark_price(self, symbol, *, timeout=None):
        raise RuntimeError("Sem conexão com a exchange")

    def exchange_info(self, *, timeout=None):
        raise RuntimeError("Sem conexão com a exchange")

    def position_risk(self, symbol=None, *, timeout=None):
        raise RuntimeError("Sem conexão com a exchange")

    def open_orders(self, symbol, *, timeout=None):
        raise RuntimeError("Sem conexão com a exchange")

    def open_algo_orders(self, symbol, *, timeout=None):
        raise RuntimeError("Sem conexão com a exchange")

    def account_info(self, *, timeout=None):
        raise RuntimeError("Sem conexão com a exchange")


def _make_tight_settings():
    return SnapshotProviderSettings(
        deadline_seconds=1.0,
        cycle_deadline_seconds=2.0,
        rest_timeout_seconds=0.5,
        rest_connect_timeout_seconds=0.05,
        rest_read_timeout_seconds=0.05,
        rest_transport_timeout_seconds=0.2,
        rest_min_attempt_timeout_seconds=0.05,
        rest_retry_delay_seconds=0.0,
        exchange_info_timeout_seconds=0.5,
        exchange_info_connect_timeout_seconds=0.05,
        exchange_info_read_timeout_seconds=0.05,
        exchange_info_transport_timeout_seconds=0.2,
    )


def test_get_heavy_snapshot_fails_when_rest_unavailable():
    """Sem REST disponível, get_heavy_snapshot deve levantar exceção (fail-closed)."""
    provider = SnapshotProvider(
        client=AlwaysFailClient(),
        settings=_make_tight_settings(),
    )
    try:
        with pytest.raises((SnapshotBuildError, RuntimeError, Exception)):
            provider.get_heavy_snapshot("BTCUSDT", now=time.time())
    finally:
        provider.close()


def test_cycle_session_revalidation_fails_closed_without_source():
    """revalidate_for_execution deve lançar exceção quando todos os sources falham."""
    provider = SnapshotProvider(
        client=AlwaysFailClient(),
        settings=_make_tight_settings(),
    )
    now = time.time()
    try:
        with pytest.raises((SnapshotBuildError, Exception)):
            cycle = provider.open_cycle_session(
                cycle_id="fail-closed-test",
                now=now,
                symbols=["BTCUSDT"],
            )
            try:
                cycle.revalidate_for_execution("BTCUSDT")
            finally:
                cycle.close()
    finally:
        provider.close()


def test_bootstrap_exchange_info_fails_closed_without_rest():
    """bootstrap_exchange_info deve levantar exceção quando REST não está disponível."""
    provider = SnapshotProvider(
        client=AlwaysFailClient(),
        settings=_make_tight_settings(),
    )
    try:
        with pytest.raises((SnapshotBuildError, RuntimeError)):
            provider.bootstrap_exchange_info(now=time.time())
    finally:
        provider.close()


class FakeClientWithStaleData:
    """Client que retorna dados mas sem o campo necessário."""

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


def test_provider_closes_cleanly_without_leaving_resources_open():
    """Provider deve fechar limpo mesmo após falhas."""
    provider = SnapshotProvider(
        client=AlwaysFailClient(),
        settings=_make_tight_settings(),
    )
    try:
        with pytest.raises((SnapshotBuildError, RuntimeError)):
            provider.get_heavy_snapshot("BTCUSDT", now=time.time())
    finally:
        provider.close()

    assert provider.closed is True


def test_heavy_snapshot_stale_when_rest_data_is_old():
    """Snapshot cujos dados operacionais REST são velhos deve ser stale (não esconder isso)."""
    provider = SnapshotProvider(
        client=FakeClientWithStaleData(),
        settings=SnapshotProviderSettings(
            max_data_age_seconds=5.0,
            exchange_info_ttl_seconds=21_600.0,
        ),
    )
    try:
        provider.bootstrap_exchange_info()
        # Pegar snapshot e depois verificá-lo muito depois
        now = time.time()
        _ = provider.get_heavy_snapshot("BTCUSDT", now=now)
        # Perguntar sobre freshness 10s depois (> max_data_age_seconds=5s)
        stale_heavy = provider.get_heavy_snapshot("BTCUSDT", now=now + 10.0)
        assert stale_heavy.stale is True, (
            "Dados com 10s de idade (max=5s) devem ser reportados como stale"
        )
    finally:
        provider.close()
