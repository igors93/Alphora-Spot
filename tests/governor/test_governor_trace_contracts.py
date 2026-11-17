import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from config.symbol_context import symbol_config_from_app_config
from domain.models import FuturesPosition, PositionDirection, TradeDecision
from governor.alphora_governor import AlphoraGovernor, CycleContext
from governor.events import (
    ATTENTION_LIGHT_ONLY,
    CLOSE_POSITION_REQUESTED,
    CLOSE_POSITION_SENT,
    ECONOMIC_GATE_EVALUATED_FINAL,
    EXECUTION_PAYLOAD_SENT,
    GOVERNOR_CYCLE_METRICS,
    GOVERNOR_EVALUATE_SYMBOL_FLOW_DECISION,
    PLANNED_EXIT_AFTER_INTELLIGENT_LAYERS,
    PLANNED_EXIT_BEFORE_INTELLIGENT_LAYERS,
    PORTFOLIO_TRADE_APPROVED,
    PRE_EXECUTION_GUARD_PASSED,
    PRE_EXECUTION_PAYLOAD_GUARD,
    RUNTIME_EFFECTIVE_CONFIG_SNAPSHOT,
    SCANNER_SCORE,
    SYMBOL_OPERATIONAL_STATE_TRANSITION,
    TRADE_APPROVED,
    TRADE_BLOCKED_BY_ECONOMIC_GATE,
    TRADE_BLOCKED_BY_FINAL_ECONOMIC_GATE,
    TRADE_BLOCKED_BY_PRE_EXECUTION_GUARD,
)
from governor.safe_journal import safe_append
from governor.services.intelligent_flow import IntelligentFlowService
from governor.services.trade_planning import TradePlanningService
from scanner.signal_score import SignalScore
from tests.governor.test_alphora_governor import (
    DecideHotWorker,
    FakeClient,
    FakeExecutionEngine,
    FakeHotWorker,
    FakeJournal,
    FakePortfolio,
    FakeRouter,
    FakeStrategyCoordinator,
    IntentStrategyCoordinator,
    LifecycleExecutionEngine,
    LifecycleHub,
)
from trading.intelligent_exit_activation import IntelligentExitActivationDecision
from trading.intent import TradeIntent


class RecordingTradeAuditRecorder:
    def __init__(self):
        self.open_candidates = []

    def record_open_candidate(self, **fields):
        self.open_candidates.append(fields)


class FailingJournal:
    def append(self, event_type, **payload):
        raise RuntimeError(f"failed {event_type}")


class RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append(message % args)


class CountingPortfolio(FakePortfolio):
    def __init__(self, allowed=True):
        super().__init__(allowed=allowed)
        self.calls = 0

    def evaluate_trade_intent(
        self, intent, symbol_contexts, account_snapshot, *, override_notional=None
    ):
        self.calls += 1
        return super().evaluate_trade_intent(
            intent, symbol_contexts, account_snapshot, override_notional=override_notional
        )


class CountingIntentCoordinator(FakeStrategyCoordinator):
    def __init__(self, scores=None):
        super().__init__(scores=scores)
        self.intent_calls = 0
        self.decide_calls = 0
        self.created_intent_id = ""

    def decide(self, symbol_config, market, position):
        self.decide_calls += 1
        return super().decide(symbol_config, market, position)

    def intent(self, symbol_config, market, position, *, score=None, decision=None):
        self.intent_calls += 1
        intent = TradeIntent(
            symbol=symbol_config.symbol,
            action="OPEN_LONG",
            confidence=95,
            score=score.score if score is not None else 95,
            priority=score.priority if score is not None else None,
            reason="single canonical intent",
            strategy_name=self.global_strategy_name,
            market_price=market.price,
            suggested_next_check_seconds=(
                score.suggested_next_check_seconds if score is not None else 0
            ),
            metadata={"notional": "50"},
        )
        self.created_intent_id = intent.intent_id
        return intent


