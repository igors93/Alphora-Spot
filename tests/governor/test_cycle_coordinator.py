"""G7: the CycleCoordinator runs one cycle as explicit phases, from fakes only."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from governor.cycle import CycleCoordinator, CyclePorts
from governor.models import SymbolRuntimeStatus
from governor.services import DefensiveModeService


class _Evaluation:
    def __init__(self, symbol: str, *, terminal: bool) -> None:
        self.evaluation_input = SimpleNamespace(symbol_config=SimpleNamespace(symbol=symbol))
        self.terminal_result = object() if terminal else None


def _ports(calls: list, **overrides) -> CyclePorts:
    configs = [SimpleNamespace(symbol=symbol) for symbol in ("AAA", "BBB", "CCC", "DDD")]

    @contextmanager
    def cycle_scope(cycle_id):
        calls.append(f"scope:{cycle_id}")
        try:
            yield
        finally:
            calls.append("scope:exit")

    def prepare(symbol_config, now, *, context):
        calls.append(f"light:{symbol_config.symbol}")
        return _Evaluation(symbol_config.symbol, terminal=symbol_config.symbol == "CCC")

    values = {
        "next_cycle_id": lambda: "cycle-1",
        "cycle_scope": cycle_scope,
        "start_cycle_context": lambda context: calls.append("start"),
        "build_request_budget": lambda: None,
        "request_budget_summary": lambda context: {"enabled": False},
        "symbol_configs": configs,
        "collect_session_health": lambda label: calls.append(f"health:{label}"),
        "close_session": lambda context: calls.append("close"),
        "consume_symbol_control_requests": lambda: calls.append("control"),
        "prepare_session_baseline": lambda context: calls.append("baseline"),
        "refresh_candle_data_hub": None,
        "audit_open_positions": lambda *, context: {"AAA"},
        "drain_dirty_symbols": lambda now: [],
        "drain_shock_events": lambda: [],
        "record_shock_events": lambda events: None,
        "fast_lane_symbols_from_shocks": lambda **_kwargs: [],
        "market_health_snapshot": lambda now: {"enabled": False},
        "defensive_mode": DefensiveModeService(
            config=SimpleNamespace(governor_defensive_mode_enabled=False)
        ),
        "defensive_mode_active": lambda: False,
        "set_defensive_mode_active": lambda active: None,
        "max_symbols_per_cycle": 10,
        "rank_due_symbols": lambda now, **_kwargs: ["AAA", "BBB", "CCC", "DDD", "ZZZ"],
        "record_fast_lane_prioritization": lambda **_kwargs: None,
        "prepare_selected_snapshot": lambda context, symbols: calls.append("selected"),
        "is_stopped": lambda symbol: symbol == "BBB",
        "is_paused": lambda symbol: False,
        "set_disabled_snapshot": lambda config, status: calls.append(
            f"disabled:{config.symbol}:{status.value}"
        ),
        "prepare_symbol_evaluation": prepare,
        "record_symbol_error": lambda config, error: calls.append(f"error:{config.symbol}"),
        "prefetch_heavy_snapshots": lambda context, evaluations: calls.append(
            "prefetch:" + ",".join(e.evaluation_input.symbol_config.symbol for e in evaluations)
        ),
        "resume_symbol_evaluation": lambda evaluation: calls.append(
            f"heavy:{evaluation.evaluation_input.symbol_config.symbol}"
        ),
        "write_state": lambda *, context: calls.append("write_state"),
        "record_learning_recommendations": lambda: None,
        "record_learning_shadow_impact_report": lambda: None,
        "record_cycle_metrics": lambda **kwargs: calls.append(
            "metrics:" + ",".join(item["symbol"] for item in kwargs["symbol_timings"])
        ),
        "slow_cycle_threshold_ms": lambda *_args, **_kwargs: 0.0,
    }
    values.update(overrides)
    return CyclePorts(**values)


def test_cycle_runs_light_for_all_then_prefetch_then_heavy_in_order():
    calls: list = []

    CycleCoordinator(_ports(calls), clock=lambda: 1_000.0).run()

    assert calls == [
        "scope:cycle-1",
        "start",
        "control",
        "baseline",
        "selected",
        "light:AAA",
        "disabled:BBB:STOPPED",
        "light:CCC",
        "light:DDD",
        "prefetch:AAA,DDD",
        "heavy:AAA",
        "heavy:DDD",
        "write_state",
        "metrics:BBB,CCC,ZZZ,AAA,DDD",
        "health:cycle_end",
        "close",
        "scope:exit",
    ]


def test_cycle_timings_follow_light_skips_then_heavy_results():
    calls: list = []

    CycleCoordinator(_ports(calls), clock=lambda: 1_000.0).run()

    metrics = next(item for item in calls if item.startswith("metrics:"))
    # Skips and light-terminal symbols are timed during the light phase; the
    # symbols that reach the heavy phase are timed when it finishes.
    assert metrics.removeprefix("metrics:").split(",") == ["BBB", "CCC", "ZZZ", "AAA", "DDD"]


def test_cycle_uses_the_clock_only_without_an_explicit_now():
    seen: list = []
    calls: list = []
    ports = _ports(calls, drain_dirty_symbols=lambda now: seen.append(now) or [])

    CycleCoordinator(ports, clock=lambda: 42.0).run()
    CycleCoordinator(ports, clock=lambda: 42.0).run(now=7.0)

    assert seen == [42.0, 7.0]


def test_cycle_closes_the_session_and_restores_the_scope_on_error():
    calls: list = []

    def explode(**_kwargs):
        raise RuntimeError("write failed")

    ports = _ports(calls, write_state=explode)

    with pytest.raises(RuntimeError):
        CycleCoordinator(ports, clock=lambda: 1.0).run()

    assert calls[-3:] == ["health:cycle_end", "close", "scope:exit"]


def test_light_phase_errors_are_isolated_per_symbol():
    calls: list = []

    def prepare(symbol_config, now, *, context):
        if symbol_config.symbol == "AAA":
            raise ValueError("bad data")
        return _Evaluation(symbol_config.symbol, terminal=False)

    ports = _ports(
        calls,
        prepare_symbol_evaluation=prepare,
        is_stopped=lambda symbol: False,
        is_paused=lambda symbol: symbol == "CCC",
    )

    CycleCoordinator(ports, clock=lambda: 1.0).run()

    assert "error:AAA" in calls
    assert f"disabled:CCC:{SymbolRuntimeStatus.PAUSED.value}" in calls
    assert "prefetch:BBB,DDD" in calls
