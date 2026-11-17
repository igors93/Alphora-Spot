from dataclasses import replace
from decimal import Decimal

from backtesting.domain.config import (
    BacktestConfig,
    BacktestStrategyConfig,
    BacktestSymbolConfig,
)
from backtesting.domain.realism import BacktestFillSettings
from backtesting.engine import run_backtest
from backtesting.engine.strategy_runtime import BacktestStrategyRuntime
from config.smart_settings import SmartExecutionSettings
from config.symbol_context import PortfolioRiskSettings
from domain.models import (
    FuturesPosition,
    MarketSnapshot,
    PositionDirection,
    TradeAction,
    TradeDecision,
)
from domain.symbol_rules import SymbolRules
from futures.performance.cost_model import CostModel
from operational_module_modes import (
    MODULE_SMART_EXECUTION,
    MODULE_SMART_SIZING,
    OperationalModuleMode,
    SmartSizingMode,
)
from scanner.signal_score import SignalScore
from strategies.futures.base import FuturesStrategy, StrategyMetadata


class OpenLongThenCloseStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        return SignalScore(symbol=symbol, score=95, action="OPEN_LONG", reason="fixed long")

    def decide_symbol(self, symbol, market, position, state):
        if not position.is_open:
            return TradeDecision.open_long("fixed long")
        return TradeDecision.close("fixed close")


class OpenLongWithStrategyExitStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        if position.is_open:
            return SignalScore(symbol=symbol, score=20, action="NO_ACTION", reason="hold")
        return SignalScore(
            symbol=symbol,
            score=95,
            action="OPEN_LONG",
            reason="dynamic long",
            metadata=_strategy_exit_metadata(
                stop_price="99.40",
                take_profit_price="101.20",
            ),
        )

    def decide_symbol(self, symbol, market, position, state):
        if not position.is_open:
            return TradeDecision.open_long("dynamic long")
        return TradeDecision.hold("hold")


class OpenLongWithInvalidStrategyExitStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        if position.is_open:
            return SignalScore(symbol=symbol, score=20, action="NO_ACTION", reason="hold")
        return SignalScore(
            symbol=symbol,
            score=95,
            action="OPEN_LONG",
            reason="invalid dynamic long",
            metadata=_strategy_exit_metadata(
                stop_price="101.00",
                take_profit_price="101.20",
            ),
        )

    def decide_symbol(self, symbol, market, position, state):
        if not position.is_open:
            return TradeDecision.open_long("invalid dynamic long")
        return TradeDecision.hold("hold")


class OpenShortThenCloseStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        return SignalScore(symbol=symbol, score=95, action="OPEN_SHORT", reason="fixed short")

    def decide_symbol(self, symbol, market, position, state):
        if not position.is_open:
            return TradeDecision.open_short("fixed short")
        return TradeDecision.close("fixed close")


class OpenLongOnceThenCloseStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        if position.is_open:
            return SignalScore(symbol=symbol, score=95, action="CLOSE_POSITION", reason="close")
        if not state.get("open_signal_sent"):
            return SignalScore(symbol=symbol, score=95, action="OPEN_LONG", reason="limit long")
        return SignalScore(symbol=symbol, score=10, action="NO_ACTION", reason="hold")

    def decide_symbol(self, symbol, market, position, state):
        if position.is_open:
            return TradeDecision.close("close")
        if not state.get("open_signal_sent"):
            state["open_signal_sent"] = True
            return TradeDecision.open_long("limit long")
        return TradeDecision.hold("hold")


class AlwaysOpenLongStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        return SignalScore(symbol=symbol, score=95, action="OPEN_LONG", reason="blocked long")

    def decide_symbol(self, symbol, market, position, state):
        return TradeDecision.open_long("blocked long")


class SymbolScoreOpenLongStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        score = 95 if symbol == "ETHUSDT" else 90
        return SignalScore(
            symbol=symbol,
            score=score,
            action="OPEN_LONG",
            reason=f"ranked long {symbol}",
        )

    def decide_symbol(self, symbol, market, position, state):
        return TradeDecision.open_long(f"ranked long {symbol}")


class StatefulSymbolStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        state["score_calls"] = int(state.get("score_calls", 0)) + 1
        return SignalScore(
            symbol=symbol,
            score=95,
            reason="stateful score",
            action="OPEN_LONG",
        )

    def decide_symbol(self, symbol, market, position, state):
        state["decide_calls"] = int(state.get("decide_calls", 0)) + 1
        return TradeDecision.open_long("stateful decide")


class OpenLongAfterTrendWarmupStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        assert "trend_context" in market.indicators
        return SignalScore(symbol=symbol, score=95, action="OPEN_LONG", reason="trend probe")

    def decide_symbol(self, symbol, market, position, state):
        assert "trend_context" in market.indicators
        if len(market.prices) < 4:
            return TradeDecision.hold("wait trend warmup")
        if not position.is_open:
            return TradeDecision.open_long("trend probe long")
        return TradeDecision.close("trend probe close")


class BlockingMetadataStrategy(FuturesStrategy):
    def score_symbol(self, symbol, market, position, state):
        return SignalScore(
            symbol=symbol,
            score=20,
            action="NO_ACTION",
            reason="blocked setup",
            metadata={
                "setup_blockers": ["RSI_CROSS_UP_NOT_CONFIRMED"],
                "setup_side": "LONG",
            },
        )

    def decide_symbol(self, symbol, market, position, state):
        return TradeDecision.hold("blocked setup")


class RequiresOneMinuteStrategy(OpenLongThenCloseStrategy):
    metadata = StrategyMetadata(required_timeframes=["1m"])


class RequiresFifteenMinuteStrategy(OpenLongThenCloseStrategy):
    metadata = StrategyMetadata(required_timeframes=["15m"])


def test_backtesting_fixed_opens_and_closes_long(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    config = _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "101"]))

    result = run_backtest(config, write_reports=False)

    assert len(result.trades) == 1
    assert result.trades[0].direction is PositionDirection.LONG
    assert result.trades[0].gross_pnl == Decimal("0.500")
    assert result.decisions[-1].final_action == "CLOSE_POSITION"


def test_backtesting_uses_strategy_metadata_stop_and_take(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongWithStrategyExitStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100", "100", "100"),
            ("2", "100", "101.20", "100", "101"),
        ],
    )
    config = _config(
        tmp_path,
        csv_path,
        stop_loss_pct=Decimal("0.005"),
        take_profit_pct=Decimal("0.005"),
    )

    result = run_backtest(config, write_reports=False)

    trade = result.trades[0]
    assert trade.dynamic_exit_used is True
    assert trade.exit_source == "STRATEGY_METADATA"
    assert trade.recommended_stop_price == Decimal("99.40")
    assert trade.recommended_take_profit_price == Decimal("101.20")
    assert trade.actual_stop_price_used == Decimal("99.40")
    assert trade.actual_take_profit_price_used == Decimal("101.20")
    assert trade.exit_price == Decimal("101.20")
    assert result.summary["dynamic_exit_diagnostics"]["dynamic_exit_used_count"] == 1
    assert result.decisions[0].metadata["dynamic_exit_used"] is True
    assert result.decisions[0].metadata["exit_source"] == "STRATEGY_METADATA"


def test_backtesting_smart_sizing_reduce_only_reduces_notional(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongWithStrategyExitStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100", "100", "100"),
            ("2", "100", "101.20", "100", "101"),
        ],
    )
    config = replace(
        _config(tmp_path, csv_path),
        operational_module_modes={
            MODULE_SMART_SIZING: SmartSizingMode.REDUCE_ONLY.value,
        },
    )

    result = run_backtest(config, write_reports=False)

    trade = result.trades[0]
    assert trade.smart_sizing_mode == SmartSizingMode.REDUCE_ONLY.value
    assert trade.smart_sizing_used is True
    assert trade.smart_sizing_affected_result is True
    assert trade.base_notional_usdt == Decimal("50")
    assert trade.final_notional_usdt == Decimal("37.50")
    assert trade.sizing_multiplier == Decimal("0.75")
    assert trade.notional == Decimal("37.500")
    diagnostics = result.summary["smart_sizing_diagnostics"]
    assert diagnostics["smart_sizing_decisions"] == 1
    assert diagnostics["smart_sizing_reductions"] == 1
    assert result.decisions[0].metadata["smart_sizing"]["affected_result"] is True


