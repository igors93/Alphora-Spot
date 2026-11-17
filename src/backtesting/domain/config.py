from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import Any

from backtesting.domain.policies import (
    EndOfDataPolicy,
    LimitActivationPolicy,
    MarketEntryPolicy,
    SameCandleExitPolicy,
    SignalTimingPolicy,
    normalize_policy,
)
from backtesting.domain.realism import (
    BacktestFillSettings,
    BacktestValidationSettings,
    EquityPoint,
    FundingRatePoint,
)
from config.regime_policy_modes import normalize_regime_policy_mode
from config.smart_settings import SmartExecutionSettings, SmartSizingSettings
from config.symbol_context import PortfolioRiskSettings
from domain.symbol_rules import SymbolRules
from domain.trade_result import TradeResult
from futures.performance.cost_model import CostModel
from futures.performance.metrics import PerformanceMetrics
from futures.performance.readiness import ReadinessResult
from governor.models import GovernorDecision
from operational_module_modes import (
    default_operational_module_modes,
    module_modes_to_values,
)
from trading_cadence import (
    DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES,
    effective_max_daily_trades,
    effective_min_seconds_between_entries,
)

STRATEGY_MODE_FIXED = "FIXED"
STRATEGY_MODE_PROBABILISTIC_AI = "PROBABILISTIC_AI"


@dataclass(frozen=True)
class BacktestStrategyConfig:
    mode: str = STRATEGY_MODE_FIXED
    strategy_module: str = "strategies.futures.fixed.random_timed_signal"
    strategy_class: str = "RandomTimedSignalStrategy"
    strategy_params: dict[str, Any] = field(default_factory=dict)
    global_model_path: str | None = None
    symbol_model_paths: dict[str, str] = field(default_factory=dict)
    use_symbol_refined_models: bool = True
    fallback_to_global_model: bool = True
    min_long_probability: float = 0.62
    min_short_probability: float = 0.62
    min_direction_gap: float = 0.18
    min_expected_value_bps: float = 3.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", str(self.mode or STRATEGY_MODE_FIXED).upper().strip())
        object.__setattr__(self, "strategy_params", dict(self.strategy_params))
        object.__setattr__(
            self,
            "global_model_path",
            str(self.global_model_path).strip() if self.global_model_path else None,
        )
        object.__setattr__(
            self,
            "symbol_model_paths",
            {
                str(symbol).upper().strip(): str(path)
                for symbol, path in self.symbol_model_paths.items()
                if str(symbol).strip() and str(path).strip()
            },
        )
        for field_name in (
            "min_long_probability",
            "min_short_probability",
            "min_direction_gap",
        ):
            value = float(getattr(self, field_name))
            object.__setattr__(self, field_name, value)
            if not isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{field_name} deve ficar entre 0 e 1")
        expected_value = float(self.min_expected_value_bps)
        object.__setattr__(self, "min_expected_value_bps", expected_value)
        if not isfinite(expected_value):
            raise ValueError("min_expected_value_bps precisa ser finito")

    @property
    def normalized_mode(self) -> str:
        return str(self.mode or STRATEGY_MODE_FIXED).upper().strip()

    def load_target(self) -> tuple[str, str, dict[str, Any]]:
        if self.normalized_mode == STRATEGY_MODE_PROBABILISTIC_AI:
            params = dict(self.strategy_params)
            global_model_path = str(
                self.global_model_path or params.get("global_model_path") or ""
            ).strip()
            if not global_model_path:
                raise ValueError("Backtesting IA probabilistica exige global_model_path")

            symbol_model_paths = self.symbol_model_paths or params.get(
                "symbol_model_paths",
                {},
            )
            params.update(
                {
                    "global_model_path": global_model_path,
                    "symbol_model_paths": dict(symbol_model_paths),
                    "use_symbol_refined_models": bool(self.use_symbol_refined_models),
                    "fallback_to_global_model": bool(self.fallback_to_global_model),
                    "min_long_probability": float(self.min_long_probability),
                    "min_short_probability": float(self.min_short_probability),
                    "min_direction_gap": float(self.min_direction_gap),
                    "min_expected_value_bps": float(self.min_expected_value_bps),
                }
            )
            return (
                "strategies.futures.ai.probabilistic_strategy",
                "ProbabilisticAIStrategy",
                params,
            )

        if self.strategy_module.startswith("strategies.futures.ai") or self.strategy_class.endswith(
            "AIStrategy"
        ):
            raise ValueError(
                "Estrategias de IA devem usar strategy_mode=PROBABILISTIC_AI no backtesting"
            )

        return (
            self.strategy_module,
            self.strategy_class,
            dict(self.strategy_params),
        )