def test_governor_start_records_runtime_effective_config_snapshot(config_factory):
    config = replace(config_factory(), recover_on_boot=False)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.control_requests.events_journal = governor.events_journal
    governor.execution_engine = LifecycleExecutionEngine()
    governor.market_data_hub = LifecycleHub()

    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "governor_stop.request").write_text("stop", encoding="utf-8")

    governor.start()

    snapshot = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == RUNTIME_EFFECTIVE_CONFIG_SNAPSHOT
    )

    expected_fields = {
        "execution_mode",
        "environment",
        "operational_mode",
        "symbols",
        "global_strategy",
        "strategy_runtime_mode",
        "strategy_state_mode",
        "economic_gate_enabled",
        "min_net_gain_bps",
        "min_net_reward_risk",
        "max_round_trip_cost_bps",
        "economic_exit_adjustment_enabled",
        "max_economic_take_bps",
        "max_economic_take_atr_multiplier",
        "dynamic_exit_enabled",
        "dynamic_sizing_enabled",
        "entry_execution_style",
        "limit_entry_enabled",
        "market_ws_enabled",
        "user_data_stream_enabled",
        "learning_store_enabled",
        "adaptive_manager_enabled",
        "adaptive_execution_enabled",
        "trade_audit_path",
        "trade_journal_path",
        "governor_events_path",
        "runtime_dir",
    }
    assert expected_fields <= set(snapshot)
    assert snapshot["symbols"] == [profile.symbol]
    assert snapshot["operational_mode"] == "GOVERNOR_SINGLE_SYMBOL"


def test_run_cycle_correlates_metrics_and_light_events(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.score_journal = FakeJournal()
    governor.execution_engine = FakeExecutionEngine()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: SignalScore(symbol=profile.symbol, score=10, reason="cold")}
    )

    governor.run_cycle(now=time.time() + 1)

    scanner = governor.score_journal.events[0][1]
    attention = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == ATTENTION_LIGHT_ONLY
    )
    decision = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == GOVERNOR_EVALUATE_SYMBOL_FLOW_DECISION
    )
    metrics = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == GOVERNOR_CYCLE_METRICS
    )

    assert scanner["cycle_id"]
    assert scanner["symbol_eval_id"]
    assert metrics["cycle_id"] == scanner["cycle_id"]
    assert attention["cycle_id"] == scanner["cycle_id"]
    assert decision["cycle_id"] == scanner["cycle_id"]
    assert attention["symbol_eval_id"] == scanner["symbol_eval_id"]
    assert decision["symbol_eval_id"] == scanner["symbol_eval_id"]


def test_evaluate_symbol_generates_symbol_eval_id_for_main_events(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.score_journal = FakeJournal()
    governor.execution_engine = FakeExecutionEngine()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: SignalScore(symbol=profile.symbol, score=10, reason="cold")}
    )

    governor._symbol_evaluator().evaluate(
        profile, time.time(), context=CycleContext(now=1.0, cycle_id="cycle-test")
    )

    scanner = next(
        data for event_type, data in governor.score_journal.events if event_type == SCANNER_SCORE
    )
    attention = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == ATTENTION_LIGHT_ONLY
    )

    assert scanner["cycle_id"] == "cycle-test"
    assert scanner["symbol_eval_id"].startswith("cycle-test:BTCUSDT:")
    assert attention["symbol_eval_id"] == scanner["symbol_eval_id"]


def test_execution_payload_sent_has_trace_contract_and_audit_trace(config_factory):
    config = replace(
        config_factory(),
        stop_loss_pct=0.003,
        take_profit_pct=0.004,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    audit_recorder = RecordingTradeAuditRecorder()
    governor.trade_audit_recorder = audit_recorder
    governor.events_journal = FakeJournal()
    governor.execution_engine = FakeExecutionEngine()
    governor.market_data_router = FakeRouter()
    ready = SignalScore(symbol=profile.symbol, score=95, action="OPEN_LONG", reason="ready")
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    payload = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == EXECUTION_PAYLOAD_SENT
    )

    expected_fields = {
        "symbol",
        "action",
        "trade_plan_id",
        "planned_quantity",
        "planned_stop_price",
        "planned_take_profit_price",
        "economic_gate_allowed",
        "expected_net_gain",
        "expected_net_loss",
        "reward_risk",
        "intent_id",
        "execution_command_id",
    }
    assert expected_fields <= set(payload)
    assert payload["cycle_id"]
    assert payload["symbol_eval_id"]
    assert payload["symbol"] == profile.symbol
    assert payload["action"] == "OPEN_LONG"
    assert payload["trade_plan_id"].startswith("tp_")
    assert payload["intent_id"]
    assert payload["execution_command_id"].startswith("ec_")
    assert payload["economic_gate_allowed"] is True

    audit = audit_recorder.open_candidates[0]
    assert audit["diagnostics"]["trace"]["cycle_id"] == payload["cycle_id"]
    assert audit["diagnostics"]["trace"]["symbol_eval_id"] == payload["symbol_eval_id"]