def test_backtesting_falls_back_when_strategy_exit_is_invalid(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongWithInvalidStrategyExitStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100", "100", "100"),
            ("2", "100", "100.50", "100", "100.40"),
        ],
    )
    config = _config(
        tmp_path,
        csv_path,
        stop_loss_pct=Decimal("0.005"),
        take_profit_pct=Decimal("0.005"),
    )

    result = run_backtest(config, write_reports=False)

    trade = result.trades[0]
    assert trade.dynamic_exit_used is False
    assert trade.exit_source == "GLOBAL_FALLBACK"
    assert trade.actual_stop_price_used == Decimal("99.50")
    assert trade.actual_take_profit_price_used == Decimal("100.50")
    assert trade.planned_exit_rejection_reason == "LONG_STOP_NOT_BELOW_ENTRY"
    diagnostics = result.summary["dynamic_exit_diagnostics"]
    assert diagnostics["global_fallback_count"] == 1
    assert diagnostics["invalid_strategy_exit_count"] == 1


def test_backtesting_smart_execution_active_blocks_high_spread(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    config = replace(
        _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "101"])),
        cost_model=CostModel.from_numbers(spread_bps="30"),
        operational_module_modes={
            MODULE_SMART_EXECUTION: OperationalModuleMode.ACTIVE.value,
        },
    )

    result = run_backtest(config, write_reports=False)

    assert result.trades == []
    assert result.rejected_signals
    assert {item.layer for item in result.rejected_signals} == {"SMART_EXECUTION"}
    assert result.rejected_signals[0].reason == "SMART_EXECUTION_SPREAD_TOO_HIGH"
    diagnostics = result.summary["smart_execution_diagnostics"]
    assert diagnostics["smart_execution_blocks"] > 0
    assert diagnostics["smart_execution_decisions"] > 0


def test_backtesting_smart_execution_limit_long_fills_when_candle_touches(
    monkeypatch,
    tmp_path,
):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100", "99.90", "100"),
            ("2", "100", "101", "99.99", "101"),
        ],
    )
    config = replace(
        _config(tmp_path, csv_path),
        operational_module_modes={
            MODULE_SMART_EXECUTION: OperationalModuleMode.ACTIVE.value,
        },
        smart_execution_settings=SmartExecutionSettings(limit_entry_enabled=True),
    )

    result = run_backtest(config, write_reports=False)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_order_type == "LIMIT"
    assert trade.limit_fill_status == "LIMIT_FILLED"
    assert trade.limit_wait_candles == 1
    assert trade.limit_price == Decimal("99.995")
    assert result.summary["smart_execution_diagnostics"]["limit_filled"] == 1


def test_backtesting_smart_execution_limit_short_fills_when_candle_touches(
    monkeypatch,
    tmp_path,
):
    _patch_strategy(monkeypatch, OpenShortThenCloseStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100.01", "100", "100"),
            ("2", "100", "100.01", "99", "99"),
        ],
    )
    config = replace(
        _config(tmp_path, csv_path),
        operational_module_modes={
            MODULE_SMART_EXECUTION: OperationalModuleMode.ACTIVE.value,
        },
        smart_execution_settings=SmartExecutionSettings(limit_entry_enabled=True),
    )

    result = run_backtest(config, write_reports=False)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_order_type == "LIMIT"
    assert trade.limit_fill_status == "LIMIT_FILLED"
    assert trade.limit_price == Decimal("100.005")


def test_backtesting_smart_execution_limit_expires_when_price_does_not_touch(
    monkeypatch,
    tmp_path,
):
    _patch_strategy(monkeypatch, OpenLongOnceThenCloseStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100", "100", "100"),
            ("2", "100", "100", "100", "100"),
        ],
    )
    config = replace(
        _config(tmp_path, csv_path),
        operational_module_modes={
            MODULE_SMART_EXECUTION: OperationalModuleMode.ACTIVE.value,
        },
        smart_execution_settings=SmartExecutionSettings(limit_entry_enabled=True),
        limit_entry_timeout_candles=1,
    )

    result = run_backtest(config, write_reports=False)

    assert result.trades == []
    assert result.rejected_signals[0].reason == "SMART_EXECUTION_LIMIT_EXPIRED"
    assert result.summary["smart_execution_diagnostics"]["limit_expired"] == 1


