from types import SimpleNamespace

from governor.opportunity_ranker import OpportunityRanker
from governor.services import OpportunityService
from scanner.priority_scheduler import ScheduledSymbol
from scanner.signal_score import SignalPriority


def _scheduled(symbol, *, score, priority):
    return ScheduledSymbol(
        symbol=symbol,
        score=score,
        priority=priority,
        next_check_at=100.0,
        last_checked_at=0.0,
        data_age_seconds=0.0,
        stale=False,
        reason="test",
    )


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


class FakeLogger:
    def info(self, *_args, **_kwargs):
        return None


class FakeScheduler:
    def __init__(self, items):
        self.items = list(items)

    def due_symbols(self, _now):
        return [item.symbol for item in self.items]

    def all_symbols(self):
        return list(self.items)


def test_opportunity_ranker_orders_ready_above_hot_when_equal():
    ranker = OpportunityRanker()

    ready = ranker.score(symbol="BTCUSDT", base_score=80, priority=SignalPriority.READY)
    hot = ranker.score(symbol="ETHUSDT", base_score=80, priority=SignalPriority.HOT)

    assert ready.opportunity_score > hot.opportunity_score


def test_opportunity_ranker_prioritizes_open_position_attention():
    ranker = OpportunityRanker()

    open_position = ranker.score(
        symbol="BTCUSDT",
        base_score=30,
        priority=SignalPriority.COLD,
        position_open=True,
    )
    flat_ready = ranker.score(
        symbol="ETHUSDT",
        base_score=40,
        priority=SignalPriority.WARM,
    )

    assert open_position.opportunity_score > flat_ready.opportunity_score


def test_opportunity_ranker_quarantine_penalty_reduces_without_blocking():
    ranker = OpportunityRanker()

    normal = ranker.score(symbol="BTCUSDT", base_score=70, priority=SignalPriority.HOT)
    cooldown = ranker.score(
        symbol="BTCUSDT",
        base_score=70,
        priority=SignalPriority.HOT,
        quarantine_level="COOLDOWN",
    )

    assert cooldown.opportunity_score < normal.opportunity_score
    assert cooldown.opportunity_score > 0
    assert cooldown.execution_changed is False
    assert cooldown.order_mutation is False


def test_governor_opportunity_ranking_shadow_does_not_change_order():
    journal = FakeJournal()
    service = OpportunityService(
        config=SimpleNamespace(
            loop_interval_seconds=1.0,
            governor_opportunity_ranking_enabled=False,
        ),
        scheduler=FakeScheduler(
            [
                _scheduled("BTCUSDT", score=95, priority=SignalPriority.READY),
                _scheduled("ETHUSDT", score=89, priority=SignalPriority.HOT),
            ]
        ),
        ranker=OpportunityRanker(),
        portfolio_settings=SimpleNamespace(correlation_groups={}),
        journal=journal,
        logger=FakeLogger(),
    )

    ranked = service.rank_due_symbols(
        100.0,
        open_symbols=set(),
        dirty_symbols=[],
        fast_lane_symbols=[],
    )

    assert ranked == ["BTCUSDT", "ETHUSDT"]
    event_type, data = journal.events[0]
    assert event_type == "governor_opportunity_ranking_shadow"
    assert data["status"] == "SHADOW"
    assert data["execution_changed"] is False
