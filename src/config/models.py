from dataclasses import dataclass, field

from config.enums import (
    ArchitectureMode,
    ExchangeEnvironment,
    ExecutionMode,
    ExitModelType,
    IntelligentActivationLevel,
    MarginType,
    ProtectionPolicy,
    ProtectionWorkingType,
    RecoveryAction,
)
from config.rest_weight_env import (
    DEFAULT_GUARD_MODE,
    DEFAULT_GUARD_RATIO,
    DEFAULT_WEIGHT_LIMIT_1M,
)
from config.smart_settings import SmartExecutionSettings, SmartSizingSettings
from config.snapshot_env import DEFAULT_CANDLE_KLINES_URL
from operational_module_modes import (
    MODULE_DYNAMIC_EXIT,
    MODULE_LEARNING_ADVISORY,
    MODULE_PORTFOLIO_ALLOCATOR,
    MODULE_SMART_EXECUTION,
    MODULE_SMART_SIZING,
    MODULE_STATE_RECONCILIATION,
    LearningAdvisoryMode,
    OperationalModuleMode,
    ReconciliationMode,
    SmartSizingMode,
    module_mode_effects,
    module_runtime_flags_from_modes,
    operational_module_mode_snapshot,
    resolve_configured_module_modes,
)
from trading_cadence import DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES


@dataclass(frozen=True)
class TradingSymbolConfig:
    symbol: str
    state_path: str
    lock_path: str
    journal_path: str