def test_backtesting_smart_execution_limit_fallback_to_market_only_when_allowed(
    monkeypatch,
    tmp_path,
):
    _patch_strategy(monkeypatch, OpenLongOnceThenCloseStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100", "100", "100"),
            ("2", "100", "100", "100", "100"),
        ],
    )
    config = replace(
        _config(tmp_path, csv_path),
        operational_module_modes={
            MODULE_SMART_EXECUTION: OperationalModuleMode.ACTIVE.value,
        },
        smart_execution_settings=SmartExecutionSettings(
            limit_entry_enabled=True,
            limit_entry_fallback_to_market=True,
        ),
        limit_entry_timeout_candles=1,
    )

    result = run_backtest(config, write_reports=False)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_order_type == "MARKET"
    assert trade.limit_fill_status == "LIMIT_FALLBACK_TO_MARKET"
    assert trade.fallback_to_market_used is True
    assert result.rejected_signals == []


def test_backtesting_fixed_opens_and_closes_short(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenShortThenCloseStrategy)
    config = _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "99"]))

    result = run_backtest(config, write_reports=False)

    assert len(result.trades) == 1
    assert result.trades[0].direction is PositionDirection.SHORT
    assert result.trades[0].gross_pnl == Decimal("0.500")
    assert result.decisions[-1].final_action == "CLOSE_POSITION"


def test_backtesting_rejected_signal_is_recorded(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, AlwaysOpenLongStrategy)
    config = _config(
        tmp_path,
        _write_candles(tmp_path / "candles.csv", ["100", "101"]),
        portfolio_settings=PortfolioRiskSettings(
            max_open_positions=5,
            max_total_exposure_usdt=10,
        ),
    )

    result = run_backtest(config, write_reports=False)

    assert result.trades == []
    assert len(result.rejected_signals) == 2
    assert result.rejected_signals[0].layer == "RISK"
    assert result.rejected_signals[0].reason == "MAX_TOTAL_EXPOSURE"
    assert result.summary["blocked_by_risk"] == 2


def test_backtesting_applies_costs_to_net_pnl(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    config = _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "102"]))
    config = BacktestConfig(
        symbols=config.symbols,
        strategy=config.strategy,
        portfolio_settings=config.portfolio_settings,
        output_dir=config.output_dir,
        market_entry_policy="SIGNAL_CLOSE",
        cost_model=CostModel.from_numbers(
            taker_fee_pct="0.001",
            slippage_bps=0,
            spread_bps=0,
            funding_enabled=False,
        ),
        fill_settings=BacktestFillSettings(
            max_volume_participation=Decimal("1"),
            reliable_volume_participation=Decimal("1"),
            require_limit_trade_through=False,
            market_impact_bps_at_full_participation=Decimal("0"),
        ),
    )

    result = run_backtest(config, write_reports=False)

    assert result.trades[0].gross_pnl == Decimal("1.000")
    assert result.trades[0].fees == Decimal("0.101000")
    assert result.trades[0].net_pnl == Decimal("0.899000")


def test_backtesting_multi_symbol_keeps_clock_and_reports_missing_candles(
    monkeypatch,
    tmp_path,
):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    btc_csv = _write_candles(tmp_path / "btc.csv", ["100", "101"])
    eth_csv = _write_candles_with_timestamps(
        tmp_path / "eth.csv",
        [("1", "50"), ("3", "51")],
    )
    config = _multi_config(
        tmp_path,
        {"BTCUSDT": btc_csv, "ETHUSDT": eth_csv},
        portfolio_settings=PortfolioRiskSettings(
            max_open_positions=5,
            max_new_positions_per_cycle=5,
        ),
    )

    result = run_backtest(config, write_reports=False)

    assert result.summary["symbols_tested"] == ["BTCUSDT", "ETHUSDT"]
    assert result.summary["symbols_tested_count"] == 2
    assert set(result.summary["symbols_with_trades"]) == {"BTCUSDT", "ETHUSDT"}
    assert result.summary["data_feed"]["partial_timestamps"] == 2
    assert result.summary["data_feed"]["missing_candles_by_symbol"] == {
        "BTCUSDT": 1,
        "ETHUSDT": 1,
    }
    assert result.summary["best_symbol"] in {"BTCUSDT", "ETHUSDT"}
    assert result.summary["worst_symbol"] in {"BTCUSDT", "ETHUSDT"}


