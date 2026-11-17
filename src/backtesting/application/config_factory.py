from __future__ import annotations

from decimal import Decimal

from backtesting.domain.config import (
    BacktestConfig,
    BacktestStrategyConfig,
    BacktestSymbolConfig,
)
from backtesting.domain.policies import (
    EndOfDataPolicy,
    LimitActivationPolicy,
    MarketEntryPolicy,
    SameCandleExitPolicy,
    SignalTimingPolicy,
    normalize_policy,
)
from backtesting.domain.realism import BacktestFillSettings, BacktestValidationSettings
from backtesting.domain.requests import BacktestRequest
from backtesting.infrastructure.funding_loader import load_funding_rates_from_csv
from config.symbol_context import PortfolioRiskSettings
from domain.symbol_rules import SymbolRules
from futures.performance.cost_model import CostModel


class BacktestConfigFactory:
    def from_request(self, request: BacktestRequest) -> BacktestConfig:
        _validate_policies(request)
        execution = request.execution
        portfolio = request.portfolio
        strategy_request = request.strategy
        symbols = tuple(
            BacktestSymbolConfig(
                symbol=symbol,
                csv_path=str(request.data.csv_by_symbol[symbol]),
                rules=SymbolRules(
                    symbol=symbol,
                    quantity_step=Decimal(str(execution.quantity_step)),
                    min_quantity=Decimal(str(execution.min_quantity)),
                    min_notional=Decimal(str(execution.min_notional)),
                    price_tick=Decimal(str(execution.price_tick)),
                ),
                timeframe=request.data.timeframe,
                position_notional_usdt=Decimal(str(execution.position_notional)),
                stop_loss_pct=Decimal(str(execution.stop_loss_pct)),
                take_profit_pct=Decimal(str(execution.take_profit_pct)),
                leverage=int(execution.leverage),
                csv_paths_by_timeframe=dict(request.data.csv_by_symbol_timeframe.get(symbol, {}))
                or None,
            )
            for symbol in request.symbols
        )
        return BacktestConfig(
            symbols=symbols,
            run_id=request.run_id,
            strategy=BacktestStrategyConfig(
                mode=strategy_request.mode,
                strategy_module=strategy_request.module,
                strategy_class=strategy_request.class_name,
                strategy_params=dict(strategy_request.params),
                global_model_path=strategy_request.global_model_path,
                symbol_model_paths=dict(strategy_request.symbol_model_paths),
                use_symbol_refined_models=strategy_request.use_symbol_refined_models,
                fallback_to_global_model=strategy_request.fallback_to_global_model,
                min_long_probability=strategy_request.min_long_probability,
                min_short_probability=strategy_request.min_short_probability,
                min_direction_gap=strategy_request.min_direction_gap,
                min_expected_value_bps=strategy_request.min_expected_value_bps,
            ),
            cost_model=CostModel.from_numbers(
                maker_fee_pct=execution.maker_fee_pct,
                taker_fee_pct=execution.taker_fee_pct,
                slippage_bps=execution.slippage_bps,
                spread_bps=execution.spread_bps,
                funding_enabled=execution.funding_enabled,
                default_funding_rate=execution.default_funding_rate,
            ),
            portfolio_settings=PortfolioRiskSettings(
                max_open_positions=int(portfolio.max_open_positions),
                max_total_exposure_usdt=float(portfolio.max_total_exposure),
                max_long_exposure_usdt=float(portfolio.max_long_exposure),
                max_short_exposure_usdt=float(portfolio.max_short_exposure),
                max_symbol_exposure_usdt=float(portfolio.max_symbol_exposure),
                max_group_exposure_usdt=float(portfolio.max_group_exposure),
                max_correlated_positions=int(portfolio.max_correlated_positions),
                max_new_positions_per_cycle=int(portfolio.max_new_positions_per_cycle),
            ),
            initial_balance=Decimal(str(portfolio.initial_balance)),
            output_dir=request.output.output_dir,
            allow_untradable_symbols=portfolio.allow_untradable_symbols,
            signal_timing_policy=request.policies.signal_timing,
            market_entry_policy=request.policies.market_entry,
            limit_activation_policy=request.policies.limit_activation,
            same_candle_exit_policy=request.policies.same_candle_exit,
            end_of_data_policy=request.policies.end_of_data,
            trend_context_mode=request.policies.trend_context_mode,
            limit_entry_timeout_candles=request.policies.limit_entry_timeout_candles,
            fill_settings=BacktestFillSettings(
                max_volume_participation=Decimal(str(execution.max_volume_participation)),
                reliable_volume_participation=Decimal(str(execution.reliable_volume_participation)),
                allow_partial_fills=bool(execution.allow_partial_fills),
                require_limit_trade_through=bool(execution.require_limit_trade_through),
                assume_limit_maker=bool(execution.assume_limit_maker),
                intrabar_limit_volume_fraction=Decimal(
                    str(execution.intrabar_limit_volume_fraction)
                ),
                market_impact_bps_at_full_participation=Decimal(
                    str(execution.market_impact_bps_at_full_participation)
                ),
                maintenance_margin_rate=Decimal(str(execution.maintenance_margin_rate)),
                liquidation_fee_pct=Decimal(str(execution.liquidation_fee_pct)),
            ),
            validation_settings=BacktestValidationSettings(
                evaluation_mode=request.policies.evaluation_mode,
                require_out_of_sample_for_approval=(
                    request.policies.require_out_of_sample_for_approval
                ),
                fail_on_model_training_overlap=(request.policies.fail_on_model_training_overlap),
                purge_candles=request.policies.purge_candles,
                embargo_candles=request.policies.embargo_candles,
                walk_forward_folds=request.policies.walk_forward_folds,
                min_profitable_walk_forward_folds=(
                    request.policies.min_profitable_walk_forward_folds
                ),
                bootstrap_samples=request.policies.bootstrap_samples,
                confidence_level=Decimal(str(request.policies.confidence_level)),
                random_seed=request.policies.random_seed,
                parameter_trials=request.policies.parameter_trials,
                min_probabilistic_sharpe=Decimal(str(request.policies.min_probabilistic_sharpe)),
                min_deflated_sharpe=Decimal(str(request.policies.min_deflated_sharpe)),
                max_risk_of_ruin=Decimal(str(request.policies.max_risk_of_ruin)),
                risk_of_ruin_horizon_trades=(request.policies.risk_of_ruin_horizon_trades),
                stress_cost_multipliers=tuple(
                    Decimal(str(value)) for value in request.policies.stress_cost_multipliers
                ),
                stress_extra_slippage_bps=tuple(
                    Decimal(str(value)) for value in request.policies.stress_extra_slippage_bps
                ),
            ),
            funding_rates_by_symbol={
                symbol: load_funding_rates_from_csv(path)
                for symbol, path in request.data.funding_csv_by_symbol.items()
            },
            funding_source_paths_by_symbol={
                symbol: str(path) for symbol, path in request.data.funding_csv_by_symbol.items()
            },
            fail_on_data_quality_warnings=request.data.fail_on_quality_warnings,
            data_start_timestamp=request.data.data_start_timestamp,
            data_end_timestamp=request.data.data_end_timestamp,
            evaluation_start_timestamp=request.data.evaluation_start_timestamp,
            evaluation_end_timestamp=request.data.evaluation_end_timestamp,
        )


