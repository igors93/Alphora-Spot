from dataclasses import replace

from tests.fakes import FakeFuturesClient
from tests.governor.factory import build_governor_for_tests


def _governor(config_factory, **overrides):
    values = {
        "market_ws_enabled": True,
        "market_shock_detector_enabled": True,
        "market_shock_window_seconds": 7.0,
        "market_shock_move_bps_threshold": 80.0,
        "market_shock_critical_bps_threshold": 200.0,
        "market_shock_cooldown_seconds": 15.0,
    }
    values.update(overrides)
    config = replace(config_factory(), **values)
    return build_governor_for_tests(config=config, client=FakeFuturesClient())


def test_governor_builds_market_data_hub_with_shock_config(config_factory):
    governor = _governor(config_factory)
    hub = governor.market_data_hub

    assert hub is not None
    assert hub.market_shock_detector is not None
    assert hub.market_shock_detector.window_seconds == 7.0
    assert hub.market_shock_detector.move_bps_threshold == 80.0
    assert hub.market_shock_detector.critical_bps_threshold == 200.0
    assert hub.market_shock_detector.cooldown_seconds == 15.0


def test_governor_can_disable_market_shock_detector(config_factory):
    governor = _governor(config_factory, market_shock_detector_enabled=False)
    hub = governor.market_data_hub

    assert hub is not None
    assert hub.market_shock_detector is None


def test_governor_builds_market_data_hub_with_wakeup_callback(config_factory):
    governor = _governor(config_factory)
    hub = governor.market_data_hub

    assert hub is not None
    assert callable(hub.market_wakeup_callback)