def test_backtesting_multi_symbol_applies_new_positions_limit_per_cycle(
    monkeypatch,
    tmp_path,
):
    _patch_strategy(monkeypatch, AlwaysOpenLongStrategy)
    btc_csv = _write_candles(tmp_path / "btc.csv", ["100", "101"])
    eth_csv = _write_candles(tmp_path / "eth.csv", ["50", "51"])
    config = _multi_config(
        tmp_path,
        {"BTCUSDT": btc_csv, "ETHUSDT": eth_csv},
        portfolio_settings=PortfolioRiskSettings(
            max_open_positions=5,
            max_new_positions_per_cycle=1,
        ),
    )

    result = run_backtest(config, write_reports=False)

    assert any(
        item.reason == "MULTIPLE_READY_BUT_PORTFOLIO_LIMITED" for item in result.rejected_signals
    )
    assert result.summary["blocked_by_risk"] > 0


def test_backtesting_ranks_same_timestamp_candidates_before_executing(
    monkeypatch,
    tmp_path,
):
    _patch_strategy(monkeypatch, SymbolScoreOpenLongStrategy)
    btc_csv = _write_candles(tmp_path / "btc.csv", ["100"])
    eth_csv = _write_candles(tmp_path / "eth.csv", ["50"])
    config = _multi_config(
        tmp_path,
        {"BTCUSDT": btc_csv, "ETHUSDT": eth_csv},
        portfolio_settings=PortfolioRiskSettings(
            max_open_positions=5,
            max_new_positions_per_cycle=1,
        ),
    )

    result = run_backtest(config, write_reports=False)

    open_decisions = [
        decision for decision in result.decisions if decision.decision_action == "OPEN_LONG"
    ]

    assert open_decisions[0].symbol == "ETHUSDT"
    assert open_decisions[0].final_status == "EXECUTED"
    assert open_decisions[0].opportunity_rank == 1
    assert open_decisions[0].cycle_candidate_count == 2
    assert open_decisions[1].symbol == "BTCUSDT"
    assert open_decisions[1].block_reason == "MULTIPLE_READY_BUT_PORTFOLIO_LIMITED"
    assert result.summary["cycle_competition"]["timestamps_with_multiple_candidates"] == 1


def test_backtesting_reserved_margin_blocks_reusing_same_capital(
    monkeypatch,
    tmp_path,
):
    _patch_strategy(monkeypatch, SymbolScoreOpenLongStrategy)
    btc_csv = _write_candles(tmp_path / "btc.csv", ["100"])
    eth_csv = _write_candles(tmp_path / "eth.csv", ["50"])
    config = _multi_config(
        tmp_path,
        {"BTCUSDT": btc_csv, "ETHUSDT": eth_csv},
        portfolio_settings=PortfolioRiskSettings(
            max_open_positions=5,
            max_new_positions_per_cycle=2,
        ),
        initial_balance=Decimal("120"),
        position_notional_usdt=Decimal("100"),
    )

    result = run_backtest(config, write_reports=False)

    blocked = [
        decision
        for decision in result.decisions
        if decision.block_reason == "BACKTEST_AVAILABLE_BALANCE_RESERVED"
    ]

    assert len(blocked) == 1
    assert blocked[0].governor_checks["reserved_capital_guard"] is True
    assert result.summary["blocked_by_reason"]["BACKTEST_AVAILABLE_BALANCE_RESERVED"] == 1


def test_backtesting_records_same_candle_stop_take_ambiguity(
    monkeypatch,
    tmp_path,
):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    csv_path = _write_candles_with_ohlc(
        tmp_path / "candles.csv",
        [
            ("1", "100", "100", "100", "100"),
            ("2", "100", "102", "98", "100"),
        ],
    )
    config = _config(
        tmp_path,
        csv_path,
        stop_loss_pct=Decimal("0.01"),
        take_profit_pct=Decimal("0.01"),
    )

    result = run_backtest(config, write_reports=False)

    assert result.trades[0].exit_reason == "ambiguous_stop_take_stop_loss"
    assert result.summary["ambiguous_stop_take_count"] == 1
    assert result.summary["same_candle_exit_policy"] == "CONSERVATIVE_STOP_FIRST"
    assert result.summary["execution_diagnostics"]["ambiguous_stop_take_by_symbol"] == {
        "BTCUSDT": 1
    }