@dataclass(frozen=True)
class AppConfig:
    architecture_mode: ArchitectureMode
    intelligent_activation_level: IntelligentActivationLevel
    execution_mode: ExecutionMode
    environment: ExchangeEnvironment
    use_testnet: bool
    execute_orders: bool
    allow_real_trading: bool
    allow_real_user_data_cache_assisted: bool

    base_url: str
    api_key: str
    api_secret: str

    market_ws_enabled: bool
    market_ws_base_url: str
    market_ws_stream: str
    market_ws_heartbeat_seconds: float
    market_ws_max_price_age_seconds: float
    market_ws_reconnect_seconds: float
    market_ws_warmup_seconds: float
    market_ws_dirty_symbols_limit: int
    market_ws_price_window: int
    market_ws_max_symbols_per_drain: int
    user_data_stream_enabled: bool
    user_data_stream_shadow_only: bool
    user_data_stream_ws_base_url: str
    user_data_stream_keepalive_seconds: float
    user_data_stream_reconnect_seconds: float
    user_data_stream_heartbeat_seconds: float
    user_data_stream_max_pending_events: int
    user_data_stream_cache_audit_enabled: bool
    user_data_stream_cache_audit_max_age_seconds: float
    user_data_stream_cache_usage_shadow_enabled: bool
    user_data_stream_cache_usage_require_audit_match: bool
    user_data_stream_cache_usage_max_age_seconds: float
    user_data_stream_cache_usage_max_audit_age_seconds: float
    user_data_stream_cache_assisted_plan_shadow_enabled: bool
    user_data_cache_operational_preset: str
    user_data_cache_effective_preset: str
    user_data_stream_cache_position_enabled: bool
    user_data_stream_cache_account_enabled: bool
    user_data_stream_cache_open_orders_enabled: bool
    user_data_stream_cache_stale_guard_enabled: bool
    user_data_stream_cache_stale_guard_max_message_age_seconds: float
    user_data_stream_cache_stale_guard_require_listen_key: bool
    user_data_stream_cache_reconciliation_enabled: bool
    governor_observability_enabled: bool
    governor_cycle_metrics_interval_seconds: float
    governor_slow_cycle_ms: float
    governor_fast_lane_enabled: bool
    governor_fast_lane_max_symbols_per_cycle: int
    governor_fast_lane_cooldown_seconds: float
    governor_wakeup_enabled: bool
    governor_wakeup_cooldown_seconds: float
    governor_request_budget_enabled: bool
    governor_request_budget_max_heavy_checks_per_cycle: int
    governor_request_budget_open_position_override: bool
    governor_defensive_mode_enabled: bool
    governor_defensive_mode_shadow_only: bool
    governor_defensive_mode_min_shock_symbols: int
    governor_defensive_mode_max_stale_symbols: int
    governor_defensive_mode_max_price_age_seconds: float
    governor_defensive_mode_budget_pressure_enabled: bool
    governor_defensive_mode_enforcement_enabled: bool
    governor_defensive_mode_block_new_entries: bool

    market_shock_detector_enabled: bool
    market_shock_window_seconds: float
    market_shock_move_bps_threshold: float
    market_shock_critical_bps_threshold: float
    market_shock_cooldown_seconds: float

    symbol: str
    symbols: tuple[str, ...]
    max_active_symbols: int
    symbol_contexts: tuple[TradingSymbolConfig, ...]
    margin_type: MarginType
    leverage: int

    position_notional_usdt: float
    stop_loss_pct: float
    take_profit_pct: float
    loop_interval_seconds: float
    cancel_open_orders_when_flat: bool

    strategy_module: str
    strategy_class: str
    strategy_params: dict

    bot_order_prefix: str
    trade_journal_path: str
    trade_audit_path: str
    runtime_dir: str
    state_path: str
    lock_enabled: bool

    recover_on_boot: bool
    recovery_action: RecoveryAction
    cancel_orphan_orders_on_boot: bool
    protection_policy: ProtectionPolicy
    protection_working_type: ProtectionWorkingType

    max_consecutive_errors: int
    initial_backoff_seconds: float
    max_backoff_seconds: float

    max_daily_loss_usdt: float
    max_daily_trades: int
    max_consecutive_losses_guard: int
    cooldown_after_loss_seconds: float
    max_position_hold_seconds: float
    max_exposure_usdt: float
    stop_trading_after_daily_profit_usdt: float

    exit_model: ExitModelType
    atr_period: int
    atr_stop_multiplier: float
    atr_take_multiplier: float

    multi_symbol_enabled: bool
    symbol_profiles_path: str
    portfolio_risk_path: str

    trade_plan_shadow: bool
    trade_planning_enabled: bool
    cost_model_enabled: bool
    dynamic_exit_enabled: bool
    dynamic_sizing_enabled: bool
    portfolio_allocator_enabled: bool
    execution_planner_enabled: bool
    adaptive_manager_enabled: bool
    adaptive_execution_enabled: bool
    learning_store_enabled: bool
    learning_paper_activation_enabled: bool
    learning_paper_reduction_enabled: bool
    learning_paper_block_enabled: bool
    learning_testnet_observation_enabled: bool

    economic_gate_enabled: bool
    min_net_gain_bps: float
    min_net_reward_risk: float
    max_round_trip_cost_bps: float
    require_profitable_trade_plan: bool
    economic_exit_adjustment_enabled: bool
    max_economic_take_bps: float
    max_economic_take_atr_multiplier: float
    economic_exit_diagnostics_enabled: bool
    entry_execution_style: str
    limit_entry_enabled: bool
    limit_entry_timeout_seconds: float
    limit_entry_max_price_drift_bps: float
    limit_entry_fallback_to_market: bool
    limit_entry_post_only: bool
    market_entry_require_economic_gate: bool

    maker_fee_pct: float
    taker_fee_pct: float
    estimated_spread_bps: float
    estimated_slippage_bps: float
    funding_enabled: bool
    estimated_funding_rate: float
    candle_hub_mode: str = "AUTO"
    # ADR-004: public klines endpoint for every execution mode (production by default).
    candle_klines_url: str = DEFAULT_CANDLE_KLINES_URL
    strategy_runtime_mode: str = "FIXED"
    ai_probabilistic_enabled: bool = False
    ai_global_model_path: str = ""
    ai_symbol_model_paths: dict[str, str] = field(default_factory=dict)
    ai_use_symbol_refined_models: bool = True
    ai_fallback_to_global_model: bool = True
    ai_min_long_probability: float = 0.62
    ai_min_short_probability: float = 0.62
    ai_min_direction_gap: float = 0.18
    ai_min_expected_value_bps: float = 3.0
    ai_close_long_min_short_probability: float = 0.62
    ai_close_short_min_long_probability: float = 0.62
    ai_require_model_approved: bool = True
    # Closed candles an AI position is held before the model may close it (0 = horizon).
    ai_min_hold_candles: int = 0
    ai_close_on_probability_fade: bool = False
    trend_context_enabled: bool = True
    trend_context_mode: str = "OBSERVE"
    trend_fast_ma_period: int = 20
    trend_slow_ma_period: int = 50
    trend_slope_lookback: int = 5
    trend_volatility_lookback: int = 20
    trend_high_volatility_threshold_pct: float = 3.0
    trend_chop_threshold: float = 0.6
    # Timeframe of the closed candles the trend is computed on; "" = strategy primary timeframe.
    trend_timeframe: str = ""
    audit_runs_dir: str = "runtime/runs"
    audit_retention_days: int = 30
    audit_max_runs: int = 100
    audit_jsonl_enabled: bool = True
    audit_text_logs_enabled: bool = True
    audit_full_traceback: bool = False
    dynamic_exit_mode: OperationalModuleMode | str | None = None
    smart_sizing_mode: SmartSizingMode | str | None = None
    smart_execution_mode: OperationalModuleMode | str | None = None
    state_reconciliation_mode: ReconciliationMode | str | None = None
    learning_advisory_mode: LearningAdvisoryMode | str | None = None
    portfolio_allocator_mode: OperationalModuleMode | str | None = None
    operational_module_mode_sources: dict[str, str] = field(default_factory=dict)
    smart_sizing_settings: SmartSizingSettings = field(default_factory=SmartSizingSettings)
    smart_execution_settings: SmartExecutionSettings = field(default_factory=SmartExecutionSettings)
    # Operational snapshot provider (ALPHORA_SNAPSHOT_*; see config.snapshot_env).
    snapshot_deadline_seconds: float = 7.0
    snapshot_rest_timeout_seconds: float = 6.5
    snapshot_rest_connect_timeout_seconds: float = 1.0
    snapshot_rest_read_timeout_seconds: float = 4.5
    snapshot_rest_transport_timeout_seconds: float = 6.0
    snapshot_rest_max_attempts: int = 4
    snapshot_rest_retry_delay_seconds: float = 0.05
    snapshot_execution_max_age_seconds: float = 6.0
    snapshot_execution_max_skew_seconds: float = 5.0
    # None: the execution mode decides whether a run may start without the provider.
    snapshot_fallback_allowed: bool | None = None
    # Shared Binance REST weight count (see config.rest_weight_env, ADR-005).
    rest_weight_guard_mode: str = DEFAULT_GUARD_MODE
    rest_weight_guard_ratio: float = DEFAULT_GUARD_RATIO
    rest_weight_limit_1m: int = DEFAULT_WEIGHT_LIMIT_1M
    pre_dispatch_price_check_enabled: bool = True
    pre_dispatch_soft_price_drift_bps: float = 10.0
    pre_dispatch_hard_price_drift_bps: float = 30.0
    post_fill_protection_revalidation_enabled: bool = True
    post_fill_max_risk_increase_pct: float = 10.0
    protection_trigger_buffer_bps: float = 3.0
    governor_parallel_heavy_prefetch_enabled: bool = True
    governor_max_concurrent_symbol_snapshots: int = 3
    governor_prefetched_snapshot_max_age_seconds: float = 3.0
    governor_shared_private_cycle_ttl_seconds: float = 2.0
    snapshot_evaluation_max_age_seconds: float = 45.0
    # The .env file load_config() read ("" when none); reported at startup.
    env_file: str = ""
    market_type: str = "FUTURES"
    allow_custom_binance_host: bool = False
    # Anti-HFT: per-symbol minimum spacing between new entries (floor 60s).
    min_seconds_between_entries: float = DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES

    def __post_init__(self) -> None:
        resolution = resolve_configured_module_modes(
            raw_modes={
                MODULE_DYNAMIC_EXIT: self.dynamic_exit_mode,
                MODULE_SMART_SIZING: self.smart_sizing_mode,
                MODULE_SMART_EXECUTION: self.smart_execution_mode,
                MODULE_STATE_RECONCILIATION: self.state_reconciliation_mode,
                MODULE_LEARNING_ADVISORY: self.learning_advisory_mode,
                MODULE_PORTFOLIO_ALLOCATOR: self.portfolio_allocator_mode,
            },
            sources=self.operational_module_mode_sources,
            execution_mode=self.execution_mode.value,
        )
        flags = module_runtime_flags_from_modes(
            resolution.modes,
            execution_mode=self.execution_mode.value,
        )
        object.__setattr__(
            self,
            "dynamic_exit_mode",
            resolution.modes[MODULE_DYNAMIC_EXIT],
        )
        object.__setattr__(
            self,
            "smart_sizing_mode",
            resolution.modes[MODULE_SMART_SIZING],
        )
        object.__setattr__(
            self,
            "smart_execution_mode",
            resolution.modes[MODULE_SMART_EXECUTION],
        )
        object.__setattr__(
            self,
            "state_reconciliation_mode",
            resolution.modes[MODULE_STATE_RECONCILIATION],
        )
        object.__setattr__(
            self,
            "learning_advisory_mode",
            resolution.modes[MODULE_LEARNING_ADVISORY],
        )
        object.__setattr__(
            self,
            "portfolio_allocator_mode",
            resolution.modes[MODULE_PORTFOLIO_ALLOCATOR],
        )
        object.__setattr__(
            self,
            "operational_module_mode_sources",
            dict(resolution.sources),
        )
        for key, value in flags.items():
            object.__setattr__(self, key, value)

    @property
    def masked_api_key(self) -> str:
        if not self.api_key:
            return "<vazia>"

        if len(self.api_key) <= 10:
            return "***"

        return f"{self.api_key[:5]}...{self.api_key[-4:]}"

    @property
    def mutations_enabled(self) -> bool:
        return self.execution_mode.is_live

    @property
    def environment_key(self) -> str:
        return self.environment.value.lower()

    @property
    def multi_symbol_experimental(self) -> bool:
        return len(self.symbols) > 1

    @property
    def operational_module_modes(self) -> dict[str, str]:
        return operational_module_mode_snapshot(self)

    @property
    def operational_module_mode_effects(self) -> dict[str, dict]:
        return module_mode_effects(self.operational_module_modes)
