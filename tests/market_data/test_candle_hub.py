from __future__ import annotations

from decimal import Decimal

from domain.candle import Candle
from domain.strategy_requirements import StrategyDataRequirements
from market_data.candle_hub import (
    CANDLE_HUB_MODE_ACTIVE_PAPER,
    CANDLE_HUB_MODE_SHADOW,
    CandleDataHub,
)


def test_candle_data_hub_bootstrap_refresh_and_snapshot():
    first = _candle(0, "100")
    second = _candle(60_000, "101")
    third = _candle(120_000, "102")
    provider = _Provider(bootstrap=[first, second], refresh=[third])
    hub = CandleDataHub(
        provider,
        requirements_by_symbol={
            "BTCUSDT": StrategyDataRequirements(
                primary_timeframe="1m",
                required_candles={"1m": 2},
            )
        },
        mode=CANDLE_HUB_MODE_ACTIVE_PAPER,
    )

    hub.bootstrap()
    assert provider.bootstrap_calls == [("BTCUSDT", "1m", 2)]
    assert [item.close for item in hub.candle_snapshot_for_symbol("BTCUSDT")["1m"]] == [
        Decimal("100"),
        Decimal("101"),
    ]

    hub.refresh(now_ms=third.open_time_ms + 60_000)
    assert provider.refresh_calls == [("BTCUSDT", "1m", second.open_time_ms)]
    assert [item.close for item in hub.candle_snapshot_for_symbol("BTCUSDT")["1m"]] == [
        Decimal("101"),
        Decimal("102"),
    ]


def test_candle_data_hub_shadow_collects_without_delivering_candles():
    provider = _Provider(bootstrap=[_candle(0, "100")], refresh=[])
    hub = CandleDataHub(
        provider,
        requirements_by_symbol={
            "BTCUSDT": StrategyDataRequirements(
                primary_timeframe="1m",
                required_candles={"1m": 1},
            )
        },
        mode=CANDLE_HUB_MODE_SHADOW,
    )

    hub.bootstrap()

    assert hub.candle_snapshot_for_symbol("BTCUSDT") == {}
    assert hub.diagnostics_for_symbol("BTCUSDT")["1m"]["available_count"] == 1


class _Provider:
    def __init__(self, *, bootstrap: list[Candle], refresh: list[Candle]):
        self.bootstrap = list(bootstrap)
        self.refresh = list(refresh)
        self.bootstrap_calls: list[tuple[str, str, int]] = []
        self.refresh_calls: list[tuple[str, str, int]] = []

    def fetch_closed_candles(self, *, symbol: str, interval: str, limit: int):
        self.bootstrap_calls.append((symbol, interval, limit))
        return self.bootstrap[-limit:]

    def fetch_since(self, *, symbol: str, interval: str, since_open_time_ms: int):
        self.refresh_calls.append((symbol, interval, since_open_time_ms))
        return [item for item in self.refresh if item.open_time_ms > since_open_time_ms]


def _candle(open_time_ms: int, close: str) -> Candle:
    value = Decimal(close)
    return Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + 59_999,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("10"),
        is_closed=True,
        source="test",
    )
