from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from module_paths import qualify_module_path

REQUEST_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class BacktestStrategyRequest:
    mode: str = "FIXED"
    module: str = "strategies.futures.fixed.random_timed_signal"
    class_name: str = "RandomTimedSignalStrategy"
    params: Mapping[str, Any] = field(default_factory=dict)
    global_model_path: str | None = None
    symbol_model_paths: Mapping[str, str] = field(default_factory=dict)
    use_symbol_refined_models: bool = True
    fallback_to_global_model: bool = True
    min_long_probability: float = 0.62
    min_short_probability: float = 0.62
    min_direction_gap: float = 0.18
    min_expected_value_bps: float = 3.0

    def __post_init__(self) -> None:
        # Requests saved before the alphora namespace use the old module path.
        object.__setattr__(self, "module", qualify_module_path(self.module))
        object.__setattr__(
            self,
            "symbol_model_paths",
            {
                str(symbol).upper().strip(): str(path)
                for symbol, path in self.symbol_model_paths.items()
                if str(symbol).strip() and str(path).strip()
            },
        )


@dataclass(frozen=True, slots=True)
class BacktestDataRequest:
    csv_by_symbol: Mapping[str, str]
    timeframe: str = "1m"
    csv_by_symbol_timeframe: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    funding_csv_by_symbol: Mapping[str, str] = field(default_factory=dict)
    data_start_timestamp: str | None = None
    data_end_timestamp: str | None = None
    evaluation_start_timestamp: str | None = None
    evaluation_end_timestamp: str | None = None
    fail_on_quality_warnings: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "csv_by_symbol",
            {
                str(symbol).upper().strip(): str(path)
                for symbol, path in self.csv_by_symbol.items()
                if str(symbol).strip() and str(path).strip()
            },
        )
        object.__setattr__(
            self,
            "csv_by_symbol_timeframe",
            {
                str(symbol).upper().strip(): {
                    str(timeframe).strip(): str(path)
                    for timeframe, path in by_timeframe.items()
                    if str(timeframe).strip() and str(path).strip()
                }
                for symbol, by_timeframe in self.csv_by_symbol_timeframe.items()
                if str(symbol).strip()
            },
        )
        object.__setattr__(
            self,
            "funding_csv_by_symbol",
            {
                str(symbol).upper().strip(): str(path)
                for symbol, path in self.funding_csv_by_symbol.items()
                if str(symbol).strip() and str(path).strip()
            },
        )


@dataclass(frozen=True, slots=True)
class BacktestExecutionRequest:
    maker_fee_pct: str = "0.0002"
    taker_fee_pct: str = "0.0005"
    slippage_bps: str = "2"
    spread_bps: str = "1"
    funding_enabled: bool = True
    default_funding_rate: str = "0"
    quantity_step: str = "0.0001"
    min_quantity: str = "0.0001"
    min_notional: str = "50"
    price_tick: str = "0.10"
    position_notional: str = "60"
    stop_loss_pct: str = "0.01"
    take_profit_pct: str = "0.02"
    leverage: int = 1
    max_volume_participation: str = "0.05"
    reliable_volume_participation: str = "0.05"
    allow_partial_fills: bool = True
    require_limit_trade_through: bool = True
    assume_limit_maker: bool = True
    intrabar_limit_volume_fraction: str = "0.25"
    market_impact_bps_at_full_participation: str = "5"
    maintenance_margin_rate: str = "0.005"
    liquidation_fee_pct: str = "0.005"


@dataclass(frozen=True, slots=True)
class BacktestPortfolioRequest:
    initial_balance: str = "1000"
    max_open_positions: int = 1
    max_total_exposure: str = "0"
    max_long_exposure: str = "0"
    max_short_exposure: str = "0"
    max_symbol_exposure: str = "0"
    max_group_exposure: str = "0"
    max_correlated_positions: int = 0
    max_new_positions_per_cycle: int = 1
    allow_untradable_symbols: bool = False


