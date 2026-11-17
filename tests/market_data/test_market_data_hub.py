import json
from decimal import Decimal

from governor.market_data_hub import (
    MarketDataHub,
    parse_book_ticker_payload,
    parse_combined_message,
)


def _message(symbol="BTCUSDT", bid="100", ask="102"):
    return json.dumps(
        {
            "stream": f"{symbol.lower()}@bookTicker",
            "data": {"s": symbol, "b": bid, "a": ask, "E": 1_000},
        }
    )


def test_market_data_hub_normalizes_symbols():
    hub = MarketDataHub([" btcusdt ", "ETHUSDT", "btcusdt"], "wss://example.test")

    assert hub.symbols == ("BTCUSDT", "ETHUSDT")


def test_market_data_hub_builds_lowercase_combined_stream_url():
    hub = MarketDataHub(["BTCUSDT", "ETHUSDT"], "wss://example.test/")

    assert hub.stream_url == (
        "wss://example.test/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker"
    )


def test_parse_combined_book_ticker_message():
    payload = parse_combined_message(_message())
    snapshot = parse_book_ticker_payload(payload, received_at=2.0)

    assert snapshot is not None
    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.bid_price == Decimal("100")
    assert snapshot.ask_price == Decimal("102")
    assert snapshot.price == Decimal("101")
    assert snapshot.event_time == 1.0


def test_parse_direct_book_ticker_payload():
    payload = {"s": "ETHUSDT", "b": "200", "a": "204"}
    snapshot = parse_book_ticker_payload(payload, received_at=5.0)

    assert snapshot is not None
    assert snapshot.symbol == "ETHUSDT"
    assert snapshot.price == Decimal("202")


def test_parse_book_ticker_ignores_invalid_event():
    assert parse_book_ticker_payload({"s": "BTCUSDT", "b": "0", "a": "102"}) is None
    assert parse_book_ticker_payload({"s": "BTCUSDT", "b": "103", "a": "102"}) is None
    assert parse_combined_message("not-json") is None


def test_market_data_hub_keeps_only_latest_snapshot_per_symbol():
    hub = MarketDataHub(["BTCUSDT"], "wss://example.test")

    hub.ingest_message(_message(bid="100", ask="102"), received_at=10.0)
    hub.ingest_message(_message(bid="110", ask="112"), received_at=11.0)

    latest = hub.latest_price("btcusdt", now=11.5)
    assert latest is not None
    assert latest.price == Decimal("111")
    assert len(hub.latest_by_symbol) == 1


def test_market_data_hub_latest_price_respects_staleness():
    hub = MarketDataHub(
        ["BTCUSDT"],
        "wss://example.test",
        max_price_age_seconds=1.0,
    )
    hub.ingest_message(_message(), received_at=10.0)

    assert hub.latest_price("BTCUSDT", now=10.5) is not None
    assert hub.latest_price("BTCUSDT", now=11.1) is None


def test_market_data_hub_latest_price_requires_enabled_symbol_and_not_stopped():
    hub = MarketDataHub(["BTCUSDT"], "wss://example.test")
    hub.ingest_message(_message(), received_at=10.0)

    assert hub.latest_price("ETHUSDT", now=10.5) is None
    assert hub.has_recent_price("BTCUSDT", now=10.5) is True

    hub.stop()

    assert hub.latest_price("BTCUSDT", now=10.5) is None
    assert hub.has_recent_price("BTCUSDT", now=10.5) is False


def test_market_data_hub_dirty_symbols_are_coalesced():
    hub = MarketDataHub(["BTCUSDT"], "wss://example.test")

    hub.ingest_message(_message(bid="100", ask="102"), received_at=10.0)
    hub.ingest_message(_message(bid="101", ask="103"), received_at=10.1)

    assert hub.drain_dirty_symbols(now=10.2) == ["BTCUSDT"]
    assert hub.drain_dirty_symbols(now=10.3) == []


def test_market_data_hub_drain_respects_max_symbols_per_drain():
    hub = MarketDataHub(
        ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "wss://example.test",
        max_symbols_per_drain=2,
    )
    for index, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT")):
        hub.ingest_message(_message(symbol=symbol), received_at=10.0 + index)

    first = hub.drain_dirty_symbols(now=20.0)
    second = hub.drain_dirty_symbols(now=20.1)

    assert len(first) == 2
    assert len(second) == 1
    assert set(first + second) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


def test_market_data_hub_health_snapshot_reports_coherent_counters():
    hub = MarketDataHub(
        ["BTCUSDT", "ETHUSDT"],
        "wss://example.test",
        max_price_age_seconds=1.0,
    )
    hub.ingest_message(_message(symbol="BTCUSDT"), received_at=10.0)
    hub.ingest_message(_message(symbol="ETHUSDT"), received_at=10.5)

    health = hub.health_snapshot(now=11.0)

    assert health["enabled"] is True
    assert health["known_prices"] == 2
    assert health["events_since_heartbeat"] == 2
    assert health["stale_symbols"] == 0
    assert health["largest_price_age_seconds"] == 1.0
    assert health["last_message_at"] == 10.5


def test_market_data_hub_does_not_accumulate_infinite_ticks():
    hub = MarketDataHub(
        ["BTCUSDT"],
        "wss://example.test",
        price_window=3,
    )

    for index in range(10):
        hub.ingest_message(
            _message(bid=str(100 + index), ask=str(102 + index)),
            received_at=10.0 + index,
        )

    assert len(hub.latest_by_symbol) == 1
    assert len(hub.prices_by_symbol["BTCUSDT"]) == 3
    assert len(hub.dirty_symbols) == 1


def test_market_data_hub_dirty_symbols_limit_is_bounded():
    hub = MarketDataHub(
        ["BTCUSDT", "ETHUSDT"],
        "wss://example.test",
        dirty_symbols_limit=1,
    )

    hub.ingest_message(_message(symbol="BTCUSDT"), received_at=10.0)
    hub.ingest_message(_message(symbol="ETHUSDT"), received_at=10.1)

    assert len(hub.dirty_symbols) == 1
    assert set(hub.latest_by_symbol) == {"BTCUSDT", "ETHUSDT"}


def test_latest_market_state_returns_price_and_history_atomically():
    hub = MarketDataHub(
        symbols=["BTCUSDT"],
        base_url="wss://stream.binancefuture.com",
        price_window=3,
        market_shock_detector_enabled=False,
    )
    for index, price in enumerate(("100", "101", "102"), start=1):
        payload = {"s": "BTCUSDT", "b": price, "a": price, "E": index * 1000}
        assert hub.ingest_message(json.dumps(payload), received_at=float(index)) is True

    state = hub.latest_market_state("BTCUSDT", now=3.0)

    assert state is not None
    assert state.price_snapshot.price == Decimal("102")
    assert state.prices == (Decimal("100"), Decimal("101"), Decimal("102"))


def test_fallback_market_state_reuses_the_hub_price_window():
    hub = MarketDataHub(
        symbols=["BTCUSDT"],
        base_url="wss://stream.binancefuture.com",
        price_window=2,
        market_shock_detector_enabled=False,
    )
    first = hub.record_fallback_market_state(
        "BTCUSDT", Decimal("100"), observed_at=1.0, source="rest"
    )
    second = hub.record_fallback_market_state(
        "BTCUSDT", Decimal("101"), observed_at=2.0, source="rest"
    )

    assert first.prices == (Decimal("100"),)
    assert second.prices == (Decimal("100"), Decimal("101"))
    assert tuple(hub.prices_by_symbol["BTCUSDT"]) == second.prices
