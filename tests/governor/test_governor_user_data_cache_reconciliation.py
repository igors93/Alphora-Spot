from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, PositionDirection
from governor.services import UserDataCacheService
from governor.user_data_cache import UserDataShadowCache


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


def _service(**config_overrides):
    config = {
        "user_data_stream_cache_reconciliation_enabled": True,
    }
    config.update(config_overrides)
    return UserDataCacheService(
        config=SimpleNamespace(**config),
        journal=FakeJournal(),
        cache=UserDataShadowCache(),
    )


def _heavy():
    return SimpleNamespace(
        position=FuturesPosition(
            symbol="BTCUSDT",
            direction=PositionDirection.LONG,
            quantity=Decimal("0.02"),
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("1.5"),
        ),
        account_info={
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "100",
                    "crossWalletBalance": "90",
                }
            ]
        },
        open_orders=[],
    )


def test_governor_records_user_data_cache_reconciliation():
    service = _service()
    service.cache.dirty_position_symbols.add("BTCUSDT")
    service.cache.account_dirty = True

    service.record_reconciliation(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        heavy=_heavy(),
        now=101.0,
        rest_backed_position=True,
        rest_backed_account=True,
        rest_backed_open_orders=True,
        position_source="REST",
        account_source="REST",
        open_orders_source="REST",
        final_position_source="SYNC_POSITION",
    )

    assert service.journal.events

    event_type, data = service.journal.events[0]

    assert event_type == "user_data_stream_cache_reconciliation"
    assert data["status"] == "RECONCILED"
    assert data["position_reconciled"] is True
    assert data["account_reconciled"] is True
    assert data["open_orders_reconciled"] is True
    assert data["dirty_position_cleared"] is True
    assert data["account_dirty_cleared"] is True
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False
    assert data["position_source"] == "REST"
    assert data["account_source"] == "REST"
    assert data["open_orders_source"] == "REST"
    assert data["final_position_source"] == "SYNC_POSITION"


def test_governor_does_not_record_reconciliation_when_disabled():
    service = _service(user_data_stream_cache_reconciliation_enabled=False)

    service.record_reconciliation(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        heavy=_heavy(),
        now=101.0,
        rest_backed_position=True,
        rest_backed_account=True,
        rest_backed_open_orders=True,
    )

    assert service.journal.events == []


def test_governor_does_not_record_reconciliation_when_everything_was_cache_backed():
    service = _service()

    service.record_reconciliation(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        heavy=_heavy(),
        now=101.0,
        rest_backed_position=False,
        rest_backed_account=False,
        rest_backed_open_orders=False,
    )

    assert service.journal.events == []


def test_governor_reconciliation_does_not_clear_dirty_position_without_rest_source():
    service = _service()
    service.cache.dirty_position_symbols.add("BTCUSDT")

    service.record_reconciliation(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"),
        heavy=_heavy(),
        now=101.0,
        rest_backed_position=True,
        rest_backed_account=False,
        rest_backed_open_orders=False,
        position_source="SYNC_POSITION",
        account_source="USER_DATA_CACHE",
        open_orders_source="USER_DATA_CACHE",
        final_position_source="SYNC_POSITION",
    )

    assert service.journal.events == []
    assert "BTCUSDT" in service.cache.dirty_position_symbols