def test_single_intent_id_crosses_hot_plan_risk_guard_and_execution(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.execution_engine = FakeExecutionEngine()
    governor.market_data_router = FakeRouter()
    ready = SignalScore(symbol=profile.symbol, score=95, action="OPEN_LONG", reason="ready")
    coordinator = CountingIntentCoordinator({profile.symbol: ready})
    governor.strategy_coordinator = coordinator
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    assert coordinator.intent_calls == 1
    assert coordinator.decide_calls == 0
    intent_id = coordinator.created_intent_id
    assert intent_id

    events = dict(governor.events_journal.events)
    assert events["trade_intent_created"]["intent_id"] == intent_id
    assert events["heavy_check_passed"]["intent_id"] == intent_id
    assert events["trade_plan_shadow_created"]["intent_id"] == intent_id
    assert events["governor_decision"]["intent_id"] == intent_id
    assert events[PRE_EXECUTION_PAYLOAD_GUARD]["intent_id"] == intent_id
    assert events[EXECUTION_PAYLOAD_SENT]["intent_id"] == intent_id
    assert governor.execution_engine.execution_kwargs[0]["intent_id"] == intent_id


def test_pre_execution_guard_passes_before_execution_payload_and_state_is_observed(
    config_factory,
):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    ready = SignalScore(symbol=profile.symbol, score=95, action="OPEN_LONG", reason="ready")
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)
    execute_marker = "execution_engine_execute_called"

    class RecordingExecutionEngine(FakeExecutionEngine):
        def execute(self, **kwargs):
            governor.events_journal.append(execute_marker, symbol=profile.symbol)
            return super().execute(**kwargs)

    execution = RecordingExecutionEngine()
    governor.execution_engine = execution

    governor.run_cycle(now=time.time() + 1)

    event_types = [event_type for event_type, _data in governor.events_journal.events]
    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert PORTFOLIO_TRADE_APPROVED in event_types
    assert PRE_EXECUTION_PAYLOAD_GUARD in event_types
    assert PRE_EXECUTION_GUARD_PASSED in event_types
    assert TRADE_APPROVED in event_types
    assert EXECUTION_PAYLOAD_SENT in event_types
    assert event_types.index(PORTFOLIO_TRADE_APPROVED) < event_types.index(
        PRE_EXECUTION_PAYLOAD_GUARD
    )
    assert event_types.index(PRE_EXECUTION_GUARD_PASSED) < event_types.index(TRADE_APPROVED)
    assert event_types.index(TRADE_APPROVED) < event_types.index(EXECUTION_PAYLOAD_SENT)
    assert event_types.index(EXECUTION_PAYLOAD_SENT) < event_types.index(execute_marker)
    assert ECONOMIC_GATE_EVALUATED_FINAL in event_types

    portfolio_approved = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == PORTFOLIO_TRADE_APPROVED
    )
    assert portfolio_approved["execution_final"] is False
    assert portfolio_approved["execution_will_run"] is False
    assert portfolio_approved["requires_pre_execution_guard"] is True

    transitions = [
        data
        for event_type, data in governor.events_journal.events
        if event_type == SYMBOL_OPERATIONAL_STATE_TRANSITION
    ]
    assert transitions
    assert {item["to_state"] for item in transitions} >= {
        "LIGHT_EVALUATED",
        "HEAVY_REQUIRED",
        "HEAVY_EVALUATED",
        "EXECUTION_APPROVED",
        "ENTRY_SENT",
    }
    light_transition = next(item for item in transitions if item["to_state"] == "LIGHT_EVALUATED")
    assert light_transition["diagnostics"]["observational_only"] is True
    assert light_transition["diagnostics"]["evaluation_cycle_reset"] is True
    assert "previous_observational_state" in light_transition["diagnostics"]
    assert {item["symbol_eval_id"] for item in transitions} == {
        transitions[0]["symbol_eval_id"],
    }


