from decimal import Decimal
from types import SimpleNamespace

from governor.market_data_hub import MarketDataHub, PriceSnapshot
from governor.market_shock_detector import MarketShockDetector


def _snapshot(symbol, price, received_at):
    return SimpleNamespace(
        symbol=symbol,
        price=Decimal(str(price)),
        event_time=None,
        received_at=float(received_at),
    )


def test_market_shock_detector_detects_drop_inside_window():
    detector = MarketShockDetector(
        window_seconds=3.0,
        move_bps_threshold=50.0,
        critical_bps_threshold=120.0,
        cooldown_seconds=10.0,
    )

    assert detector.evaluate_snapshot(_snapshot("BTCUSDT", "100", 100.0)) is None

    event = detector.evaluate_snapshot(_snapshot("BTCUSDT", "99.20", 101.0))

    assert event is not None
    assert event.symbol == "BTCUSDT"
    assert event.direction == "DROP"
    assert event.severity == "MEDIUM"
    assert event.move_bps == -80.0
    assert event.shadow_only is True

    drained = detector.drain_events()
    assert len(drained) == 1
    assert drained[0]["symbol"] == "BTCUSDT"
    assert drained[0]["direction"] == "DROP"


def test_market_shock_detector_respects_cooldown():
    detector = MarketShockDetector(
        window_seconds=3.0,
        move_bps_threshold=50.0,
        cooldown_seconds=10.0,
    )

    detector.evaluate_snapshot(_snapshot("BTCUSDT", "100", 100.0))
    first = detector.evaluate_snapshot(_snapshot("BTCUSDT", "99", 101.0))
    second = detector.evaluate_snapshot(_snapshot("BTCUSDT", "98", 102.0))

    assert first is not None
    assert second is None
    assert len(detector.drain_events()) == 1


def test_market_data_hub_records_market_shock_without_rest():
    hub = MarketDataHub(
        symbols=["BTCUSDT"],
        base_url="wss://example.test",
        market_shock_window_seconds=3.0,
        market_shock_move_bps_threshold=50.0,
        market_shock_cooldown_seconds=10.0,
    )

    hub._record_snapshot(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=Decimal("100"),
            bid_price=Decimal("99.99"),
            ask_price=Decimal("100.01"),
            event_time=None,
            received_at=100.0,
        )
    )
    hub._record_snapshot(
        PriceSnapshot(
            symbol="BTCUSDT",
            price=Decimal("99.2"),
            bid_price=Decimal("99.19"),
            ask_price=Decimal("99.21"),
            event_time=None,
            received_at=101.0,
        )
    )

    events = hub.drain_market_shock_events()

    assert len(events) == 1
    assert events[0]["symbol"] == "BTCUSDT"
    assert events[0]["direction"] == "DROP"

    health = hub.health_snapshot(now=101.0)
    assert health["market_shock_pending_events"] == 0
