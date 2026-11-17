"""M6 differential: a candle and configuration produce the same live and historical plan.

Fill price, slippage, funding and latency belong to ExecutionSimulator and are
intentionally outside this decision comparison. Live currently refuses LIMIT
dispatch, so this differential exercises the common MARKET plan.
"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtesting.adapters.decision_stages import HistoricalDecisionStages
from backtesting.adapters.planned_exit import HistoricalPlannedExitPort
from backtesting.adapters.signal import HistoricalSignalPort
from backtesting.domain.config import BacktestStrategyConfig, BacktestSymbolConfig, CandleEvent
from backtesting.engine.decision_pipeline import GovernorSimulator
from backtesting.engine.execution import ExecutionSimulator
from backtesting.engine.portfolio import PortfolioSimulator
from backtesting.engine.strategy_runtime import BacktestStrategyEvaluation
from config.smart_settings import SmartExecutionSettings, SmartSizingSettings
from config.symbol_context import PortfolioRiskSettings
from domain.candle import Candle
from domain.models import TradeAction, TradeDecision
from domain.symbol_rules import SymbolRules
from futures.performance.cost_model import CostModel
from futures.planned_exit import resolve_strategy_planned_exit
from futures.trend import TrendContextEngine
from governor.evaluation.outcomes import StageOutcomes
from governor.evaluation.runtime_board import SymbolRuntimeBoard
from governor.services.trade_planning import TradePlanningService
from governor.smart_execution import SmartExecutionEngine
from governor.smart_sizing import SmartSizingEngine
from governor.stages.decision_kernel import PURE_DECISION_STAGES
from governor.stages.execution_planning import SmartExecutionStage
from governor.stages.sizing import SmartSizingStage
from operational_module_modes import (
    MODULE_SMART_EXECUTION,
    MODULE_SMART_SIZING,
    OperationalModuleMode,
    SmartSizingMode,
    module_modes_from_mapping,
)
from scanner.signal_score import SignalScore
from tests.golden.harness import run_scenario


class _Journal:
    def append(self, event_type, **data):
        pass


@pytest.mark.parametrize("action", [TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT])
@pytest.mark.parametrize("price", [Decimal("100"), Decimal("103")])
def test_historical_candle_matches_live_stage_plan(action, price, tmp_path):
    symbol_config = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(tmp_path / "unused.csv"),
        rules=SymbolRules(
            symbol="BTCUSDT",
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
            price_tick=Decimal("0.1"),
        ),
        position_notional_usdt=Decimal("50"),
    )
    candle = Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=1_700_000_000_000,
        close_time_ms=1_700_000_059_999,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("1000"),
        is_closed=True,
    )
    event = CandleEvent(timestamp=candle.timestamp, symbol="BTCUSDT", candle=candle)
    portfolio = PortfolioSimulator(
        symbols=[symbol_config],
        strategy=BacktestStrategyConfig(),
        settings=PortfolioRiskSettings(max_open_positions=5),
        initial_balance="1000",
    )
    execution = ExecutionSimulator(symbols=[symbol_config], cost_model=CostModel(spread_bps=0))
    modes = module_modes_from_mapping(
        {
            MODULE_SMART_SIZING: SmartSizingMode.REDUCE_ONLY.value,
            MODULE_SMART_EXECUTION: OperationalModuleMode.ACTIVE.value,
        }
    ).modes
    sizing_engine = SmartSizingEngine(SmartSizingSettings())
    execution_engine = SmartExecutionEngine(SmartExecutionSettings(limit_entry_enabled=False))
    historical = HistoricalDecisionStages(
        execution=execution,
        portfolio=portfolio,
        symbol_configs={symbol_config.symbol: symbol_config},
        module_modes=modes,
        sizing_engine=sizing_engine,
        execution_engine=execution_engine,
        signal_timing_policy="ON_CLOSE",
        strategy_name="test.strategy",
    )
    score = SignalScore(symbol="BTCUSDT", score=60, action=action.value, reason="signal")
    planned_exit = {
        "stop_price": price - 1 if action is TradeAction.OPEN_LONG else price + 1,
        "take_profit_price": price + 2 if action is TradeAction.OPEN_LONG else price - 2,
        "risk_abs": Decimal("1"),
        "reward_abs": Decimal("2"),
        "reward_risk_liquido": Decimal("2"),
    }
    session = historical.for_open(
        event=event,
        score=score,
        action=action,
        reason="signal",
        planned_exit=planned_exit,
    )
    assert tuple(type(stage) for stage in session.stages) == PURE_DECISION_STAGES
    if action is TradeAction.OPEN_LONG and price == Decimal("103"):

        def reject_quantity(**kwargs):
            raise ValueError("invalid")

        invalid_rules = SimpleNamespace(
            minimum_required_notional=lambda _: Decimal("5"),
            round_quantity=lambda _: Decimal("0"),
            validate_quantity=reject_quantity,
        )
        invalid_session = replace(
            session,
            evaluation=replace(
                session.evaluation,
                heavy=replace(session.evaluation.heavy, exchange_rules=invalid_rules),
            ),
        )
        invalid_session.size()
        assert invalid_session.sizing_block_reason == "SMART_SIZING_QUANTITY_INVALID"
    live_evaluation = replace(session.evaluation)
    journal = _Journal()
    board = SymbolRuntimeBoard({}, strategy=SimpleNamespace(global_strategy_name="test.strategy"))
    outcomes = StageOutcomes(journal=journal, trace_payload=lambda: {}, board=board)
    live_sizing_stage = SmartSizingStage(
        outcomes=outcomes,
        module_modes=modes,
        engine=lambda: sizing_engine,
        journal=journal,
        trace_payload=lambda: {},
        record_transition=lambda *args, **kwargs: None,
    )
    live_execution_stage = SmartExecutionStage(
        outcomes=outcomes,
        module_modes=modes,
        engine=lambda: execution_engine,
        config=SimpleNamespace(
            estimated_spread_bps=Decimal("0"),
            estimated_slippage_bps=Decimal("0"),
            limit_entry_enabled=False,
        ),
        journal=journal,
        trace_payload=lambda: {},
    )

    historical_sizing = session.size()
    live_sizing_stage(live_evaluation)
    live_sizing = live_evaluation.smart_sizing_decision
    assert live_sizing is not None
    assert historical_sizing.to_dict() == live_sizing.to_dict()
    assert historical_sizing.affected_result
    assert session.approved_notional(historical_sizing) == (
        live_evaluation.planned_quantity * price
    )

    historical_execution = session.choose_execution(historical_sizing)
    live_execution_stage(live_evaluation)
    live_execution = live_evaluation.smart_execution_decision
    assert live_execution is not None
    assert (
        session.evaluation.intent.action,
        session.evaluation.planned_quantity,
        session.evaluation.planned_exit,
        historical_execution.entry_order_type,
        historical_execution.blocked,
    ) == (
        live_evaluation.intent.action,
        live_evaluation.planned_quantity,
        live_evaluation.planned_exit,
        live_execution.entry_order_type,
        live_execution.blocked,
    )
    assert session.evaluation.evaluation_input.now == (candle.close_time_ms + 1) / 1000
    assert session.evaluation.intent.created_at == session.evaluation.evaluation_input.now


@pytest.mark.parametrize("valid", [True, False])
def test_historical_and_live_validate_the_same_strategy_exit(valid):
    rules = SymbolRules(
        symbol="BTCUSDT",
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_tick=Decimal("0.1"),
    )
    config = BacktestSymbolConfig(symbol="BTCUSDT", csv_path="unused", rules=rules)
    candle = Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=1_700_000_000_000,
        close_time_ms=1_700_000_059_999,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("100"),
        is_closed=True,
    )
    event = CandleEvent(timestamp=candle.timestamp, symbol="BTCUSDT", candle=candle)
    metadata = {
        "recommended_stop_price": "99" if valid else "101",
        "recommended_take_profit_price": "102",
        "risk_abs": "1",
        "reward_abs": "2",
        "reward_risk_liquido": "2",
    }
    score = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", metadata=metadata)
    historical = HistoricalPlannedExitPort(
        {"BTCUSDT": config}, price_for_event=lambda _: Decimal("100")
    )
    historical_plan = historical.resolve(score=score, action=TradeAction.OPEN_LONG, event=event)
    live_service = SimpleNamespace(journal=_Journal(), trace_payload=lambda: {})
    live_plan, live_reason = TradePlanningService.strategy_planned_exit_for_execution(
        live_service,
        score=score,
        heavy_snapshot=SimpleNamespace(
            market=SimpleNamespace(price=Decimal("100")), exchange_rules=rules
        ),
        action="OPEN_LONG",
    )
    _, _, shared = resolve_strategy_planned_exit(
        metadata,
        action="OPEN_LONG",
        entry_price=Decimal("100"),
        price_tick=rules.price_tick,
    )
    assert historical_plan["dynamic_exit_used"] is shared.valid
    assert (live_plan is not None) is shared.valid
    if valid:
        assert live_reason == "STRATEGY_METADATA_EXIT_READY"
        assert historical_plan["stop_price"] == live_plan["stop_price"]
        assert historical_plan["take_profit_price"] == live_plan["take_profit_price"]
    else:
        assert historical_plan["planned_exit_rejection_reason"] == live_reason


def test_closed_candle_signal_history_stays_bounded():
    score = SignalScore(symbol="BTCUSDT", score=10, action="HOLD", reason="hold")
    runtime = SimpleNamespace(
        evaluate=lambda symbol, market, position: SimpleNamespace(
            score=score, decision=TradeDecision.hold("hold"), metadata={}
        )
    )
    execution = SimpleNamespace(current_position=lambda symbol: None)
    port = HistoricalSignalPort(
        runtime=runtime,
        execution=execution,
        clock=SimpleNamespace(
            signal_timing_policy="ON_CLOSE", price=lambda event: event.candle.close
        ),
        trend_engine=TrendContextEngine(),
        trend_enabled=False,
        trend_mode="OBSERVE",
        price_window=30,
    )
    prices = {}
    for index in range(100):
        candle = Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time_ms=1_700_000_000_000 + index * 60_000,
            close_time_ms=1_700_000_059_999 + index * 60_000,
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("1"),
            is_closed=True,
        )
        event = CandleEvent(timestamp=candle.timestamp, symbol="BTCUSDT", candle=candle)
        assert (
            port.evaluate(event, prices_by_symbol=prices, closed_intrabar=False).action
            is TradeAction.HOLD
        )
    assert len(prices["BTCUSDT"]) == 30


def test_entry_approved_golden_and_backtest_plan_match(config_factory):
    """Replay the same recorded price, account and sizing settings through both paths."""
    scenario = Path(__file__).parents[1] / "golden" / "entry_approved"
    live = run_scenario(scenario, config_factory)
    live_order = live["mutations"]["new_order"][0]
    live_protections = live["mutations"]["new_algo_order"]
    pure_names = tuple(stage.name for stage in PURE_DECISION_STAGES)
    assert (
        tuple(item["stage"] for item in live["stages"][0]["trace"] if item["stage"] in pure_names)
        == pure_names
    )

    rules = SymbolRules(
        symbol="BTCUSDT",
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_tick=Decimal("0.1"),
    )
    symbol = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path=str(scenario / "candles.csv"),
        rules=rules,
        position_notional_usdt=Decimal("50"),
        stop_loss_pct=Decimal("0.01"),
        take_profit_pct=Decimal("0.02"),
    )
    execution = ExecutionSimulator(symbols=[symbol], cost_model=CostModel())
    portfolio = PortfolioSimulator(
        symbols=[symbol],
        strategy=BacktestStrategyConfig(),
        settings=PortfolioRiskSettings(max_open_positions=5),
        initial_balance="1000",
    )

    class GoldenRuntime:
        module_name = "tests"
        class_name = "FakeStrategy"
        required_history = 1

        def evaluate(self, symbol, market, position):
            return BacktestStrategyEvaluation(
                score=SignalScore(symbol=symbol, score=95, action="OPEN_LONG", reason="ready"),
                decision=TradeDecision.open_long("confirmed"),
            )

    governor = GovernorSimulator(
        strategy_runtime=GoldenRuntime(),
        portfolio=portfolio,
        execution=execution,
        strategy_config=BacktestStrategyConfig(),
        symbol_configs=[symbol],
        initial_balance=Decimal("1000"),
        trend_context_enabled=False,
        market_entry_policy="SIGNAL_CLOSE",
    )
    candle = Candle(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=1_700_000_000_000,
        close_time_ms=1_700_000_059_999,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("1000"),
        is_closed=True,
    )
    event = CandleEvent(timestamp=candle.timestamp, symbol="BTCUSDT", candle=candle)
    governor.start_cycle()
    governor.process_candle_open(event)
    governor.process_intrabar_exit(event)
    governor.process_signal_cycle([event])

    position = execution.current_position("BTCUSDT")
    simulated_position = execution.brokers["BTCUSDT"].position
    assert simulated_position is not None
    assert governor.decisions[-1].final_action == "OPEN_LONG"
    assert governor.governor_decisions[-1].allowed
    assert position.quantity == Decimal(live_order["quantity"])
    assert simulated_position.stop_price == Decimal(live_protections[0]["triggerPrice"])
    assert simulated_position.take_profit_price == Decimal(live_protections[1]["triggerPrice"])
