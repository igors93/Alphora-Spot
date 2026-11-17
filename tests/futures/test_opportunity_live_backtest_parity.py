"""Differential parity and executability tests for TradeOpportunity (OPP-023)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from backtesting.adapters.decision_stages import HistoricalDecisionStages
from backtesting.domain.config import BacktestStrategyConfig, BacktestSymbolConfig, CandleEvent
from backtesting.engine.execution import ExecutionSimulator
from backtesting.engine.portfolio import PortfolioSimulator
from config import ExecutionMode
from config.smart_settings import SmartExecutionSettings, SmartSizingSettings
from config.symbol_context import PortfolioRiskSettings, symbol_config_from_app_config
from domain.candle import Candle
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeAction
from domain.opportunity import NotExecutable, TradeOpportunity
from domain.symbol_rules import SymbolRules
from futures.performance.cost_model import CostModel as BacktestCostModel
from futures.trading_policy import TradingPolicy
from governor.smart_execution import SmartExecutionEngine
from governor.smart_sizing import SmartSizingEngine
from governor.trade_pipeline import TradePipeline
from market_data.market_snapshots import HeavyMarketSnapshot
from scanner.signal_score import SignalScore
from trading.intent import TradeIntent

D = Decimal


def _rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        quantity_step=D("0.001"),
        min_quantity=D("0.001"),
        min_notional=D("5"),
        price_tick=D("0.10"),
    )


def _closed_candle(index: int = 10, price: str = "100.0") -> Candle:
    p = D(price)
    return Candle(
        symbol="BTCUSDT",
        interval="15m",
        open_time_ms=index * 900_000,
        close_time_ms=(index + 1) * 900_000 - 1,
        open=p - D("0.3"),
        high=p + D("0.6"),
        low=p - D("0.5"),
        close=p,
        volume=D("150"),
        is_closed=True,
    )


def test_live_and_backtest_produce_identical_trade_opportunity_for_same_candle(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    candle = _closed_candle(10, "100.0")
    rules = _rules()

    score = SignalScore(
        symbol="BTCUSDT",
        score=92,
        action="OPEN_LONG",
        confidence=90,
        reason="donchian_breakout",
        strategy="strategies.futures.fixed.donchian_adx_breakout.DonchianAdxBreakoutStrategy",
        metadata={
            "recommended_stop_price": "99.0",
            "recommended_take_profit_price": "102.0",
            "risk_abs": "1.0",
            "reward_abs": "2.0",
            "reward_risk_liquido": "1.8",
            "raw_reward_risk": "2.0",
            "cost_to_reward_ratio": "0.10",
            "risk_pct": "0.01",
            "reward_pct": "0.02",
        },
    )

    # 1. Live TradePipeline (used by Stage 15 TradePlanStage)
    live_heavy = HeavyMarketSnapshot(
        symbol="BTCUSDT",
        market=MarketSnapshot(
            symbol="BTCUSDT",
            price=candle.close,
            prices=[candle.close],
            candles={"15m": (candle,)},
            primary_timeframe="15m",
        ),
        mark_price=candle.close,
        exchange_rules=rules,
        position=FuturesPosition("BTCUSDT", PositionDirection.NONE, D("0"), D("0"), D("0")),
        open_orders=[],
        open_algo_orders=[],
        account_info={},
        timestamp=candle.close_time_ms / 1000.0 + 0.35,
        data_age_seconds=0.35,
        stale=False,
    )
    live_intent = TradeIntent(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        score=score.score,
        priority=score.priority,
        confidence=score.confidence,
        reason=score.reason,
        strategy_name=score.strategy,
        market_price=candle.close,
        created_at=live_heavy.timestamp,
    )
    live_pipeline = TradePipeline(config=config)
    live_result = live_pipeline.build_plan(
        intent=live_intent,
        symbol_config=profile,
        heavy_snapshot=live_heavy,
        portfolio_snapshot={"available_balance_usdt": "1000"},
        score=score,
    )

    # 2. Backtest HistoricalDecisionStages (used by GovernorSimulator)
    bt_symbol = BacktestSymbolConfig(
        symbol="BTCUSDT",
        csv_path="unused.csv",
        position_notional_usdt=D(str(profile.position_notional_usdt)),
        stop_loss_pct=D(str(profile.stop_loss_pct)),
        take_profit_pct=D(str(profile.take_profit_pct)),
        leverage=profile.leverage,
        rules=rules,
    )
    bt_execution = ExecutionSimulator(
        symbols=[bt_symbol],
        cost_model=BacktestCostModel(
            maker_fee_pct=D(str(config.maker_fee_pct)),
            taker_fee_pct=D(str(config.taker_fee_pct)),
            spread_bps=D(str(config.estimated_spread_bps)),
            slippage_bps=D(str(config.estimated_slippage_bps)),
            default_funding_rate=D(str(config.estimated_funding_rate)),
        ),
    )
    bt_portfolio = PortfolioSimulator(
        symbols=[bt_symbol],
        strategy=BacktestStrategyConfig(),
        settings=PortfolioRiskSettings(),
        initial_balance="1000",
    )
    bt_stages = HistoricalDecisionStages(
        execution=bt_execution,
        portfolio=bt_portfolio,
        symbol_configs={"BTCUSDT": bt_symbol},
        module_modes=config.operational_module_modes,
        sizing_engine=SmartSizingEngine(settings=SmartSizingSettings()),
        execution_engine=SmartExecutionEngine(settings=SmartExecutionSettings()),
        signal_timing_policy="ON_CLOSE",
        strategy_name=score.strategy,
        min_seconds_between_entries=config.min_seconds_between_entries,
        max_daily_trades=config.max_daily_trades,
    )
    event = CandleEvent(
        symbol="BTCUSDT",
        timestamp=candle.timestamp,
        candle=candle,
        candle_snapshot={"15m": (candle,)},
    )
    bt_session = bt_stages.for_open(
        event=event,
        score=score,
        action=TradeAction.OPEN_LONG,
        reason=score.reason,
        planned_exit={"stop_price": D("99.0"), "take_profit_price": D("102.0")},
    )

    live_opp = live_result.opportunity
    bt_opp = bt_session.evaluation.opportunity

    assert isinstance(live_opp, TradeOpportunity)
    assert isinstance(bt_opp, TradeOpportunity)
    assert live_opp.executable is True
    assert bt_opp.executable is True
    assert live_opp.opportunity_id == bt_opp.opportunity_id
    assert live_opp.policy_version == bt_opp.policy_version
    assert live_opp.knowledge_time == bt_opp.knowledge_time
    assert live_opp.knowledge_time_ms == bt_opp.knowledge_time_ms == candle.close_time_ms + 1
    assert live_opp.data_watermark == bt_opp.data_watermark
    assert live_opp.plan == bt_opp.plan
    assert [c.name for c in live_opp.executability.checks] == [
        c.name for c in bt_opp.executability.checks
    ]
    assert [(c.name, c.passed, c.reason) for c in live_opp.executability.checks] == [
        (c.name, c.passed, c.reason) for c in bt_opp.executability.checks
    ]


def test_executability_checks_reject_hard_violations_and_preserve_path_context(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    candle = _closed_candle(10, "100.0")
    rules = _rules()
    policy = TradingPolicy.from_config(config)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 90, "breakout", "tests.Strategy")

    base_context = TradePipeline(config=config).context_builder.build(
        symbol_config=profile,
        heavy_snapshot=HeavyMarketSnapshot(
            symbol="BTCUSDT",
            market=MarketSnapshot(
                symbol="BTCUSDT",
                price=candle.close,
                prices=[candle.close],
                candles={"15m": (candle,)},
                primary_timeframe="15m",
            ),
            mark_price=candle.close,
            exchange_rules=rules,
            position=FuturesPosition(
                "BTCUSDT", PositionDirection.LONG, D("0.5"), D("98.0"), D("1.0")
            ),
            open_orders=[],
            open_algo_orders=[],
            account_info={},
            timestamp=candle.close_time_ms / 1000.0 + 0.1,
            data_age_seconds=0.1,
            stale=False,
        ),
        portfolio_snapshot={
            "open_positions": [{"symbol": "BTCUSDT", "notional": "50"}],
            "daily_realized_pnl": "-12.5",
            "daily_trades": 3,
            "consecutive_losses": 2,
            "available_balance_usdt": "950",
        },
    )

    # Path-dependent state is recorded as context without making the opportunity non-executable.
    valid_opp = policy.opportunity_at(intent=intent, context=base_context)
    assert valid_opp.executable is True
    assert not isinstance(valid_opp, NotExecutable)
    assert valid_opp.path_context.position_open is True
    assert valid_opp.path_context.open_positions_count == 1
    assert valid_opp.path_context.gross_exposure_usdt == D("50")
    assert valid_opp.path_context.daily_realized_pnl_usdt == D("-12.5")
    assert valid_opp.path_context.consecutive_losses == 2

    # 1. Future candle in window -> rejected by causal_watermark
    future_opp = policy.opportunity_at(
        intent=intent,
        context=base_context,
        knowledge_time_ms=candle.close_time_ms - 1000,
    )
    assert isinstance(future_opp, NotExecutable)
    assert future_opp.executable is False
    assert future_opp.rejection_reason == "FUTURE_DATA_IN_CANDLE_WINDOW"

    # 2. Fast timeframe in REAL mode -> rejected by timeframe_allowed
    real_policy = TradingPolicy.from_config(
        replace(config, execution_mode=ExecutionMode.REAL_LIVE),
        primary_timeframe="1m",
    )
    fast_tf_opp = real_policy.opportunity_at(intent=intent, context=base_context, timeframe="1m")
    assert isinstance(fast_tf_opp, NotExecutable)
    assert fast_tf_opp.rejection_reason == "REAL_MODE_TIMEFRAME_TOO_FAST"

    # 3. Cadence violation -> rejected by cadence
    cadence_opp = policy.opportunity_at(
        intent=intent,
        context=base_context,
        seconds_since_last_entry=15.0,
    )
    assert isinstance(cadence_opp, NotExecutable)
    assert cadence_opp.rejection_reason == "MIN_SECONDS_BETWEEN_ENTRIES"

    # 4. Protection inside trigger buffer -> rejected by protection_viable
    tight_mark_context = replace(base_context, mark_price=D("99.01"))
    buffer_opp = policy.opportunity_at(intent=intent, context=tight_mark_context)
    assert isinstance(buffer_opp, NotExecutable)
    assert buffer_opp.rejection_reason == "PROTECTION_WITHIN_TRIGGER_BUFFER"