def _validate_policies(request: BacktestRequest) -> None:
    normalize_policy(SignalTimingPolicy, request.policies.signal_timing)
    normalize_policy(MarketEntryPolicy, request.policies.market_entry)
    normalize_policy(LimitActivationPolicy, request.policies.limit_activation)
    normalize_policy(SameCandleExitPolicy, request.policies.same_candle_exit)
    normalize_policy(EndOfDataPolicy, request.policies.end_of_data)
    missing = [symbol for symbol in request.symbols if symbol not in request.data.csv_by_symbol]
    if missing:
        raise ValueError("BacktestRequest sem CSV para simbolos: " + ", ".join(missing))
    unknown_multi_timeframe = set(request.data.csv_by_symbol_timeframe) - set(request.symbols)
    unknown_funding = set(request.data.funding_csv_by_symbol) - set(request.symbols)
    unknown_primary = set(request.data.csv_by_symbol) - set(request.symbols)
    if unknown_primary:
        raise ValueError(
            "CSV principal configurado para simbolos ausentes: "
            + ", ".join(sorted(unknown_primary))
        )
    if unknown_multi_timeframe:
        raise ValueError(
            "CSV multi-timeframe configurado para simbolos ausentes: "
            + ", ".join(sorted(unknown_multi_timeframe))
        )
    if unknown_funding:
        raise ValueError(
            "Funding configurado para simbolos ausentes: " + ", ".join(sorted(unknown_funding))
        )