def test_backtesting_reports_missing_csv_with_symbol(tmp_path):
    config = _multi_config(
        tmp_path,
        {
            "BTCUSDT": _write_candles(tmp_path / "btc.csv", ["100", "101"]),
            "ETHUSDT": tmp_path / "missing_eth.csv",
        },
        portfolio_settings=PortfolioRiskSettings(max_open_positions=5),
    )

    try:
        run_backtest(config, write_reports=False)
    except FileNotFoundError as error:
        assert "CSV nao encontrado para ETHUSDT" in str(error)
    else:
        raise AssertionError("Backtest deveria falhar com CSV ausente por simbolo")


def test_backtesting_strategy_runtime_evaluates_score_and_decide_once(monkeypatch):
    _patch_strategy(monkeypatch, StatefulSymbolStrategy)
    runtime = BacktestStrategyRuntime(
        BacktestStrategyConfig(),
        ["BTCUSDT"],
    )
    market = MarketSnapshot(
        symbol="BTCUSDT",
        price=Decimal("100"),
        prices=[Decimal("100")],
    )
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )

    evaluation = runtime.evaluate("BTCUSDT", market, position)

    assert evaluation.score.action == "OPEN_LONG"
    assert evaluation.decision.action is TradeAction.OPEN_LONG
    assert runtime._states["BTCUSDT"]["score_calls"] == 1
    assert runtime._states["BTCUSDT"]["decide_calls"] == 1


def test_backtesting_decisions_include_trend_context_and_report_groups(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongAfterTrendWarmupStrategy)
    config = _trend_config(
        _config(
            tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "101", "102", "103", "104"])
        )
    )

    result = run_backtest(config, write_reports=False)

    entry = next(
        decision
        for decision in result.decisions
        if decision.final_action == "OPEN_LONG" and decision.final_status == "EXECUTED"
    )
    assert entry.trend_direction == "UP"
    assert entry.market_regime == "TREND_UP"
    assert entry.trend_alignment == "WITH_TREND"
    assert entry.is_with_trend is True
    assert entry.is_against_trend is False
    assert "trend_context" in entry.metadata
    assert "by_market_regime" in result.summary
    assert "by_trend_alignment" in result.summary
    assert result.summary["by_trend_alignment"]["WITH_TREND"]["total_trades"] == 1
    assert "by_directional_regime" in result.summary
    assert "by_volatility_state" in result.summary


def test_backtesting_long_in_downtrend_is_against_trend(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongAfterTrendWarmupStrategy)
    config = _trend_config(
        _config(
            tmp_path, _write_candles(tmp_path / "candles.csv", ["103", "102", "101", "100", "99"])
        )
    )

    result = run_backtest(config, write_reports=False)

    entry = next(
        decision
        for decision in result.decisions
        if decision.final_action == "OPEN_LONG" and decision.final_status == "EXECUTED"
    )
    assert entry.trend_direction == "DOWN"
    assert entry.market_regime == "TREND_DOWN"
    assert entry.trend_alignment == "AGAINST_TREND"
    assert entry.is_with_trend is False
    assert entry.is_against_trend is True
    assert result.summary["by_trend_alignment"]["AGAINST_TREND"]["total_trades"] == 1


def test_backtesting_sizing_diagnostics_marks_untradable_symbol(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, AlwaysOpenLongStrategy)
    config = BacktestConfig(
        symbols=(
            BacktestSymbolConfig(
                symbol="BNBUSDT",
                csv_path=str(_write_candles(tmp_path / "bnb.csv", ["600", "601"])),
                rules=SymbolRules(
                    symbol="BNBUSDT",
                    quantity_step=Decimal("1"),
                    min_quantity=Decimal("1"),
                    min_notional=Decimal("5"),
                    price_tick=Decimal("0.10"),
                ),
                position_notional_usdt=Decimal("60"),
                stop_loss_pct=Decimal("0.50"),
                take_profit_pct=Decimal("0.50"),
            ),
        ),
        strategy=BacktestStrategyConfig(),
        portfolio_settings=PortfolioRiskSettings(max_open_positions=5),
        output_dir=str(tmp_path),
    )

    result = run_backtest(config, write_reports=False)

    diagnostic = result.summary["sizing_diagnostics"]["BNBUSDT"]
    assert diagnostic["tradable_with_config"] is False
    assert diagnostic["reason"] == "POSITION_NOTIONAL_ROUNDS_TO_ZERO"
    assert result.summary["sizing_status"] == "INVALID_FOR_SIZING"
    assert result.summary["operational_verdict"] == "INVALID_FOR_SIZING"
    assert "SYMBOL_NOT_TRADABLE_WITH_CONFIGURED_NOTIONAL" in result.summary["warnings"]
    assert result.rejected_signals == []
    assert result.decisions == []


