from scanner.priority_scheduler import PriorityScheduler, ScheduledSymbol
from scanner.signal_score import SignalPriority, SignalScore


def test_cold_next_check_is_later_than_hot():
    scheduler = PriorityScheduler()
    cold = scheduler.update(SignalScore("BTCUSDT", 10, reason="cold"), now=100)
    hot = scheduler.update(SignalScore("ETHUSDT", 80, reason="hot"), now=100)

    assert cold.priority is SignalPriority.COLD
    assert hot.priority is SignalPriority.HOT
    assert cold.next_check_at > hot.next_check_at


def test_ready_next_check_is_immediate():
    scheduled = PriorityScheduler().update(SignalScore("BTCUSDT", 95, reason="ready"), now=10)

    assert scheduled.next_check_at == 10


def test_due_symbols_returns_expired_symbols():
    scheduler = PriorityScheduler()
    scheduler.update(SignalScore("BTCUSDT", 80, reason="hot"), now=100)
    scheduler.update(SignalScore("ETHUSDT", 10, reason="cold"), now=100)

    assert scheduler.due_symbols(106) == ["BTCUSDT"]


def test_top_symbols_orders_by_score_and_summary_counts():
    scheduler = PriorityScheduler()
    scheduler.update(SignalScore("BTCUSDT", 10, reason="cold"), now=100)
    scheduler.update(SignalScore("ETHUSDT", 95, reason="ready"), now=100)
    scheduler.update(SignalScore("SOLUSDT", 45, reason="warm"), now=100)

    top = scheduler.top_symbols(2)

    assert [item.symbol for item in top] == ["ETHUSDT", "SOLUSDT"]
    assert scheduler.summary() == {"COLD": 1, "WARM": 1, "HOT": 0, "READY": 1}


def test_top_symbols_uses_priority_before_raw_score():
    scheduler = PriorityScheduler()
    scheduler._symbols = {
        "COLDUSDT": ScheduledSymbol(
            "COLDUSDT",
            score=99,
            priority=SignalPriority.COLD,
            next_check_at=100,
            last_checked_at=90,
            data_age_seconds=0,
            stale=False,
        ),
        "HOTUSDT": ScheduledSymbol(
            "HOTUSDT",
            score=70,
            priority=SignalPriority.HOT,
            next_check_at=100,
            last_checked_at=90,
            data_age_seconds=0,
            stale=False,
        ),
    }

    top = scheduler.top_symbols(2)

    assert [item.symbol for item in top] == ["HOTUSDT", "COLDUSDT"]


def test_stale_is_marked_when_data_age_exceeds_limit():
    scheduler = PriorityScheduler(max_data_age_seconds=5)
    scheduled = scheduler.update(
        SignalScore("BTCUSDT", 50, reason="old", data_age_seconds=6),
        now=100,
    )

    assert scheduled.stale is True