@dataclass(frozen=True)
class BacktestSymbolConfig:
    symbol: str
    csv_path: str
    rules: SymbolRules
    timeframe: str = "1m"
    position_notional_usdt: Decimal = Decimal("60")
    stop_loss_pct: Decimal = Decimal("0.01")
    take_profit_pct: Decimal = Decimal("0.02")
    leverage: int = 1
    # Optional: map of extra timeframe CSVs for multi-timeframe backtest.
    # When present, these are loaded alongside csv_path and delivered to the
    # strategy via market.candles without lookahead bias.
    csv_paths_by_timeframe: dict[str, str] | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.upper().strip()
        if not symbol:
            raise ValueError("BacktestSymbolConfig exige symbol")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(
            self,
            "timeframe",
            str(self.timeframe or "1m").strip() or "1m",
        )
        object.__setattr__(
            self,
            "position_notional_usdt",
            Decimal(str(self.position_notional_usdt)),
        )
        object.__setattr__(self, "stop_loss_pct", Decimal(str(self.stop_loss_pct)))
        object.__setattr__(
            self,
            "take_profit_pct",
            Decimal(str(self.take_profit_pct)),
        )
        leverage = int(self.leverage)
        if leverage < 1:
            raise ValueError("leverage precisa ser maior ou igual a 1")
        object.__setattr__(self, "leverage", leverage)
        normalized_secondary = {
            str(timeframe).strip(): str(path)
            for timeframe, path in dict(self.csv_paths_by_timeframe or {}).items()
            if str(timeframe).strip() and str(path).strip()
        }
        object.__setattr__(
            self,
            "csv_paths_by_timeframe",
            normalized_secondary or None,
        )
        if not self.position_notional_usdt.is_finite() or self.position_notional_usdt <= 0:
            raise ValueError("position_notional_usdt precisa ser maior que zero")
        if (
            not self.stop_loss_pct.is_finite()
            or not self.take_profit_pct.is_finite()
            or self.stop_loss_pct <= 0
            or self.take_profit_pct <= 0
            or self.stop_loss_pct >= 1
            or self.take_profit_pct >= 1
        ):
            raise ValueError("stop_loss_pct e take_profit_pct devem ficar entre 0 e 1")


