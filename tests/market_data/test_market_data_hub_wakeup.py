from decimal import Decimal

from governor.market_data_hub import MarketDataHub, PriceSnapshot


def _snapshot(symbol, price, received_at):
    price = Decimal(str(price))
    return PriceSnapshot(
        symbol=symbol,
        price=price,
        bid_price=price - Decimal("0.01"),
        ask_price=price + Decimal("0.01"),
        event_time=None,
        received_at=float(received_at),
    )


def test_market_data_hub_calls_wakeup_callback_on_market_shock():
    wakeups = []

    hub = MarketDataHub(
        symbols=["BTCUSDT"],
        base_url="wss://example.test",
        market_shock_window_seconds=3.0,
        market_shock_move_bps_threshold=50.0,
        market_shock_cooldown_seconds=10.0,
        market_wakeup_callback=wakeups.append,
    )

    hub._record_snapshot(_snapshot("BTCUSDT", "100", 100.0))
    hub._record_snapshot(_snapshot("BTCUSDT", "99.20", 101.0))

    assert len(wakeups) == 1
    assert wakeups[0]["symbol"] == "BTCUSDT"
    assert wakeups[0]["direction"] == "DROP"
    assert wakeups[0]["shadow_only"] is True


def test_market_data_hub_does_not_call_wakeup_callback_without_shock():
    wakeups = []

    hub = MarketDataHub(
        symbols=["BTCUSDT"],
        base_url="wss://example.test",
        market_shock_window_seconds=3.0,
        market_shock_move_bps_threshold=50.0,
        market_wakeup_callback=wakeups.append,
    )

    hub._record_snapshot(_snapshot("BTCUSDT", "100", 100.0))
    hub._record_snapshot(_snapshot("BTCUSDT", "99.90", 101.0))

    assert wakeups == []