def test_backtesting_can_run_untradable_symbol_when_explicitly_allowed(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, AlwaysOpenLongStrategy)
    config = BacktestConfig(
        symbols=(
            BacktestSymbolConfig(
                symbol="BNBUSDT",
                csv_path=str(_write_candles(tmp_path / "bnb.csv", ["600"])),
                rules=SymbolRules(
                    symbol="BNBUSDT",
                    quantity_step=Decimal("1"),
                    min_quantity=Decimal("1"),
                    min_notional=Decimal("5"),
                    price_tick=Decimal("0.10"),
                ),
                position_notional_usdt=Decimal("60"),
                stop_loss_pct=Decimal("0.50"),
                take_profit_pct=Decimal("0.50"),
            ),
        ),
        strategy=BacktestStrategyConfig(),
        portfolio_settings=PortfolioRiskSettings(max_open_positions=5),
        output_dir=str(tmp_path),
        allow_untradable_symbols=True,
        market_entry_policy="SIGNAL_CLOSE",
    )

    result = run_backtest(config, write_reports=False)

    assert result.summary["allow_untradable_symbols"] is True
    assert result.summary["sizing_status"] == "INVALID_FOR_SIZING"
    assert result.rejected_signals
    assert result.rejected_signals[0].layer == "EXCHANGE_RULE"


def test_backtesting_tradable_symbol_has_ok_sizing(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, OpenLongThenCloseStrategy)
    config = _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "101"]))

    result = run_backtest(config, write_reports=False)

    assert result.summary["sizing_diagnostics"]["BTCUSDT"]["tradable_with_config"] is True
    assert result.summary["sizing_status"] == "OK"


def test_backtesting_strategy_blockers_are_aggregated(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, BlockingMetadataStrategy)
    config = _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "101"]))

    result = run_backtest(config, write_reports=False)

    assert result.summary["blocked_setups_count"] == 2
    assert result.summary["strategy_blocked_by_reason"] == {"RSI_CROSS_UP_NOT_CONFIRMED": 2}
    assert result.summary["blocked_setups_by_side"] == {"LONG": 2}
    assert result.summary["rejected_signals_count"] == 0


def test_backtesting_timeframe_matching_strategy_passes(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, RequiresOneMinuteStrategy)
    config = _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "101"]))

    result = run_backtest(config, write_reports=False)

    assert result.summary["timeframe_match"] is True
    assert result.summary["timeframe_by_symbol"] == {"BTCUSDT": "1m"}
    assert result.summary["strategy_required_timeframes"] == ["1m"]


def test_backtesting_timeframe_mismatch_fails_clearly(monkeypatch, tmp_path):
    _patch_strategy(monkeypatch, RequiresFifteenMinuteStrategy)
    config = _config(tmp_path, _write_candles(tmp_path / "candles.csv", ["100", "101"]))

    try:
        run_backtest(config, write_reports=False)
    except ValueError as error:
        assert "Backtest timeframe incompatível" in str(error)
        assert "15m" in str(error)
    else:
        raise AssertionError("Backtest deveria falhar com timeframe incompativel")


def _patch_strategy(monkeypatch, strategy_type):
    import backtesting.engine.strategy_runtime as runtime

    monkeypatch.setattr(runtime, "load_strategy", lambda *_args, **_kwargs: strategy_type())


def _strategy_exit_metadata(
    *,
    stop_price: str,
    take_profit_price: str,
) -> dict[str, str]:
    return {
        "setup_math_side": "LONG",
        "entry_price": "100",
        "recommended_stop_price": stop_price,
        "recommended_take_profit_price": take_profit_price,
        "risk_abs": "0.60",
        "reward_abs": "1.20",
        "estimated_cost_abs": "0.16",
        "net_reward_abs": "1.04",
        "net_risk_abs": "0.76",
        "reward_risk_liquido": "1.368421052631578947368421053",
        "raw_reward_risk": "2",
        "risk_pct": "0.60",
        "reward_pct": "1.20",
        "cost_to_reward_ratio": "0.1333333333333333333333333333",
    }


