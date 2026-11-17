from governor.cycle.telemetry import CycleMetricsState, CycleTelemetry
from governor.governor_observability import build_cycle_metrics_payload


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


class FakeLogger:
    def warning(self, *_args, **_kwargs):
        return None


class FakeHub:
    def __init__(self):
        self.events = [
            {
                "symbol": "BTCUSDT",
                "direction": "DROP",
                "severity": "HIGH",
                "reason": "DROP_120BPS_IN_1S",
                "price": "98.8",
                "reference_price": "100",
                "move_bps": -120.0,
                "window_seconds": 3.0,
                "observed_seconds": 1.0,
                "event_time": None,
                "received_at": 100.0,
                "shadow_only": True,
            }
        ]

    def drain_market_shock_events(self):
        events = list(self.events)
        self.events.clear()
        return events

    def health_snapshot(self, *, now=None):
        return {
            "enabled": True,
            "running": True,
            "connected": True,
            "known_prices": 2,
            "stale_symbols": 0,
            "largest_price_age_seconds": 0.25,
            "market_shock_pending_events": 0,
        }


def test_build_cycle_metrics_payload_summarizes_cycle():
    payload = build_cycle_metrics_payload(
        cycle_started_at=100.0,
        cycle_duration_ms=12.5,
        due_symbols=["BTCUSDT"],
        dirty_symbols=["ETHUSDT"],
        open_symbols={"BTCUSDT"},
        request_budget_summary={
            "enabled": True,
            "used_heavy_checks": 1,
            "denied_heavy_checks": 0,
        },
        defensive_blocks=[
            {
                "symbol": "BTCUSDT",
                "reason": "DEFENSIVE_MODE_MARKET_SHOCK",
            }
        ],
        defensive_mode_summary={
            "enabled": True,
            "mode": "MARKET_SHOCK",
            "shadow_only": True,
        },
        shock_events=[{"symbol": "ETHUSDT"}],
        symbol_timings=[
            {"symbol": "BTCUSDT", "status": "OK", "duration_ms": 4.0},
            {"symbol": "ETHUSDT", "status": "OK", "duration_ms": 7.0},
        ],
        fast_lane_symbols=["ETHUSDT"],
        market_health={
            "enabled": True,
            "running": True,
            "connected": True,
            "known_prices": 2,
            "stale_symbols": 0,
            "largest_price_age_seconds": 0.3,
            "market_shock_pending_events": 1,
        },
    )

    assert payload["cycle_duration_ms"] == 12.5
    assert payload["evaluated_symbols"] == 2
    assert payload["dirty_symbols_count"] == 1
    assert payload["open_symbols_count"] == 1
    assert payload["shock_events_count"] == 1
    assert payload["fast_lane_symbols_count"] == 1
    assert payload["fast_lane_symbols"] == ["ETHUSDT"]
    assert payload["slowest_symbol"] == "ETHUSDT"
    assert payload["market_ws_connected"] is True
    assert payload["request_budget"]["enabled"] is True
    assert payload["request_budget"]["used_heavy_checks"] == 1
    assert payload["defensive_mode"]["enabled"] is True
    assert payload["defensive_mode"]["mode"] == "MARKET_SHOCK"
    assert payload["defensive_mode"]["shadow_only"] is True
    assert payload["defensive_blocks_count"] == 1
    assert payload["defensive_blocked_symbols"] == ["BTCUSDT"]


def test_governor_records_market_shock_events_and_cycle_metrics():
    telemetry = CycleTelemetry(
        config=None,
        journal=FakeJournal(),
        logger=FakeLogger(),
        market_data_hub=FakeHub(),
        state=CycleMetricsState(),
    )

    shock_events = telemetry.drain_shock_events()
    telemetry.record_shock_events(shock_events)
    telemetry.record_cycle_metrics(
        current_time=100.0,
        cycle_duration_ms=9.5,
        due_symbols=["BTCUSDT"],
        dirty_symbols=["BTCUSDT"],
        open_symbols=set(),
        shock_events=shock_events,
        symbol_timings=[
            {
                "symbol": "BTCUSDT",
                "status": "OK",
                "duration_ms": 3.2,
            }
        ],
    )

    event_types = [event_type for event_type, _data in telemetry.journal.events]

    assert "market_shock_detected" in event_types
    assert "governor_cycle_metrics" in event_types

    metrics = next(
        data
        for event_type, data in telemetry.journal.events
        if event_type == "governor_cycle_metrics"
    )

    assert metrics["cycle_duration_ms"] == 9.5
    assert metrics["evaluated_symbols"] == 1
    assert metrics["shock_events_count"] == 1
    assert metrics["market_ws_connected"] is True