def test_pre_execution_guard_blocks_open_without_required_dynamic_exit_payload(
    config_factory,
    monkeypatch,
):
    config = config_factory(dynamic_exit_enabled=True)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    governor.events_journal = FakeJournal()
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter()
    ready = SignalScore(symbol=profile.symbol, score=95, action="OPEN_LONG", reason="ready")
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    def missing_planned_exit(_trade_pipeline_result, *, action):
        assert action == "OPEN_LONG"
        return None, "TEST_MISSING_PLANNED_EXIT"

    monkeypatch.setattr(
        TradePlanningService,
        "planned_exit_for_execution",
        lambda _planning, *args, **kwargs: missing_planned_exit(*args, **kwargs),
    )

    governor.run_cycle(now=time.time() + 1)

    event_types = [event_type for event_type, _data in governor.events_journal.events]
    blocked = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == TRADE_BLOCKED_BY_PRE_EXECUTION_GUARD
    )
    flow = next(
        data
        for event_type, data in governor.events_journal.events
        if (
            event_type == GOVERNOR_EVALUATE_SYMBOL_FLOW_DECISION
            and data["stage"] == "PRE_EXECUTION_GUARD"
        )
    )
    assert blocked["reason"] == "PLANNED_EXIT_REQUIRED"
    assert blocked["motivo_pt"] == "A operacao de entrada nao possui plano de saida."
    assert blocked["como_corrigir"] == (
        "Verifique se o TradePlan gerou stop e take antes da execucao."
    )
    assert blocked["severity"] == "WARNING"
    assert flow["reason"] == "PLANNED_EXIT_REQUIRED"
    assert PORTFOLIO_TRADE_APPROVED in event_types
    assert TRADE_APPROVED not in event_types
    assert EXECUTION_PAYLOAD_SENT not in event_types
    assert execution.executed == []
    assert any(
        event_type == SYMBOL_OPERATIONAL_STATE_TRANSITION
        and data["to_state"] == "BLOCKED"
        and data["stage"] == "PRE_EXECUTION_GUARD"
        for event_type, data in governor.events_journal.events
    )


def test_close_position_guard_bypasses_open_payload_requirements(config_factory):
    now = time.time() + 1
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.5"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=now - 10,
    )
    config = config_factory(dynamic_exit_enabled=True)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine(positions={profile.symbol: position})
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="CLOSE_POSITION",
        reason="close risk",
    )
    governor.events_journal = FakeJournal()
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter(position=position)
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: ready},
        TradeDecision.close("close risk"),
    )
    governor.hot_symbol_worker = DecideHotWorker(TradeDecision.close("close risk"))
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=now)

    event_types = [event_type for event_type, _data in governor.events_journal.events]
    guard = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == PRE_EXECUTION_GUARD_PASSED
    )
    assert guard["reason"] == "CLOSE_POSITION_BYPASSES_PRE_EXECUTION_OPEN_GUARDS"
    assert guard["motivo_pt"] == "Fechamento permitido sem travas de abertura."
    assert guard["planned_stop_price"] is None
    assert guard["planned_take_profit_price"] is None
    assert event_types.index(PRE_EXECUTION_GUARD_PASSED) < event_types.index(TRADE_APPROVED)
    assert CLOSE_POSITION_REQUESTED in event_types
    assert CLOSE_POSITION_SENT in event_types
    assert execution.executed == [(profile.symbol, "CLOSE_POSITION")]


def test_initial_economic_gate_blocks_before_intelligent_layers(config_factory):
    config = replace(config_factory(), take_profit_pct=0.0001)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    portfolio = CountingPortfolio(allowed=True)
    governor.events_journal = FakeJournal()
    governor.execution_engine = FakeExecutionEngine()
    governor.market_data_router = FakeRouter()
    ready = SignalScore(symbol=profile.symbol, score=95, action="OPEN_LONG", reason="ready")
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = portfolio

    governor.run_cycle(now=time.time() + 1)

    event_types = [event_type for event_type, _data in governor.events_journal.events]
    assert TRADE_BLOCKED_BY_ECONOMIC_GATE in event_types
    assert PLANNED_EXIT_BEFORE_INTELLIGENT_LAYERS not in event_types
    assert PLANNED_EXIT_AFTER_INTELLIGENT_LAYERS not in event_types
    assert "intelligent_order_plan_shadow_created" not in event_types
    assert governor.execution_engine.executed == []
    assert portfolio.calls == 0


