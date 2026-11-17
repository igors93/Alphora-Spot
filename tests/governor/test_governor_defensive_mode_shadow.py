from types import SimpleNamespace

from governor.services import DefensiveModeService


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


def _service(**config_overrides):
    config = {
        "governor_defensive_mode_enabled": True,
        "governor_defensive_mode_shadow_only": True,
        "governor_defensive_mode_min_shock_symbols": 2,
        "governor_defensive_mode_max_stale_symbols": 0,
        "governor_defensive_mode_max_price_age_seconds": 5.0,
        "governor_defensive_mode_budget_pressure_enabled": True,
    }
    config.update(config_overrides)

    return DefensiveModeService(config=SimpleNamespace(**config), journal=FakeJournal())


def _health(**overrides):
    data = {
        "enabled": True,
        "running": True,
        "connected": True,
        "stale_symbols": 0,
        "largest_price_age_seconds": 0.1,
    }
    data.update(overrides)
    return data


def test_governor_evaluates_defensive_mode_shadow():
    service = _service()

    evaluation = service.evaluate_shadow(
        shock_events=[
            {"symbol": "BTCUSDT"},
            {"symbol": "ETHUSDT"},
        ],
        fast_lane_symbols=["BTCUSDT", "ETHUSDT"],
        market_health=_health(),
        request_budget_summary={
            "enabled": True,
            "denied_heavy_checks": 0,
        },
        open_symbols=set(),
        dirty_symbols=[],
    )

    assert evaluation is not None
    assert evaluation.mode == "MARKET_SHOCK"
    assert evaluation.shadow_only is True


def test_governor_records_defensive_mode_shadow_event():
    service = _service()

    evaluation = service.evaluate_shadow(
        shock_events=[{"symbol": "BTCUSDT"}],
        fast_lane_symbols=["BTCUSDT"],
        market_health=_health(connected=False),
        request_budget_summary={
            "enabled": True,
            "denied_heavy_checks": 0,
        },
        open_symbols=set(),
        dirty_symbols=[],
    )

    active = service.record_shadow(evaluation, previously_active=False)

    assert active is True

    assert service.journal.events

    event_type, data = service.journal.events[0]

    assert event_type == "governor_defensive_mode_shadow"
    assert data["mode"] == "DEFENSIVE_SHADOW"
    assert data["shadow_only"] is True
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False
    assert data["would_block_new_entries"] is True
    assert data["would_allow_closing"] is True


def test_governor_defensive_mode_can_be_disabled():
    service = _service(governor_defensive_mode_enabled=False)

    evaluation = service.evaluate_shadow(
        shock_events=[{"symbol": "BTCUSDT"}],
        fast_lane_symbols=["BTCUSDT"],
        market_health=_health(connected=False),
        request_budget_summary={
            "enabled": True,
            "denied_heavy_checks": 1,
        },
        open_symbols=set(),
        dirty_symbols=[],
    )

    assert evaluation is None
    assert DefensiveModeService.summary(evaluation)["enabled"] is False
