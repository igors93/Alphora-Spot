"""G5: the hot symbol decision (10) and the state reconciliation guard (11)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from domain.models import MarketSnapshot
from governor.hot_symbol_worker import GuardDecision, GuardOutcome
from governor.models import SymbolRuntimeSnapshot, SymbolRuntimeStatus
from governor.stages.decision import HotSymbolDecisionStage, StateReconciliationStage
from governor.symbol_state_machine import SymbolOperationalState
from operational_module_modes import MODULE_STATE_RECONCILIATION, ReconciliationMode
from tests.governor.stages.fakes import make_evaluation, make_position, make_score
from trading.intent import TradeIntent


def _heavy():
    return SimpleNamespace(
        market=MarketSnapshot(symbol="BTCUSDT", price=Decimal("100"), prices=[Decimal("100")]),
        position=make_position(),
        open_orders=[],
        open_algo_orders=[],
        stale=False,
    )


class _Strategy:
    global_strategy_name = "test_strategy"

    def intent(self, symbol_config, market, position, *, score=None):
        return TradeIntent(symbol="BTCUSDT", action="OPEN_LONG", reason="signal")


class _Worker:
    def __init__(self, decision: GuardDecision) -> None:
        self.decision = decision
        self.paused: list[bool] = []

    def evaluate(self, *, paused=False, **_kwargs) -> GuardDecision:
        self.paused.append(paused)
        return self.decision


def _hot_stage(outcomes, journal, transitions, decision, *, quarantine_events=None, paused=()):
    worker = _Worker(decision)
    stage = HotSymbolDecisionStage(
        outcomes=outcomes,
        strategy=_Strategy(),
        hot_worker=worker,
        risk_guard_for_symbol=lambda _config: None,
        paused_symbols=set(paused),
        journal=journal,
        trace_payload=lambda: {"cycle_id": "cycle-1"},
        record_transition=transitions,
        record_quarantine_event=lambda **kwargs: (
            quarantine_events.append(kwargs) if quarantine_events is not None else None
        ),
    )
    return stage, worker


def test_hot_decision_accepts_the_intent_and_fills_the_market_price(outcomes, journal, transitions):
    stage, worker = _hot_stage(
        outcomes, journal, transitions, GuardDecision(GuardOutcome.ALLOW, "HOT_CHECK_PASSED")
    )
    evaluation = make_evaluation(heavy=_heavy(), score=make_score(), position=make_position())

    assert stage(evaluation) is None
    assert evaluation.intent is not None and evaluation.intent.market_price == Decimal("100")
    assert journal.types() == ["heavy_check_passed"]
    assert transitions.calls[-1]["to_state"] is SymbolOperationalState.SIGNAL_CANDIDATE
    assert worker.paused == [False]


def test_hot_decision_blocks_and_feeds_the_quarantine(outcomes, journal, transitions, snapshots):
    snapshots["BTCUSDT"] = SymbolRuntimeSnapshot(
        symbol="BTCUSDT", enabled=True, status=SymbolRuntimeStatus.READY
    )
    events: list[dict] = []
    stage, worker = _hot_stage(
        outcomes,
        journal,
        transitions,
        GuardDecision(GuardOutcome.BLOCK, "STALE_DATA"),
        quarantine_events=events,
        paused={"BTCUSDT"},
    )
    evaluation = make_evaluation(heavy=_heavy(), score=make_score(), position=make_position())

    result = stage(evaluation)

    assert result is not None and result.stage == "HOT_SYMBOL_WORKER"
    assert result.classification == "operacional"
    assert journal.events[0][0] == "heavy_check_failed"
    assert journal.events[0][1]["severity"] == "WARNING"
    assert events[0]["event"] == "STALE_DATA_REPEATED"
    assert snapshots["BTCUSDT"].status is SymbolRuntimeStatus.BLOCKED
    assert worker.paused == [True]


def test_hot_decision_treats_strategy_hold_as_observation(outcomes, journal, transitions):
    stage, _ = _hot_stage(
        outcomes, journal, transitions, GuardDecision(GuardOutcome.BLOCK, "STRATEGY_HOLD")
    )
    evaluation = make_evaluation(heavy=_heavy(), score=make_score(), position=make_position())

    result = stage(evaluation)

    assert result is not None and result.classification == "observacao"
    assert journal.events[0][1]["severity"] == "INFO"
    assert transitions.calls == []


class _Engine:
    def __init__(self, *, block: bool) -> None:
        self.block = block
        self.modes: list = []

    def evaluate(self, snapshot, *, mode):
        self.modes.append(mode)
        return SimpleNamespace(
            mode=mode,
            status="BLOCKED" if self.block else "OK",
            issues=[],
            should_block_new_entries=self.block,
            affected_result=self.block,
            to_dict=lambda: {"diagnostics": {}},
        )


def _reconciliation(outcomes, journal, transitions, *, block, mode=ReconciliationMode.BLOCK):
    engine = _Engine(block=block)
    stage = StateReconciliationStage(
        outcomes=outcomes,
        engine=engine,
        module_modes={MODULE_STATE_RECONCILIATION: mode},
        journal=journal,
        trace_payload=lambda: {},
        record_transition=transitions,
    )
    return stage, engine


def _open_long_evaluation():
    return make_evaluation(
        heavy=_heavy(),
        intent=TradeIntent(symbol="BTCUSDT", action="OPEN_LONG", reason="signal"),
        position=make_position(),
    )


def test_reconciliation_blocks_new_entries_on_divergence(outcomes, journal, transitions):
    stage, engine = _reconciliation(outcomes, journal, transitions, block=True)
    evaluation = _open_long_evaluation()

    result = stage(evaluation)

    assert result is not None and result.reason == "RECONCILIATION_BLOCK"
    assert result.classification == "seguranca"
    assert "reconciliation_block_applied" in journal.types()
    assert evaluation.state_reconciliation_decision is not None
    assert transitions.calls[-1]["to_state"] is SymbolOperationalState.BLOCKED


def test_reconciliation_skips_closes_and_disabled_mode(outcomes, journal, transitions):
    stage, engine = _reconciliation(outcomes, journal, transitions, block=True)
    close = make_evaluation(
        heavy=_heavy(),
        intent=TradeIntent(symbol="BTCUSDT", action="CLOSE_POSITION", reason="exit"),
    )
    off_stage, off_engine = _reconciliation(
        outcomes, journal, transitions, block=True, mode=ReconciliationMode.OFF
    )

    assert stage(close) is None
    assert off_stage(_open_long_evaluation()) is None
    assert engine.modes == [] and off_engine.modes == []
