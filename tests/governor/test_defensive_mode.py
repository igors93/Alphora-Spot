from governor.defensive_mode import DefensiveModeEvaluator


def _health(**overrides):
    data = {
        "enabled": True,
        "running": True,
        "connected": True,
        "stale_symbols": 0,
        "largest_price_age_seconds": 0.2,
    }
    data.update(overrides)
    return data


def _budget(**overrides):
    data = {
        "enabled": True,
        "denied_heavy_checks": 0,
    }
    data.update(overrides)
    return data


def test_defensive_mode_normal_when_no_risk_signals():
    evaluator = DefensiveModeEvaluator()

    evaluation = evaluator.evaluate(
        shock_events=[],
        fast_lane_symbols=[],
        market_health=_health(),
        request_budget_summary=_budget(),
        open_symbols=set(),
        dirty_symbols=[],
    )

    assert evaluation.mode == "NORMAL"
    assert evaluation.would_block_new_entries is False
    assert evaluation.shadow_only is True


def test_defensive_mode_volatile_on_single_symbol_shock():
    evaluator = DefensiveModeEvaluator(min_shock_symbols=2)

    evaluation = evaluator.evaluate(
        shock_events=[{"symbol": "BTCUSDT"}],
        fast_lane_symbols=["BTCUSDT"],
        market_health=_health(),
        request_budget_summary=_budget(),
        open_symbols=set(),
        dirty_symbols=[],
    )

    assert evaluation.mode == "VOLATILE"
    assert evaluation.would_block_new_entries is False
    assert evaluation.would_reduce_new_entries is True
    assert evaluation.would_prioritize_protection is True


def test_defensive_mode_market_shock_on_multiple_symbols():
    evaluator = DefensiveModeEvaluator(min_shock_symbols=2)

    evaluation = evaluator.evaluate(
        shock_events=[
            {"symbol": "BTCUSDT"},
            {"symbol": "ETHUSDT"},
        ],
        fast_lane_symbols=["BTCUSDT", "ETHUSDT"],
        market_health=_health(),
        request_budget_summary=_budget(),
        open_symbols=set(),
        dirty_symbols=[],
    )

    assert evaluation.mode == "MARKET_SHOCK"
    assert evaluation.would_block_new_entries is True
    assert evaluation.would_allow_closing is True


def test_defensive_mode_data_unstable_when_websocket_disconnected():
    evaluator = DefensiveModeEvaluator()

    evaluation = evaluator.evaluate(
        shock_events=[],
        fast_lane_symbols=[],
        market_health=_health(connected=False),
        request_budget_summary=_budget(),
        open_symbols=set(),
        dirty_symbols=[],
    )

    assert evaluation.mode == "DATA_UNSTABLE"
    assert evaluation.reason == "MARKET_DATA_UNSTABLE"
    assert evaluation.would_block_new_entries is True


def test_defensive_mode_request_budget_pressure():
    evaluator = DefensiveModeEvaluator()

    evaluation = evaluator.evaluate(
        shock_events=[],
        fast_lane_symbols=[],
        market_health=_health(),
        request_budget_summary=_budget(denied_heavy_checks=2),
        open_symbols=set(),
        dirty_symbols=[],
    )

    assert evaluation.mode == "REQUEST_BUDGET_PRESSURE"
    assert evaluation.would_reduce_new_entries is True


def test_defensive_mode_defensive_shadow_on_shock_and_unstable_data():
    evaluator = DefensiveModeEvaluator()

    evaluation = evaluator.evaluate(
        shock_events=[{"symbol": "BTCUSDT"}],
        fast_lane_symbols=["BTCUSDT"],
        market_health=_health(connected=False),
        request_budget_summary=_budget(),
        open_symbols=set(),
        dirty_symbols=[],
    )

    assert evaluation.mode == "DEFENSIVE_SHADOW"
    assert evaluation.severity == "CRITICAL"
    assert evaluation.would_block_new_entries is True
    assert evaluation.shadow_only is True