def _config(
    tmp_path,
    csv_path,
    *,
    portfolio_settings: PortfolioRiskSettings | None = None,
    stop_loss_pct: Decimal = Decimal("0.50"),
    take_profit_pct: Decimal = Decimal("0.50"),
) -> BacktestConfig:
    return BacktestConfig(
        symbols=(
            BacktestSymbolConfig(
                symbol="BTCUSDT",
                csv_path=str(csv_path),
                rules=SymbolRules(
                    symbol="BTCUSDT",
                    quantity_step=Decimal("0.001"),
                    min_quantity=Decimal("0.001"),
                    min_notional=Decimal("5"),
                    price_tick=Decimal("0.10"),
                ),
                position_notional_usdt=Decimal("50"),
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
            ),
        ),
        strategy=BacktestStrategyConfig(
            mode="FIXED",
            strategy_module="strategies.futures.fixed.example",
            strategy_class="ExampleStrategy",
        ),
        portfolio_settings=portfolio_settings or PortfolioRiskSettings(max_open_positions=5),
        output_dir=str(tmp_path),
        market_entry_policy="SIGNAL_CLOSE",
        fill_settings=BacktestFillSettings(
            max_volume_participation=Decimal("1"),
            reliable_volume_participation=Decimal("1"),
            require_limit_trade_through=False,
            market_impact_bps_at_full_participation=Decimal("0"),
        ),
    )


def _trend_config(config: BacktestConfig) -> BacktestConfig:
    return replace(
        config,
        trend_fast_ma_period=2,
        trend_slow_ma_period=3,
        trend_slope_lookback=1,
        trend_volatility_lookback=4,
        trend_high_volatility_threshold_pct=10.0,
        trend_chop_threshold=0.55,
    )


def _multi_config(
    tmp_path,
    csv_paths_by_symbol,
    *,
    portfolio_settings: PortfolioRiskSettings,
    initial_balance: Decimal = Decimal("1000"),
    position_notional_usdt: Decimal = Decimal("50"),
) -> BacktestConfig:
    return BacktestConfig(
        symbols=tuple(
            BacktestSymbolConfig(
                symbol=symbol,
                csv_path=str(csv_path),
                rules=SymbolRules(
                    symbol=symbol,
                    quantity_step=Decimal("0.001"),
                    min_quantity=Decimal("0.001"),
                    min_notional=Decimal("5"),
                    price_tick=Decimal("0.10"),
                ),
                position_notional_usdt=position_notional_usdt,
                stop_loss_pct=Decimal("0.50"),
                take_profit_pct=Decimal("0.50"),
            )
            for symbol, csv_path in csv_paths_by_symbol.items()
        ),
        strategy=BacktestStrategyConfig(
            mode="FIXED",
            strategy_module="strategies.futures.fixed.example",
            strategy_class="ExampleStrategy",
        ),
        portfolio_settings=portfolio_settings,
        initial_balance=initial_balance,
        output_dir=str(tmp_path),
        market_entry_policy="SIGNAL_CLOSE",
        fill_settings=BacktestFillSettings(
            max_volume_participation=Decimal("1"),
            reliable_volume_participation=Decimal("1"),
            market_impact_bps_at_full_participation=Decimal("0"),
        ),
    )


def _write_candles(path, closes):
    rows = ["timestamp,open,high,low,close,volume"]
    for index, close in enumerate(closes, start=1):
        rows.append(f"{index},{close},{close},{close},{close},1")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _write_candles_with_timestamps(path, timestamp_closes):
    rows = ["timestamp,open,high,low,close,volume"]
    for timestamp, close in timestamp_closes:
        rows.append(f"{timestamp},{close},{close},{close},{close},1")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _write_candles_with_ohlc(path, rows_by_timestamp):
    rows = ["timestamp,open,high,low,close,volume"]
    for timestamp, open_, high, low, close in rows_by_timestamp:
        rows.append(f"{timestamp},{open_},{high},{low},{close},1")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path