@dataclass(frozen=True)
class BacktestConfig:
    symbols: tuple[BacktestSymbolConfig, ...]
    strategy: BacktestStrategyConfig
    run_id: str | None = None
    cost_model: CostModel = field(default_factory=CostModel)
    portfolio_settings: PortfolioRiskSettings = field(default_factory=PortfolioRiskSettings)
    initial_balance: Decimal = Decimal("1000")
    output_dir: str = "reports"
    min_sample_trades: int = 30
    min_profit_factor: Decimal = Decimal("1.2")
    min_expectancy: Decimal = Decimal("0")
    max_drawdown_pct: Decimal = Decimal("0.2")
    max_consecutive_losses: int = 5
    signal_timing_policy: str = SignalTimingPolicy.ON_CLOSE.value
    market_entry_policy: str = MarketEntryPolicy.NEXT_OPEN.value
    limit_activation_policy: str = LimitActivationPolicy.NEXT_CANDLE.value
    same_candle_exit_policy: str = SameCandleExitPolicy.CONSERVATIVE_STOP_FIRST.value
    end_of_data_policy: str = EndOfDataPolicy.CLOSE_AT_LAST_PRICE.value
    rank_cycle_candidates: bool = True
    allow_untradable_symbols: bool = False
    trend_context_enabled: bool = True
    trend_context_mode: str = "OBSERVE"
    trend_fast_ma_period: int = 20
    trend_slow_ma_period: int = 50
    trend_slope_lookback: int = 5
    trend_volatility_lookback: int = 20
    trend_high_volatility_threshold_pct: float = 3.0
    trend_chop_threshold: float = 0.6
    # Closed candles the trend is computed on, as in the Governor; "" = primary timeframe.
    trend_timeframe: str = ""
    operational_module_modes: dict[str, str] = field(default_factory=dict)
    smart_sizing_settings: SmartSizingSettings = field(default_factory=SmartSizingSettings)
    smart_execution_settings: SmartExecutionSettings = field(default_factory=SmartExecutionSettings)
    limit_entry_timeout_candles: int = 1
    fill_settings: BacktestFillSettings = field(default_factory=BacktestFillSettings)
    validation_settings: BacktestValidationSettings = field(
        default_factory=BacktestValidationSettings
    )
    funding_rates_by_symbol: dict[str, tuple[FundingRatePoint, ...]] = field(default_factory=dict)
    funding_source_paths_by_symbol: dict[str, str] = field(default_factory=dict)
    fail_on_data_quality_warnings: bool = False
    data_start_timestamp: str | None = None
    data_end_timestamp: str | None = None
    evaluation_start_timestamp: str | None = None
    evaluation_end_timestamp: str | None = None
    # Same anti-HFT cadence as live trading, so backtests stay reproducible.
    min_seconds_between_entries: float = DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES
    max_daily_trades: int = 0

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("Backtesting exige ao menos um simbolo")
        object.__setattr__(
            self,
            "min_seconds_between_entries",
            effective_min_seconds_between_entries(self.min_seconds_between_entries),
        )
        object.__setattr__(
            self,
            "max_daily_trades",
            effective_max_daily_trades(self.max_daily_trades),
        )
        normalized_run_id = str(self.run_id or "").strip() or None
        object.__setattr__(self, "run_id", normalized_run_id)
        seen_symbols: set[str] = set()
        duplicate_symbols: set[str] = set()
        for symbol_config in self.symbols:
            if symbol_config.symbol in seen_symbols:
                duplicate_symbols.add(symbol_config.symbol)
            seen_symbols.add(symbol_config.symbol)
        if duplicate_symbols:
            raise ValueError(
                "Backtesting recebeu simbolos duplicados: " + ", ".join(sorted(duplicate_symbols))
            )
        object.__setattr__(self, "initial_balance", Decimal(str(self.initial_balance)))
        if not self.initial_balance.is_finite() or self.initial_balance <= 0:
            raise ValueError("initial_balance precisa ser maior que zero")
        object.__setattr__(self, "min_sample_trades", max(1, int(self.min_sample_trades)))
        object.__setattr__(
            self,
            "min_profit_factor",
            Decimal(str(self.min_profit_factor)),
        )
        object.__setattr__(self, "min_expectancy", Decimal(str(self.min_expectancy)))
        object.__setattr__(
            self,
            "max_drawdown_pct",
            Decimal(str(self.max_drawdown_pct)),
        )
        for field_name in ("min_profit_factor", "min_expectancy", "max_drawdown_pct"):
            if not getattr(self, field_name).is_finite():
                raise ValueError(f"{field_name} precisa ser finito")
        if self.min_profit_factor < 0:
            raise ValueError("min_profit_factor nao pode ser negativo")
        if not Decimal("0") <= self.max_drawdown_pct <= Decimal("1"):
            raise ValueError("max_drawdown_pct deve ficar entre 0 e 1")
        object.__setattr__(
            self,
            "max_consecutive_losses",
            max(0, int(self.max_consecutive_losses)),
        )
        object.__setattr__(
            self,
            "signal_timing_policy",
            normalize_policy(SignalTimingPolicy, self.signal_timing_policy).value,
        )
        object.__setattr__(
            self,
            "market_entry_policy",
            normalize_policy(MarketEntryPolicy, self.market_entry_policy).value,
        )
        object.__setattr__(
            self,
            "limit_activation_policy",
            normalize_policy(LimitActivationPolicy, self.limit_activation_policy).value,
        )
        object.__setattr__(
            self,
            "same_candle_exit_policy",
            normalize_policy(SameCandleExitPolicy, self.same_candle_exit_policy).value,
        )
        object.__setattr__(
            self,
            "end_of_data_policy",
            normalize_policy(EndOfDataPolicy, self.end_of_data_policy).value,
        )
        object.__setattr__(
            self,
            "rank_cycle_candidates",
            bool(self.rank_cycle_candidates),
        )
        object.__setattr__(
            self,
            "trend_context_mode",
            normalize_regime_policy_mode(self.trend_context_mode),
        )
        modes = (
            self.operational_module_modes
            if self.operational_module_modes
            else default_operational_module_modes()
        )
        object.__setattr__(
            self,
            "operational_module_modes",
            module_modes_to_values(modes),
        )
        object.__setattr__(
            self,
            "limit_entry_timeout_candles",
            max(0, int(self.limit_entry_timeout_candles)),
        )
        normalized_funding = {
            str(symbol).upper().strip(): tuple(sorted(points, key=lambda point: point.timestamp))
            for symbol, points in self.funding_rates_by_symbol.items()
            if str(symbol).strip()
        }
        object.__setattr__(self, "funding_rates_by_symbol", normalized_funding)
        funding_paths = {
            str(symbol).upper().strip(): str(path)
            for symbol, path in self.funding_source_paths_by_symbol.items()
            if str(symbol).strip() and str(path).strip()
        }
        object.__setattr__(self, "funding_source_paths_by_symbol", funding_paths)
        unknown_funding_symbols = set(normalized_funding) - seen_symbols
        unknown_funding_symbols.update(set(funding_paths) - seen_symbols)
        if unknown_funding_symbols:
            raise ValueError(
                "Funding configurado para simbolos ausentes: "
                + ", ".join(sorted(unknown_funding_symbols))
            )
        timestamps = {
            "data_start_timestamp": _parse_optional_timestamp(self.data_start_timestamp),
            "data_end_timestamp": _parse_optional_timestamp(self.data_end_timestamp),
            "evaluation_start_timestamp": _parse_optional_timestamp(
                self.evaluation_start_timestamp
            ),
            "evaluation_end_timestamp": _parse_optional_timestamp(self.evaluation_end_timestamp),
        }
        data_start = timestamps["data_start_timestamp"]
        data_end = timestamps["data_end_timestamp"]
        evaluation_start = timestamps["evaluation_start_timestamp"]
        evaluation_end = timestamps["evaluation_end_timestamp"]
        if data_start and data_end and data_start > data_end:
            raise ValueError("data_start_timestamp nao pode superar data_end_timestamp")
        if evaluation_start and evaluation_end and evaluation_start > evaluation_end:
            raise ValueError("evaluation_start_timestamp nao pode superar evaluation_end_timestamp")
        if data_start and evaluation_start and evaluation_start < data_start:
            raise ValueError("evaluation_start_timestamp nao pode anteceder data_start_timestamp")
        if data_end and evaluation_end and evaluation_end > data_end:
            raise ValueError("evaluation_end_timestamp nao pode superar data_end_timestamp")

    @property
    def symbol_names(self) -> list[str]:
        return [item.symbol for item in self.symbols]