@dataclass(frozen=True, slots=True)
class BacktestPolicyRequest:
    signal_timing: str = "ON_CLOSE"
    market_entry: str = "NEXT_OPEN"
    limit_activation: str = "NEXT_CANDLE"
    same_candle_exit: str = "CONSERVATIVE_STOP_FIRST"
    end_of_data: str = "CLOSE_AT_LAST_PRICE"
    trend_context_mode: str = "BLOCK_STRONG_COUNTER_TREND"
    limit_entry_timeout_candles: int = 1
    evaluation_mode: str = "IN_SAMPLE"
    require_out_of_sample_for_approval: bool = True
    fail_on_model_training_overlap: bool = True
    purge_candles: int = 0
    embargo_candles: int = 0
    walk_forward_folds: int = 5
    min_profitable_walk_forward_folds: int = 3
    bootstrap_samples: int = 1000
    confidence_level: str = "0.95"
    random_seed: int = 1729
    parameter_trials: int = 1
    min_probabilistic_sharpe: str = "0.95"
    min_deflated_sharpe: str = "0.95"
    max_risk_of_ruin: str = "0.01"
    risk_of_ruin_horizon_trades: int = 1000
    stress_cost_multipliers: tuple[str, ...] = ("1.0", "1.5", "2.0")
    stress_extra_slippage_bps: tuple[str, ...] = ("0", "2", "5")


@dataclass(frozen=True, slots=True)
class BacktestOutputRequest:
    output_dir: str = "reports"


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    symbols: tuple[str, ...]
    data: BacktestDataRequest
    strategy: BacktestStrategyRequest = field(default_factory=BacktestStrategyRequest)
    execution: BacktestExecutionRequest = field(default_factory=BacktestExecutionRequest)
    portfolio: BacktestPortfolioRequest = field(default_factory=BacktestPortfolioRequest)
    policies: BacktestPolicyRequest = field(default_factory=BacktestPolicyRequest)
    output: BacktestOutputRequest = field(default_factory=BacktestOutputRequest)
    schema_version: int = REQUEST_SCHEMA_VERSION
    run_id: str = field(default_factory=lambda: f"bt_{uuid4().hex}")

    def __post_init__(self) -> None:
        if self.schema_version != REQUEST_SCHEMA_VERSION:
            raise ValueError(f"BacktestRequest schema_version={self.schema_version} nao suportado")
        symbols = tuple(dict.fromkeys(symbol.upper().strip() for symbol in self.symbols))
        if not symbols:
            raise ValueError("BacktestRequest exige ao menos um simbolo")
        if len(symbols) != len(tuple(self.symbols)):
            raise ValueError("BacktestRequest recebeu simbolos duplicados")
        object.__setattr__(self, "symbols", symbols)


def request_hash(request: BacktestRequest) -> str:
    payload = asdict(request)
    payload.pop("run_id", None)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def request_from_json(payload: Mapping[str, Any]) -> BacktestRequest:
    version = int(payload.get("schema_version", REQUEST_SCHEMA_VERSION))
    if version != REQUEST_SCHEMA_VERSION:
        raise ValueError(f"schema_version nao suportado: {version}")
    return BacktestRequest(
        schema_version=version,
        run_id=str(payload.get("run_id") or f"bt_{uuid4().hex}"),
        symbols=tuple(payload.get("symbols") or ()),
        data=BacktestDataRequest(**dict(payload.get("data") or {})),
        strategy=BacktestStrategyRequest(**dict(payload.get("strategy") or {})),
        execution=BacktestExecutionRequest(**dict(payload.get("execution") or {})),
        portfolio=BacktestPortfolioRequest(**dict(payload.get("portfolio") or {})),
        policies=BacktestPolicyRequest(**dict(payload.get("policies") or {})),
        output=BacktestOutputRequest(**dict(payload.get("output") or {})),
    )
