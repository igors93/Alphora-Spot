from types import SimpleNamespace

from governor.opportunity_ranker import OpportunityRanker
from governor.services import FastLaneService, OpportunityService
from scanner.priority_scheduler import ScheduledSymbol
from scanner.signal_score import SignalPriority


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


class FakeLogger:
    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None


class FakeScheduler:
    def __init__(self, items, due=None):
        self.items = items
        self.due = due or []

    def due_symbols(self, _now):
        return list(self.due)

    def all_symbols(self):
        return list(self.items)


def _scheduled(
    symbol,
    *,
    score=10,
    priority=SignalPriority.COLD,
    next_check_at=999.0,
    last_checked_at=0.0,
):
    return ScheduledSymbol(
        symbol=symbol,
        score=score,
        priority=priority,
        next_check_at=next_check_at,
        last_checked_at=last_checked_at,
        data_age_seconds=0.0,
        stale=False,
        reason="test",
    )


def _opportunity_service(scheduler):
    return OpportunityService(
        config=SimpleNamespace(loop_interval_seconds=3.0),
        scheduler=scheduler,
        ranker=OpportunityRanker(),
        portfolio_settings=SimpleNamespace(correlation_groups={}),
        journal=FakeJournal(),
        logger=FakeLogger(),
    )


def _fast_lane(**config_overrides):
    config = {
        "loop_interval_seconds": 3.0,
        "governor_fast_lane_enabled": True,
        "governor_fast_lane_max_symbols_per_cycle": 3,
        "governor_fast_lane_cooldown_seconds": 2.0,
    }
    config.update(config_overrides)
    return FastLaneService(
        config=SimpleNamespace(**config),
        journal=FakeJournal(),
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"],
        stopped_symbols=set(),
        paused_symbols=set(),
        last_selected_at={},
    )


def test_fast_lane_extracts_unique_limited_symbols_from_shock_events():
    fast_lane = _fast_lane(governor_fast_lane_max_symbols_per_cycle=2)

    symbols = fast_lane.symbols_from_shocks(
        shock_events=[
            {"symbol": "btcusdt", "severity": "HIGH"},
            {"symbol": "ETHUSDT", "severity": "MEDIUM"},
            {"symbol": "SOLUSDT", "severity": "HIGH"},
            {"symbol": "BTCUSDT", "severity": "HIGH"},
            {"symbol": "UNKNOWN", "severity": "HIGH"},
        ],
        now=100.0,
    )

    assert symbols == ["BTCUSDT", "ETHUSDT"]


def test_fast_lane_respects_cooldown_per_symbol():
    fast_lane = _fast_lane(governor_fast_lane_cooldown_seconds=5.0)

    first = fast_lane.symbols_from_shocks(
        shock_events=[{"symbol": "BTCUSDT", "severity": "HIGH"}],
        now=100.0,
    )
    second = fast_lane.symbols_from_shocks(
        shock_events=[{"symbol": "BTCUSDT", "severity": "HIGH"}],
        now=102.0,
    )
    third = fast_lane.symbols_from_shocks(
        shock_events=[{"symbol": "BTCUSDT", "severity": "HIGH"}],
        now=106.0,
    )

    assert first == ["BTCUSDT"]
    assert second == []
    assert third == ["BTCUSDT"]


def test_fast_lane_can_be_disabled():
    fast_lane = _fast_lane(governor_fast_lane_enabled=False)

    symbols = fast_lane.symbols_from_shocks(
        shock_events=[{"symbol": "BTCUSDT", "severity": "HIGH"}],
        now=100.0,
    )

    assert symbols == []


def test_rank_due_symbols_prioritizes_fast_lane_after_open_positions():
    service = _opportunity_service(
        FakeScheduler(
            [
                _scheduled("BTCUSDT", score=10, priority=SignalPriority.COLD),
                _scheduled("ETHUSDT", score=80, priority=SignalPriority.HOT),
                _scheduled("SOLUSDT", score=20, priority=SignalPriority.COLD),
            ],
            due=["SOLUSDT"],
        )
    )

    ranked = service.rank_due_symbols(
        100.0,
        open_symbols={"ETHUSDT"},
        dirty_symbols=[],
        fast_lane_symbols=["BTCUSDT"],
    )

    assert ranked[:3] == ["ETHUSDT", "BTCUSDT", "SOLUSDT"]


def test_fast_lane_symbol_is_included_even_when_not_due_or_dirty():
    service = _opportunity_service(
        FakeScheduler(
            [
                _scheduled("BTCUSDT", score=10, priority=SignalPriority.COLD),
                _scheduled("SOLUSDT", score=20, priority=SignalPriority.COLD),
            ],
            due=["SOLUSDT"],
        )
    )

    ranked = service.rank_due_symbols(
        100.0,
        open_symbols=set(),
        dirty_symbols=[],
        fast_lane_symbols=["BTCUSDT"],
    )

    assert "BTCUSDT" in ranked
    assert ranked[0] == "BTCUSDT"


def test_fast_lane_prioritization_records_audit_event():
    fast_lane = _fast_lane()

    fast_lane.record_prioritization(
        current_time=100.0,
        fast_lane_symbols=["BTCUSDT"],
        selected_symbols=["BTCUSDT", "ETHUSDT"],
        shock_events=[{"symbol": "BTCUSDT", "severity": "HIGH"}],
    )

    assert fast_lane.journal.events

    event_type, data = fast_lane.journal.events[0]

    assert event_type == "governor_fast_lane_prioritized"
    assert data["status"] == "APPLIED"
    assert data["fast_lane_symbols"] == ["BTCUSDT"]
    assert data["selected_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False
