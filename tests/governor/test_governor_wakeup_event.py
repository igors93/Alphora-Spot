from types import SimpleNamespace

from governor.cycle.wakeup import MarketWakeup


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


def _wakeup(**config_overrides):
    config = {
        "loop_interval_seconds": 30.0,
        "governor_wakeup_enabled": True,
        "governor_wakeup_cooldown_seconds": 1.0,
    }
    config.update(config_overrides)
    return MarketWakeup(config=SimpleNamespace(**config))


def test_market_shock_wakeup_sets_event():
    wakeup = _wakeup()

    wakeup.on_market_shock(
        {
            "symbol": "btcusdt",
            "reason": "DROP_80BPS_IN_1S",
        }
    )

    assert wakeup.event.is_set() is True
    assert wakeup.count == 1
    assert wakeup.last_symbol == "BTCUSDT"
    assert wakeup.last_reason == "DROP_80BPS_IN_1S"


def test_market_shock_wakeup_respects_global_cooldown():
    wakeup = _wakeup(governor_wakeup_cooldown_seconds=10.0)

    wakeup.on_market_shock({"symbol": "BTCUSDT", "reason": "FIRST"})
    wakeup.clear()
    wakeup.on_market_shock({"symbol": "ETHUSDT", "reason": "SECOND"})

    assert wakeup.event.is_set() is False
    assert wakeup.count == 1
    assert wakeup.suppressed_count == 1


def test_wait_until_next_cycle_consumes_wakeup_without_sleeping_full_interval():
    wakeup = _wakeup(loop_interval_seconds=30.0)
    journal = FakeJournal()
    heartbeats = []
    wakeup.on_market_shock(
        {
            "symbol": "BTCUSDT",
            "reason": "DROP_80BPS_IN_1S",
        }
    )

    wakeup.wait_for_next_cycle(journal=journal, heartbeat=lambda: heartbeats.append(1))

    assert wakeup.event.is_set() is False
    assert heartbeats == [1]

    event_type, data = journal.events[0]
    assert event_type == "governor_wakeup_event_consumed"
    assert data["status"] == "APPLIED"
    assert data["last_symbol"] == "BTCUSDT"
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_wait_until_next_cycle_sleeps_the_interval_when_wakeup_is_disabled():
    slept = []
    wakeup = MarketWakeup(
        config=SimpleNamespace(loop_interval_seconds=7.0, governor_wakeup_enabled=False),
        sleep=slept.append,
    )
    journal = FakeJournal()

    wakeup.wait_for_next_cycle(journal=journal, heartbeat=lambda: None)

    assert slept == [7.0]
    assert journal.events == []


def test_wakeup_can_be_disabled():
    wakeup = _wakeup(governor_wakeup_enabled=False)

    wakeup.on_market_shock(
        {
            "symbol": "BTCUSDT",
            "reason": "DROP_80BPS_IN_1S",
        }
    )

    assert wakeup.event.is_set() is False
    assert wakeup.count == 0


def test_governor_hub_callback_is_the_wakeup_of_the_same_governor(config_factory):
    from config.symbol_context import symbol_config_from_app_config
    from governor.alphora_governor import AlphoraGovernor
    from tests.governor.test_alphora_governor import FakeClient

    config = config_factory(symbols=("BTCUSDT",))
    governor = AlphoraGovernor(
        config=config,
        client=FakeClient(),
        symbol_configs=[symbol_config_from_app_config(config)],
    )

    assert governor.market_data_hub is not None
    governor.market_data_hub._notify_market_wakeup({"symbol": "BTCUSDT", "reason": "SHOCK"})

    assert governor.market_wakeup.event.is_set() is True
    assert governor.market_wakeup.last_symbol == "BTCUSDT"
