"""G5: the light (1) and heavy (9) context stages, built from fakes only."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from domain.models import MarketSnapshot
from governor.cycle_context import CycleContext
from governor.evaluation import SymbolRuntimeBoard
from governor.models import SymbolRuntimeStatus
from governor.services import AIGateService
from governor.stages.context import (
    HeavyContextPorts,
    HeavyContextStage,
    LightContextPorts,
    LightContextStage,
    UserDataCachePorts,
)
from governor.symbol_evaluation_pipeline import LightEvaluation
from market_data.market_snapshots import HeavyMarketSnapshot, LightMarketSnapshot
from scanner.priority_scheduler import ScheduledSymbol
from tests.governor.stages.fakes import FakeJournal, make_evaluation, make_position, make_score


def _market() -> MarketSnapshot:
    return MarketSnapshot(symbol="BTCUSDT", price=Decimal("100"), prices=[Decimal("100")])


class _LightRouter:
    def get_light_snapshot(self, symbol, *, now, operational_snapshot=None):
        return LightMarketSnapshot(
            symbol=symbol, market=_market(), timestamp=now, data_age_seconds=0.5, stale=False
        )


class _Strategy:
    global_strategy_name = "test_strategy"

    def __init__(self, *, run: bool = True) -> None:
        self.run = run
        self.marked: list[str] = []

    def should_evaluate(self, symbol, market, *, symbol_config):
        return self.run, "" if self.run else "WAIT_NEXT_CANDLE"

    def score(self, symbol_config, market, position):
        return make_score(action="OPEN_LONG", score=90, reason="signal")

    def mark_evaluated(self, symbol, market):
        self.marked.append(symbol)


class _Scheduler:
    def update(self, score, now):
        return ScheduledSymbol(
            symbol=score.symbol,
            score=score.score,
            priority=score.priority,
            next_check_at=now + 5,
            last_checked_at=now,
            data_age_seconds=0.0,
            stale=False,
        )


def _light_ports(snapshots, *, strategy, journal, transitions, trend=None) -> LightContextPorts:
    return LightContextPorts(
        router=_LightRouter(),
        remember_operational_snapshot=lambda _context: None,
        trend_for_market=lambda **_kwargs: trend,
        attach_trend=lambda market, _trend: replace(market, indicators={"trend_context": "T"}),
        record_trend=lambda _trend: journal.append("trend_context_evaluated"),
        position_for_symbol=lambda _config, context=None: make_position(),
        runtime_state_for=None,
        strategy=strategy,
        skip_score=lambda **kwargs: make_score(
            action="NO_ACTION", score=0, reason=kwargs["reason"]
        ),
        warmup_interval_seconds=lambda: 30.0,
        ai_gate=AIGateService(config=SimpleNamespace(), journal=journal, strategy=strategy),
        scheduler=_Scheduler(),
        score_journal=journal,
        trace_payload=lambda: {"cycle_id": "cycle-1"},
        execution_mode="DRY_RUN",
        environment="TESTNET",
        record_learning_advisory=lambda **_kwargs: None,
        board=SymbolRuntimeBoard(snapshots, strategy=strategy),
        record_transition=transitions,
    )


def test_light_context_scores_the_symbol_and_publishes_the_board(transitions, tmp_path):
    journal = FakeJournal()
    snapshots: dict = {}
    strategy = _Strategy()
    stage = LightContextStage(
        _light_ports(snapshots, strategy=strategy, journal=journal, transitions=transitions)
    )
    evaluation = make_evaluation()
    evaluation.evaluation_input.symbol_config.runtime_dir = tmp_path

    assert stage(evaluation) is None

    assert stage.name == "light_snapshot_score"
    assert evaluation.score is not None and evaluation.score.reason == "signal"
    assert strategy.marked == ["BTCUSDT"]
    assert isinstance(evaluation.light_evaluation, LightEvaluation)
    assert evaluation.status is SymbolRuntimeStatus.READY
    assert snapshots["BTCUSDT"].last_price == "100"
    assert snapshots["BTCUSDT"].next_check_at == 1_005.0
    assert journal.types() == ["scanner_score"]
    assert transitions.calls[0]["reason"] == "LIGHT_EVALUATION_STARTED"


def test_light_context_uses_the_skip_score_and_attaches_the_trend(transitions, tmp_path):
    journal = FakeJournal()
    strategy = _Strategy(run=False)
    stage = LightContextStage(
        _light_ports({}, strategy=strategy, journal=journal, transitions=transitions, trend="T")
    )
    evaluation = make_evaluation()
    evaluation.evaluation_input.symbol_config.runtime_dir = tmp_path

    stage(evaluation)

    assert evaluation.score is not None and evaluation.score.reason == "WAIT_NEXT_CANDLE"
    assert strategy.marked == []
    assert evaluation.light is not None
    assert evaluation.light.market.indicators == {"trend_context": "T"}
    assert journal.types() == ["trend_context_evaluated", "scanner_score"]


def _heavy_snapshot(position) -> HeavyMarketSnapshot:
    return HeavyMarketSnapshot(
        symbol="BTCUSDT",
        market=_market(),
        mark_price=Decimal("100"),
        exchange_rules=None,
        position=position,
        open_orders=[],
        open_algo_orders=[],
        account_info={},
        timestamp=1_000.0,
        data_age_seconds=0.2,
        stale=False,
    )


class _HeavyRouter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get_heavy_snapshot(self, symbol, **kwargs):
        self.calls.append(kwargs)
        return _heavy_snapshot(make_position())


def _user_data(recorded: list[str], *, cached_position=None) -> UserDataCachePorts:
    def recorder(name, value=None):
        def call(**_kwargs):
            recorded.append(name)
            return value

        return call

    return UserDataCachePorts(
        record_stale_guard=recorder("stale_guard"),
        record_usage_shadow=recorder("usage"),
        record_assisted_plan_shadow=recorder("assisted_plan"),
        cached_position=recorder("cached_position", cached_position),
        cached_account_info=recorder("cached_account"),
        cached_open_orders=recorder("cached_orders"),
        record_runtime_metrics=recorder("runtime_metrics"),
        record_shadow_audit=recorder("shadow_audit"),
        record_reconciliation=recorder("reconciliation"),
    )


def _heavy_ports(router, journal, transitions, recorded, *, sync=None) -> HeavyContextPorts:
    return HeavyContextPorts(
        router=router,
        prefetched_snapshot_max_age_seconds=3.0,
        prepare_symbol_snapshot=None,
        user_data=_user_data(recorded),
        journal=journal,
        trace_payload=lambda: {},
        strategy_name=lambda: "test_strategy",
        remember_operational_snapshot=lambda _context: None,
        sync_position=sync,
        attach_trend=lambda market, _trend: market,
        record_adaptive_management=lambda **_kwargs: recorded.append("adaptive"),
        record_transition=transitions,
    )


def _light_evaluation(evaluation):
    return LightEvaluation(
        evaluation_input=evaluation.evaluation_input,
        light=LightMarketSnapshot(
            symbol="BTCUSDT", market=_market(), timestamp=1.0, data_age_seconds=0.1, stale=False
        ),
        position=make_position(),
        runtime_state=None,
        score=make_score(),
        scheduled=ScheduledSymbol(
            symbol="BTCUSDT",
            score=90,
            priority=make_score().priority,
            next_check_at=5.0,
            last_checked_at=1.0,
            data_age_seconds=0.0,
            stale=False,
        ),
        status=SymbolRuntimeStatus.READY,
        warmup=False,
    )


def test_heavy_context_resolves_just_in_time_and_syncs_the_position(journal, transitions):
    recorded: list[str] = []
    router = _HeavyRouter()
    synced = make_position(open_position=True)
    stage = HeavyContextStage(
        _heavy_ports(router, journal, transitions, recorded, sync=lambda _config, _pos: synced)
    )
    context = CycleContext(now=1_000.0)
    evaluation = make_evaluation(
        cycle_context=context, score=make_score(), position=make_position()
    )
    evaluation.light_evaluation = _light_evaluation(evaluation)

    assert stage(evaluation) is None

    assert stage.name == "heavy_snapshot"
    assert router.calls and router.calls[0]["cached_position"] is None
    assert evaluation.heavy is not None and evaluation.heavy.position is synced
    assert evaluation.router_heavy is not None and evaluation.router_heavy.position is not synced
    assert context.positions["BTCUSDT"] is synced
    assert evaluation.heavy_evaluation is not None
    assert journal.types() == ["heavy_check_started"]
    assert recorded[-3:] == ["shadow_audit", "reconciliation", "adaptive"]
    assert transitions.calls[-1]["reason"] == "HEAVY_SNAPSHOT_EVALUATED"


def test_heavy_context_uses_the_prefetched_snapshot(journal, transitions):
    recorded: list[str] = []
    router = _HeavyRouter()
    stage = HeavyContextStage(_heavy_ports(router, journal, transitions, recorded))
    context = CycleContext(now=1_000.0)
    prefetched = _heavy_snapshot(make_position())
    context.take_prefetched_heavy_snapshot = lambda symbol, *, now, max_age_seconds: prefetched
    evaluation = make_evaluation(
        cycle_context=context, score=make_score(), position=make_position()
    )

    stage(evaluation)

    assert router.calls == []
    assert evaluation.router_heavy is prefetched
    assert journal.types() == ["heavy_check_started", "heavy_snapshot_prefetch_used"]
