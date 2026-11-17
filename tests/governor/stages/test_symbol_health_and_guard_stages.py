"""G4: warmup, quarantine (4, 5, 12, 13) and the defensive/AI guards (6, 14)."""

from __future__ import annotations

from types import SimpleNamespace

from governor.cycle_context import CycleContext
from governor.defensive_mode import DefensiveModeEvaluation
from governor.services import AIGateService, DefensiveModeService
from governor.stages.guards import DefensiveAndAIGuardStage
from governor.stages.symbol_health import QuarantineStage, SymbolWarmupStage
from governor.symbol_quarantine import SymbolQuarantine, SymbolQuarantineSettings
from governor.symbol_warmup import SymbolWarmupEvaluator
from tests.governor.stages.fakes import make_evaluation, make_position, make_score
from trading.intent import TradeIntent


def _warmup_stage(phase, outcomes, journal) -> SymbolWarmupStage:
    return SymbolWarmupStage(
        phase=phase,
        outcomes=outcomes,
        journal=journal,
        trace_payload=lambda: {"cycle_id": "cycle-1"},
        evaluator=lambda: SymbolWarmupEvaluator(min_observed_prices=5, mode="PROTECTIVE"),
        min_observed_prices=lambda: 5,
    )


def _light(prices: int):
    market = SimpleNamespace(prices=[100.0] * prices, data_age_seconds=0.1, stale=False)
    return SimpleNamespace(market=market, prices=market.prices, data_age_seconds=0.1, stale=False)


def test_pre_heavy_warmup_blocks_a_new_entry_without_enough_prices(outcomes, journal):
    stage = _warmup_stage("PRE_HEAVY", outcomes, journal)
    evaluation = make_evaluation(light=_light(1), score=make_score(), position=make_position())

    result = stage(evaluation)

    assert stage.name == "run_symbol_warmup_pre_heavy"
    assert result is not None and result.stage == "SYMBOL_WARMUP"
    assert result.reason == "SYMBOL_WARMUP_INCOMPLETE"
    assert journal.types()[:2] == ["symbol_warmup_evaluated", "symbol_warmup_new_entry_blocked"]
    assert journal.events[0][1]["stage"] == "PRE_HEAVY"


def test_post_heavy_warmup_only_observes(outcomes, journal):
    stage = _warmup_stage("POST_HEAVY", outcomes, journal)
    intent = TradeIntent(symbol="BTCUSDT", action="OPEN_LONG", reason="signal")
    evaluation = make_evaluation(heavy=_light(1), intent=intent, position=make_position())

    assert stage(evaluation) is None
    assert stage.name == "run_symbol_warmup_post_heavy"
    assert journal.types() == ["symbol_warmup_evaluated"]
    assert journal.events[0][1]["stage"] == "POST_HEAVY"


def _quarantine() -> SymbolQuarantine:
    return SymbolQuarantine(
        SymbolQuarantineSettings(
            watch_after_errors=1,
            cooldown_after_errors=1,
            quarantine_after_errors=1,
            cooldown_seconds=600.0,
            quarantine_seconds=1800.0,
        )
    )


def test_quarantine_blocks_new_entries_of_a_quarantined_symbol(outcomes, journal):
    manager = _quarantine()
    manager.record_event("BTCUSDT", event="SYMBOL_EVALUATION_ERROR", reason="boom", now=999.0)
    stage = QuarantineStage(
        phase="PRE_HEAVY",
        outcomes=outcomes,
        journal=journal,
        trace_payload=lambda: {},
        quarantine=lambda: manager,
    )

    result = stage(make_evaluation(score=make_score(), position=make_position()))

    assert stage.name == "run_symbol_quarantine_pre_heavy"
    assert result is not None and result.stage == "SYMBOL_QUARANTINE"
    assert journal.types() == [
        "symbol_quarantine_shadow_evaluated",
        "symbol_quarantine_new_entry_blocked",
        "governor_evaluate_symbol_flow_decision",
    ]


def test_post_heavy_quarantine_uses_the_intent_and_heavy_position(outcomes, journal):
    stage = QuarantineStage(
        phase="POST_HEAVY",
        outcomes=outcomes,
        journal=journal,
        trace_payload=lambda: {},
        quarantine=_quarantine,
    )
    intent = TradeIntent(symbol="BTCUSDT", action="CLOSE_POSITION", reason="exit")
    evaluation = make_evaluation(
        intent=intent,
        heavy=SimpleNamespace(position=make_position(open_position=True)),
        position=make_position(),
    )

    assert stage(evaluation) is None
    payload = journal.events[0][1]
    assert payload["stage"] == "POST_HEAVY" and payload["position_open"] is True


