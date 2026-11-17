from types import SimpleNamespace

from governor.cycle.telemetry import CycleMetricsState, CycleTelemetry


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


class FakeLogger:
    def warning(self, *_args, **_kwargs):
        return None


class FakeHub:
    def health_snapshot(self, *, now=None):
        return {
            "enabled": True,
            "running": True,
            "connected": True,
            "known_prices": 1,
            "stale_symbols": 0,
            "largest_price_age_seconds": 0.1,
            "market_shock_pending_events": 0,
        }


def _telemetry(
    *,
    observability_enabled=True,
    interval_seconds=30.0,
    slow_cycle_ms=1500.0,
):
    return CycleTelemetry(
        config=SimpleNamespace(
            governor_observability_enabled=observability_enabled,
            governor_cycle_metrics_interval_seconds=interval_seconds,
            governor_slow_cycle_ms=slow_cycle_ms,
        ),
        journal=FakeJournal(),
        logger=FakeLogger(),
        market_data_hub=FakeHub(),
        state=CycleMetricsState(),
    )


def _record(telemetry, *, current_time, cycle_duration_ms=10.0, shock_events=None):
    telemetry.record_cycle_metrics(
        current_time=current_time,
        cycle_duration_ms=cycle_duration_ms,
        due_symbols=["BTCUSDT"],
        dirty_symbols=[],
        open_symbols=set(),
        shock_events=shock_events or [],
        symbol_timings=[
            {
                "symbol": "BTCUSDT",
                "status": "OK",
                "duration_ms": 2.0,
            }
        ],
    )


def test_cycle_metrics_respects_interval_to_reduce_journal_noise():
    telemetry = _telemetry(interval_seconds=30.0)

    _record(telemetry, current_time=100.0)
    _record(telemetry, current_time=110.0)

    events = [
        event_type
        for event_type, _data in telemetry.journal.events
        if event_type == "governor_cycle_metrics"
    ]

    assert len(events) == 1


def test_cycle_metrics_records_after_interval():
    telemetry = _telemetry(interval_seconds=30.0)

    _record(telemetry, current_time=100.0)
    _record(telemetry, current_time=131.0)

    events = [
        event_type
        for event_type, _data in telemetry.journal.events
        if event_type == "governor_cycle_metrics"
    ]

    assert len(events) == 2


def test_cycle_metrics_always_records_when_market_shock_exists():
    telemetry = _telemetry(interval_seconds=30.0)

    _record(telemetry, current_time=100.0)
    _record(
        telemetry,
        current_time=110.0,
        shock_events=[
            {
                "symbol": "BTCUSDT",
                "direction": "DROP",
                "severity": "HIGH",
            }
        ],
    )

    metrics = [
        data
        for event_type, data in telemetry.journal.events
        if event_type == "governor_cycle_metrics"
    ]

    assert len(metrics) == 2
    assert metrics[-1]["shock_events_count"] == 1


def test_cycle_metrics_records_slow_cycle_even_before_interval():
    telemetry = _telemetry(interval_seconds=30.0, slow_cycle_ms=100.0)

    _record(telemetry, current_time=100.0)
    _record(telemetry, current_time=110.0, cycle_duration_ms=150.0)

    metrics = [
        data
        for event_type, data in telemetry.journal.events
        if event_type == "governor_cycle_metrics"
    ]

    assert len(metrics) == 2
    assert metrics[-1]["cycle_duration_ms"] == 150.0


def test_open_position_cycles_receive_private_state_work_budget():
    telemetry = _telemetry(slow_cycle_ms=1500.0)

    assert telemetry.slow_cycle_threshold_ms(1) == 1500.0
    assert telemetry.slow_cycle_threshold_ms(1, open_symbols=1) == 3000.0
    assert telemetry.slow_cycle_threshold_ms(3, open_symbols=2) == 7500.0


def test_cycle_metrics_can_be_disabled():
    telemetry = _telemetry(observability_enabled=False)

    _record(
        telemetry,
        current_time=100.0,
        shock_events=[
            {
                "symbol": "BTCUSDT",
                "direction": "DROP",
                "severity": "HIGH",
            }
        ],
    )

    assert telemetry.journal.events == []