@dataclass(frozen=True)
class CandleEvent:
    timestamp: str
    symbol: str
    candle: Any
    # Dict mapping timeframe → tuple[Candle, ...] for all timeframes available
    # at the time of this event (no lookahead). Empty when multi-TF is not configured.
    candle_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestRejectedSignal:
    timestamp: str
    symbol: str
    action: str
    price: Decimal
    layer: str
    reason: str
    strategy_reason: str = ""
    model_source: str | None = None
    model_path: str | None = None
    long_score: float | None = None
    short_score: float | None = None
    action_candidate: str | None = None
    direction_candidate: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestDecisionRecord:
    timestamp: str
    symbol: str
    price: Decimal
    strategy_mode: str
    score: int | None
    priority: str | None
    score_action: str | None
    decision_action: str
    final_action: str
    final_status: str
    reason: str
    governor_allowed: bool | None = None
    governor_reason: str | None = None
    block_layer: str | None = None
    block_reason: str | None = None
    model_source: str | None = None
    model_path: str | None = None
    long_score: float | None = None
    short_score: float | None = None
    action_candidate: str | None = None
    direction_candidate: str | None = None
    trend_direction: str | None = None
    trend_strength: str | None = None
    market_regime: str | None = None
    primary_regime: str | None = None
    directional_regime: str | None = None
    volatility_state: str | None = None
    trend_confidence: float | None = None
    is_with_trend: bool = False
    is_against_trend: bool = False
    trend_alignment: str = "NEUTRAL_OR_UNKNOWN"
    governor_checks: dict[str, Any] = field(default_factory=dict)
    cycle_candidate_count: int = 0
    opportunity_rank: int | None = None
    opportunity_score: int | None = None
    opportunity_reasons: list[str] = field(default_factory=list)
    opportunity_diagnostics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestArtifactPaths:
    summary_json: Path
    trades_csv: Path
    decisions_jsonl: Path
    rejected_jsonl: Path
    equity_csv: Path | None = None
    manifest_json: Path | None = None


@dataclass
class BacktestRunResult:
    config: BacktestConfig
    trades: list[TradeResult]
    decisions: list[BacktestDecisionRecord]
    rejected_signals: list[BacktestRejectedSignal]
    metrics: PerformanceMetrics
    readiness: ReadinessResult
    summary: dict[str, Any]
    governor_decisions: list[GovernorDecision] = field(default_factory=list)
    data_diagnostics: dict[str, Any] = field(default_factory=dict)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    paths: BacktestArtifactPaths | None = None
    started_at: str | None = None
    finished_at: str | None = None


def _parse_optional_timestamp(value: str | None) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Timestamp de backtest invalido: {value}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