def _active_defensive_context() -> CycleContext:
    return CycleContext(
        now=1_000.0,
        defensive_mode=DefensiveModeEvaluation(
            mode="MARKET_SHOCK",
            reason="SHOCK",
            severity="HIGH",
            shadow_only=False,
            would_block_new_entries=True,
            would_reduce_new_entries=True,
            would_prioritize_protection=True,
            would_allow_closing=True,
            would_allow_stop_take_adjustment=True,
            diagnostics={},
        ),
    )


def _guard(phase, outcomes, journal, *, ai_enabled=False, regime_mode="OBSERVE"):
    strategy = SimpleNamespace(global_strategy_name="test_strategy")
    config = SimpleNamespace(
        ai_probabilistic_enabled=ai_enabled,
        governor_defensive_mode_enforcement_enabled=True,
        governor_defensive_mode_block_new_entries=True,
    )
    return DefensiveAndAIGuardStage(
        phase=phase,
        outcomes=outcomes,
        ai_gate=AIGateService(config=config, journal=journal, strategy=strategy),
        defensive_mode=DefensiveModeService(config=config, journal=journal, strategy=strategy),
        regime_policy_mode=lambda: regime_mode,
    )


def test_pre_heavy_guard_blocks_entries_in_active_defensive_mode(outcomes, journal, snapshots):
    context = _active_defensive_context()
    evaluation = make_evaluation(
        cycle_context=context, score=make_score(), position=make_position()
    )

    result = _guard("PRE_HEAVY", outcomes, journal)(evaluation)

    assert result is not None and result.stage == "PRE_HEAVY_DEFENSIVE_MODE"
    assert result.reason == "DEFENSIVE_MODE_MARKET_SHOCK"
    assert context.defensive_blocks and context.defensive_blocks[0]["stage"] == "PRE_HEAVY_SCORE"
    assert journal.types()[0] == "trade_blocked_by_defensive_mode"
    assert snapshots["BTCUSDT"].reason == "DEFENSIVE_MODE_MARKET_SHOCK"


def test_pre_heavy_guard_blocks_entries_without_ai_metadata(outcomes, journal):
    evaluation = make_evaluation(score=make_score(), position=make_position())

    result = _guard("PRE_HEAVY", outcomes, journal, ai_enabled=True)(evaluation)

    assert result is not None and result.stage == "PRE_HEAVY_AI_PROBABILITY"
    assert result.reason == "AI_PROBABILITY_METADATA_MISSING"
    assert journal.types()[0] == "ai_probability_gate_blocked"


def test_post_heavy_guard_checks_regime_ai_and_defensive_mode_in_order(outcomes, journal):
    intent = TradeIntent(symbol="BTCUSDT", action="OPEN_LONG", reason="signal")
    evaluation = make_evaluation(
        cycle_context=_active_defensive_context(),
        score=make_score(),
        intent=intent,
        heavy=SimpleNamespace(position=make_position()),
    )

    result = _guard("POST_HEAVY", outcomes, journal)(evaluation)

    assert result is not None and result.stage == "POST_HEAVY_DEFENSIVE_MODE"
    assert evaluation.diagnostics["regime_policy"]["allowed"] is True


def test_post_heavy_guard_lets_closes_through(outcomes, journal):
    intent = TradeIntent(symbol="BTCUSDT", action="CLOSE_POSITION", reason="exit")
    evaluation = make_evaluation(
        cycle_context=_active_defensive_context(),
        score=make_score(action="CLOSE_POSITION"),
        intent=intent,
        heavy=SimpleNamespace(position=make_position(open_position=True)),
    )

    assert _guard("POST_HEAVY", outcomes, journal, ai_enabled=True)(evaluation) is None


def test_ai_signal_is_recorded_once_per_evaluation(journal):
    from scanner.signal_score import SignalScore

    service = AIGateService(
        config=SimpleNamespace(
            ai_min_long_probability=0.62,
            ai_min_short_probability=0.62,
            ai_min_direction_gap=0.18,
            ai_min_expected_value_bps=3.0,
        ),
        journal=journal,
        strategy=SimpleNamespace(global_strategy_name="ProbabilisticAIStrategy"),
    )
    score = SignalScore(
        symbol="BTCUSDT",
        score=95,
        action="OPEN_LONG",
        reason="ai",
        metadata={
            "source": "AI_PROBABILISTIC",
            "model_source": "GLOBAL",
            "fallback_used": True,
            "fallback_reason": "SYMBOL_REFINED_MODEL_NOT_CONFIGURED",
        },
    )

    service.record_signal(
        symbol_config=SimpleNamespace(symbol="BTCUSDT"), score=score, stage="LIGHT_SCORE"
    )

    assert journal.types() == ["ai_probability_signal"]