def test_planned_exit_checkpoints_keep_trace_ids(config_factory):
    config = replace(
        config_factory(),
        stop_loss_pct=0.003,
        take_profit_pct=0.004,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.execution_engine = FakeExecutionEngine()
    governor.market_data_router = FakeRouter()
    ready = SignalScore(symbol=profile.symbol, score=95, action="OPEN_LONG", reason="ready")
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    before = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == PLANNED_EXIT_BEFORE_INTELLIGENT_LAYERS
    )
    after = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == PLANNED_EXIT_AFTER_INTELLIGENT_LAYERS
    )
    payload = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == EXECUTION_PAYLOAD_SENT
    )

    assert before["cycle_id"] == payload["cycle_id"]
    assert after["cycle_id"] == payload["cycle_id"]
    assert before["symbol_eval_id"] == payload["symbol_eval_id"]
    assert after["symbol_eval_id"] == payload["symbol_eval_id"]
    assert before["planned_take_profit_price"] == Decimal("100.90")
    assert after["planned_take_profit_price"] == Decimal("100.90")


def test_final_economic_gate_blocks_before_portfolio_and_execution(
    config_factory,
    monkeypatch,
):
    config = config_factory(dynamic_exit_enabled=True)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    portfolio = CountingPortfolio(allowed=True)
    ready = SignalScore(symbol=profile.symbol, score=95, action="OPEN_LONG", reason="ready")
    governor.events_journal = FakeJournal()
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = portfolio

    def invalid_original_exit(_trade_pipeline_result, *, action):
        assert action == "OPEN_LONG"
        return {
            "stop_price": Decimal("99.00"),
            "take_profit_price": Decimal("100.01"),
        }, "TEST_INVALID_ORIGINAL_EXIT"

    def invalid_intelligent_exit(**_kwargs):
        return IntelligentExitActivationDecision(
            status="APPLIED",
            action="APPLY_PAPER_INTELLIGENT_EXIT",
            reason="test final exit still invalid",
            symbol=profile.symbol,
            trade_action="OPEN_LONG",
            trade_plan_id="tp_test",
            final_stop_price=Decimal("99.00"),
            final_take_profit_price=Decimal("100.02"),
            applied=True,
            exit_applied=True,
            can_use_for_execution=True,
        )

    monkeypatch.setattr(
        TradePlanningService,
        "planned_exit_for_execution",
        lambda _planning, *args, **kwargs: invalid_original_exit(*args, **kwargs),
    )
    monkeypatch.setattr(
        IntelligentFlowService,
        "evaluate_exit_activation",
        lambda _flow, **kwargs: invalid_intelligent_exit(**kwargs),
    )

    governor.run_cycle(now=time.time() + 1)

    blocked = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == TRADE_BLOCKED_BY_FINAL_ECONOMIC_GATE
    )
    assert blocked["reason"] == "EXPECTED_NET_GAIN_NOT_POSITIVE"
    assert execution.executed == []
    assert portfolio.calls == 0


def test_flow_decision_event_has_minimum_contract(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.execution_engine = FakeExecutionEngine()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: SignalScore(symbol=profile.symbol, score=10, reason="cold")}
    )

    governor.run_cycle(now=time.time() + 1)

    decision = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == GOVERNOR_EVALUATE_SYMBOL_FLOW_DECISION
    )
    expected_fields = {
        "symbol",
        "stage",
        "classification",
        "reason",
        "terminal",
        "execution_will_run",
    }
    assert expected_fields <= set(decision)
    assert decision["cycle_id"]
    assert decision["symbol_eval_id"]


def test_safe_append_failure_does_not_raise():
    logger = RecordingLogger()

    safe_append(FailingJournal(), "diagnostic_event", logger=logger, symbol="BTCUSDT")

    assert logger.warnings
    assert "diagnostic_event" in logger.warnings[0]
