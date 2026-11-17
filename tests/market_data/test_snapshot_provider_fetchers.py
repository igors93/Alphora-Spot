"""The WebSocket market stream source the snapshot engine pulls (ADR-003)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from governor.market_data_hub import MarketStateSnapshot, PriceSnapshot
from market_data.price_window import MarketPriceWindow
from market_data.snapshot_provider.fetchers import SourceFetchers
from market_data.snapshot_provider.keys import market_state
from market_data.snapshot_provider.observed_at import CONTEXT_NOW_KEY, DOMAIN_OBSERVED_AT_KEY
from market_data.snapshot_provider.settings import SnapshotProviderSettings


@dataclass
class _Context:
    requested_at: float
    metadata: dict[str, Any] = field(default_factory=dict)


class _Hub:
    def __init__(self, states: dict[str, MarketStateSnapshot]) -> None:
        self.states = states
        self.calls: list[tuple[str, float]] = []

    def latest_market_state(self, symbol: str, *, now: float) -> MarketStateSnapshot | None:
        self.calls.append((symbol, now))
        return self.states.get(symbol)


def _fetchers(hub: Any) -> SourceFetchers:
    settings = SnapshotProviderSettings()
    return SourceFetchers(
        client=None,
        transport=None,  # type: ignore[arg-type]  # the stream source never calls REST
        settings=settings,
        market_data_hub=hub,
        candle_data_hub=None,
        user_data_cache=None,
        price_window=MarketPriceWindow(maxlen=8),
        wall_clock=lambda: 0.0,
    )


def _state(*, event_time: float | None, received_at: float) -> MarketStateSnapshot:
    return MarketStateSnapshot(
        price_snapshot=PriceSnapshot(
            symbol="BTCUSDT",
            price=Decimal("100.5"),
            bid_price=Decimal("100.4"),
            ask_price=Decimal("100.6"),
            event_time=event_time,
            received_at=received_at,
        ),
        prices=(Decimal("100.0"), Decimal("100.5")),
    )


def test_stream_value_is_read_at_resolution_time_with_the_cycle_clock() -> None:
    hub = _Hub({"BTCUSDT": _state(event_time=1_000.0, received_at=1_000.2)})
    context = _Context(requested_at=1_001.0, metadata={CONTEXT_NOW_KEY: 1_000.9})

    payloads = _fetchers(hub).market_stream_batch([market_state("BTCUSDT")], context)

    payload = payloads[market_state("BTCUSDT")]
    assert hub.calls == [("BTCUSDT", 1_000.9)]
    assert payload.value.price == Decimal("100.5")
    assert payload.value.prices == [Decimal("100.0"), Decimal("100.5")]
    assert payload.observed_at == pytest.approx(1_000.0)
    assert payload.metadata[DOMAIN_OBSERVED_AT_KEY] == pytest.approx(1_000.0)
    assert payload.metadata["transport_delay_ms"] == pytest.approx(200.0)


def test_exchange_clock_ahead_cannot_create_a_future_observation() -> None:
    hub = _Hub({"BTCUSDT": _state(event_time=1_005.0, received_at=1_000.0)})

    payloads = _fetchers(hub).market_stream_batch(
        [market_state("BTCUSDT")], _Context(requested_at=1_000.0)
    )

    payload = payloads[market_state("BTCUSDT")]
    assert payload.observed_at == pytest.approx(1_000.0)
    assert payload.metadata["transport_delay_ms"] == 0.0


def test_symbols_without_stream_state_are_left_to_the_rest_source() -> None:
    hub = _Hub({"BTCUSDT": _state(event_time=None, received_at=1_000.0)})

    payloads = _fetchers(hub).market_stream_batch(
        [market_state("BTCUSDT"), market_state("ETHUSDT")], _Context(requested_at=1_000.0)
    )

    assert list(payloads) == [market_state("BTCUSDT")]
    assert payloads[market_state("BTCUSDT")].observed_at == pytest.approx(1_000.0)


def test_no_hub_resolves_nothing() -> None:
    assert _fetchers(None).market_stream_batch([market_state("BTCUSDT")], _Context(1.0)) == {}
