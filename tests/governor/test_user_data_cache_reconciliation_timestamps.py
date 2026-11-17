"""Tests: timestamps de reconciliação REST não devem ser artificialmente iguais.

Critério obrigatório (problema #3):
- REST usa o instante real de recebimento ou conclusão
- Chamadas diferentes não recebem artificialmente o mesmo timestamp
- O início do ciclo não é reutilizado como timestamp comum
"""

from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, PositionDirection
from governor.user_data_cache import UserDataShadowCache
from governor.user_data_cache_reconciliation import reconcile_user_data_cache_from_rest


def _flat_position() -> FuturesPosition:
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def _heavy_with_flat_position():
    return SimpleNamespace(
        position=_flat_position(),
        account_info={"assets": [{"asset": "USDT", "walletBalance": "1000"}]},
        open_orders=[],
    )


def test_reconciliation_uses_time_time_not_cycle_start():
    """received_at de cada componente deve ser >= agora, não o timestamp do ciclo."""
    cache = UserDataShadowCache()
    heavy = _heavy_with_flat_position()
    cycle_start = time.time() - 5.0  # simula ciclo que começou 5s atrás

    before_reconcile = time.time()
    reconcile_user_data_cache_from_rest(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=heavy,
        now=cycle_start,  # ciclo iniciou 5s atrás
        rest_backed_position=True,
        rest_backed_account=True,
        rest_backed_open_orders=True,
    )
    after_reconcile = time.time()

    position_cached = cache.position_snapshot_with_timestamp("BTCUSDT")
    account_cached = cache.account_info_snapshot_with_timestamp()

    assert position_cached is not None
    _, pos_ts = position_cached
    assert pos_ts >= before_reconcile, (
        f"position received_at={pos_ts:.3f} é menor que before_reconcile={before_reconcile:.3f}. "
        "Deveria ser time.time() no momento da chamada, não o timestamp do ciclo."
    )
    assert pos_ts <= after_reconcile + 0.1

    if account_cached is not None:
        _, acc_ts = account_cached
        assert acc_ts >= before_reconcile, (
            f"account received_at={acc_ts:.3f} é menor que before_reconcile={before_reconcile:.3f}"
        )


def test_distinct_timestamps_for_different_rest_components():
    """Posição, conta e ordens devem ter timestamps distintos (não idênticos)."""
    call_times: list[float] = []
    original_time = time.time

    class TrackingCache(UserDataShadowCache):
        def reconcile_position_from_rest(self, **kwargs):
            call_times.append(kwargs["received_at"])
            return super().reconcile_position_from_rest(**kwargs)

        def reconcile_account_from_rest(self, **kwargs):
            call_times.append(kwargs["received_at"])
            return super().reconcile_account_from_rest(**kwargs)

        def reconcile_open_orders_from_rest(self, **kwargs):
            call_times.append(kwargs["received_at"])
            return super().reconcile_open_orders_from_rest(**kwargs)

    cache = TrackingCache()
    heavy = _heavy_with_flat_position()
    cycle_start = original_time() - 10.0

    reconcile_user_data_cache_from_rest(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=heavy,
        now=cycle_start,
        rest_backed_position=True,
        rest_backed_account=True,
        rest_backed_open_orders=True,
    )

    assert len(call_times) == 3, f"Esperado 3 chamadas, obtido {len(call_times)}"
    for ts in call_times:
        assert ts > cycle_start, (
            f"timestamp={ts:.3f} não deve ser o tempo do ciclo cycle_start={cycle_start:.3f}"
        )


def test_reconciliation_cycle_now_not_used_as_received_at():
    """O parâmetro 'now' (início do ciclo) não deve ser reutilizado como received_at."""
    seen_received_ats: list[float] = []

    class SpyCache(UserDataShadowCache):
        def reconcile_position_from_rest(self, **kwargs):
            seen_received_ats.append(kwargs["received_at"])
            return super().reconcile_position_from_rest(**kwargs)

    cache = SpyCache()
    heavy = _heavy_with_flat_position()
    artificial_cycle_now = 1_000_000.0  # tempo de ciclo sintético no passado distante

    reconcile_user_data_cache_from_rest(
        cache=cache,
        symbol="BTCUSDT",
        heavy_snapshot=heavy,
        now=artificial_cycle_now,
        rest_backed_position=True,
        rest_backed_account=False,
        rest_backed_open_orders=False,
    )

    assert len(seen_received_ats) == 1
    actual_ts = seen_received_ats[0]
    assert actual_ts != artificial_cycle_now, (
        f"received_at={actual_ts} é igual ao ciclo artificial={artificial_cycle_now}. "
        "O início do ciclo não deve ser reutilizado como timestamp de recebimento REST."
    )
    assert actual_ts > artificial_cycle_now + 1_000, (
        "received_at deve ser tempo atual (>>tempo sintético de ciclo)"
    )
