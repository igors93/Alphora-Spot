import shutil
from decimal import Decimal
from pathlib import Path

import pytest

import utils.audit_logger as _audit_logger
from config import (
    AppConfig,
    ArchitectureMode,
    ExchangeEnvironment,
    ExecutionMode,
    ExitModelType,
    IntelligentActivationLevel,
    MarginType,
    ProtectionPolicy,
    ProtectionWorkingType,
    RecoveryAction,
    TradingSymbolConfig,
)
from domain.symbol_rules import SymbolRules
from utils.events import EventChannel, EventRecord, register_event_sink, unregister_event_sink


def _reset_observability_singletons() -> None:
    try:
        from observability.application.lifecycle import reset_exception_hooks_for_testing
        from observability.application.service import set_service
        from observability.bootstrap import reset_observability_bootstrap_for_testing
        from observability.infrastructure.text_logging import reset_text_logging_registry
        from utils.run_context import reset_run_context_for_testing

        reset_observability_bootstrap_for_testing()
        reset_exception_hooks_for_testing()
        reset_run_context_for_testing()
        set_service(None)
        reset_text_logging_registry()
    except Exception:
        pass


def _close_live_snapshot_providers() -> None:
    try:
        from market_data.snapshot_provider import close_live_snapshot_providers_for_testing

        close_live_snapshot_providers_for_testing()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_audit_logger(tmp_path_factory):
    """Isolate every test from the audit-logger's global state.

    The audit_logger module holds file-level singletons (_RUN_ID, _RUN_DIR,
    file handlers, summary counters) that persist across tests in the same
    pytest process.  Without this fixture, a test that exercises the governor
    (which calls initialize_run_audit) installs file handlers pointing to a
    tmp directory; subsequent tests then write ERROR records into that
    directory — the "falha isolada / ETHUSDT" noise seen in operational logs.

    This fixture runs around every test (autouse), resets all mutable state
    before the test body, and resets again afterwards so any accidental
    initialize_run_audit call inside the test does not bleed into the next.
    The run directories live in a per-test folder that is removed afterwards:
    without one, reset_for_testing() leaves a new mkdtemp folder per call.
    """
    runs_root = tmp_path_factory.mktemp("audit_runs")
    _close_live_snapshot_providers()
    _audit_logger.reset_for_testing(runs_root)
    _reset_observability_singletons()
    yield
    _close_live_snapshot_providers()
    _audit_logger.reset_for_testing(runs_root)
    _reset_observability_singletons()
    shutil.rmtree(runs_root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_rest_weight_ledger():
    """A simulated 429/418 must not leave the shared REST back-off running for the next test."""
    from binance.rest_weight import shared_rest_weight_ledger

    shared_rest_weight_ledger().reset()
    yield
    shared_rest_weight_ledger().reset()


@pytest.fixture
def symbol_rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_tick=Decimal("0.10"),
    )


@pytest.fixture
def config_factory(tmp_path):
    def _factory(**kwargs) -> AppConfig:
        return make_config(tmp_path, **kwargs)

    return _factory


def make_config(
    tmp_path: Path,
    *,
    architecture_mode: ArchitectureMode = ArchitectureMode.INTELLIGENT_FLOW,
    intelligent_activation_level: IntelligentActivationLevel = (
        IntelligentActivationLevel.SHADOW_ONLY
    ),
    execution_mode: ExecutionMode = ExecutionMode.DRY_RUN,
    protection_policy: ProtectionPolicy = ProtectionPolicy.AUTO_REPAIR,
    position_notional_usdt: float = 50.0,
    max_daily_loss_usdt: float = 0.0,
    max_daily_trades: int = 0,
    max_consecutive_losses_guard: int = 0,
    cooldown_after_loss_seconds: float = 0.0,
    max_position_hold_seconds: float = 0.0,
    max_exposure_usdt: float = 0.0,
    stop_trading_after_daily_profit_usdt: float = 0.0,
    dynamic_exit_enabled: bool = False,
    dynamic_sizing_enabled: bool = False,
    execution_planner_enabled: bool = False,
    adaptive_manager_enabled: bool = False,
    adaptive_execution_enabled: bool = False,
    learning_store_enabled: bool = False,
    learning_paper_activation_enabled: bool = False,
    learning_paper_reduction_enabled: bool = False,
    learning_paper_block_enabled: bool = False,
    learning_testnet_observation_enabled: bool = False,
    economic_exit_adjustment_enabled: bool = True,
    max_economic_take_bps: float = 120.0,
    max_economic_take_atr_multiplier: float = 3.0,
    economic_exit_diagnostics_enabled: bool = True,
    candle_hub_mode: str = "OFF",
    symbols: tuple[str, ...] = ("BTCUSDT",),
) -> AppConfig:
    environment = (
        ExchangeEnvironment.REAL
        if execution_mode is ExecutionMode.REAL_LIVE
        else ExchangeEnvironment.TESTNET
    )
    dynamic_exit_mode = "ACTIVE" if dynamic_exit_enabled else "OFF"
    smart_sizing_mode = "ACTIVE" if dynamic_sizing_enabled else "OFF"
    smart_execution_mode = (
        "ACTIVE"
        if execution_planner_enabled or adaptive_manager_enabled or adaptive_execution_enabled
        else "OFF"
    )
    if learning_paper_block_enabled:
        learning_advisory_mode = "PAPER_BLOCK"
    elif learning_paper_reduction_enabled or learning_paper_activation_enabled:
        learning_advisory_mode = "REDUCE_PRIORITY"
    elif learning_testnet_observation_enabled:
        learning_advisory_mode = "OBSERVE"
    elif learning_store_enabled:
        learning_advisory_mode = "SHADOW"
    else:
        learning_advisory_mode = "OFF"
    return AppConfig(
        architecture_mode=architecture_mode,
        intelligent_activation_level=intelligent_activation_level,
        execution_mode=execution_mode,
        environment=environment,
        use_testnet=environment is ExchangeEnvironment.TESTNET,
        execute_orders=execution_mode.is_live,
        allow_real_trading=execution_mode is ExecutionMode.REAL_LIVE,
        allow_real_user_data_cache_assisted=False,
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
        market_ws_enabled=True,
        market_ws_base_url="wss://stream.binancefuture.com",
        market_ws_stream="bookTicker",
        market_ws_heartbeat_seconds=60.0,
        market_ws_max_price_age_seconds=3.0,
        market_ws_reconnect_seconds=5.0,
        market_ws_warmup_seconds=0.0,
        market_ws_dirty_symbols_limit=500,
        market_ws_price_window=120,
        market_ws_max_symbols_per_drain=50,
        user_data_stream_enabled=False,
        user_data_stream_shadow_only=True,
        user_data_stream_ws_base_url="",
        user_data_stream_keepalive_seconds=30 * 60,
        user_data_stream_reconnect_seconds=5.0,
        user_data_stream_heartbeat_seconds=60.0,
        user_data_stream_max_pending_events=500,
        user_data_stream_cache_audit_enabled=True,
        user_data_stream_cache_audit_max_age_seconds=30.0,
        user_data_stream_cache_usage_shadow_enabled=True,
        user_data_stream_cache_usage_require_audit_match=True,
        user_data_stream_cache_usage_max_age_seconds=10.0,
        user_data_stream_cache_usage_max_audit_age_seconds=60.0,
        user_data_stream_cache_assisted_plan_shadow_enabled=True,
        user_data_cache_operational_preset="",
        user_data_cache_effective_preset="",
        user_data_stream_cache_position_enabled=False,
        user_data_stream_cache_account_enabled=False,
        user_data_stream_cache_open_orders_enabled=False,
        user_data_stream_cache_stale_guard_enabled=True,
        user_data_stream_cache_stale_guard_max_message_age_seconds=30.0,
        user_data_stream_cache_stale_guard_require_listen_key=True,
        user_data_stream_cache_reconciliation_enabled=True,
        governor_observability_enabled=True,
        governor_cycle_metrics_interval_seconds=0.0,
        governor_slow_cycle_ms=1500.0,
        governor_fast_lane_enabled=True,
        governor_fast_lane_max_symbols_per_cycle=3,
        governor_fast_lane_cooldown_seconds=2.0,
        governor_wakeup_enabled=True,
        governor_wakeup_cooldown_seconds=1.0,
        governor_request_budget_enabled=True,
        governor_request_budget_max_heavy_checks_per_cycle=3,
        governor_request_budget_open_position_override=True,
        governor_defensive_mode_enabled=True,
        governor_defensive_mode_shadow_only=True,
        governor_defensive_mode_min_shock_symbols=2,
        governor_defensive_mode_max_stale_symbols=0,
        governor_defensive_mode_max_price_age_seconds=5.0,
        governor_defensive_mode_budget_pressure_enabled=True,
        governor_defensive_mode_enforcement_enabled=False,
        governor_defensive_mode_block_new_entries=True,
        market_shock_detector_enabled=True,
        market_shock_window_seconds=3.0,
        market_shock_move_bps_threshold=50.0,
        market_shock_critical_bps_threshold=120.0,
        market_shock_cooldown_seconds=10.0,
        symbol=symbols[0],
        symbols=symbols,
        max_active_symbols=len(symbols),
        symbol_contexts=(
            TradingSymbolConfig(
                symbol="BTCUSDT",
                state_path=str(tmp_path / "runtime" / "state.json"),
                lock_path=str(tmp_path / "runtime" / "alphora-btcusdt-testnet.lock"),
                journal_path=str(tmp_path / "data" / "trades.jsonl"),
            ),
        ),
        margin_type=MarginType.ISOLATED,
        leverage=1,
        position_notional_usdt=position_notional_usdt,
        stop_loss_pct=0.01,
        take_profit_pct=0.02,
        loop_interval_seconds=0.01,
        cancel_open_orders_when_flat=True,
        strategy_module="strategies.futures.fixed.random_timed_signal",
        strategy_class="RandomTimedSignalStrategy",
        strategy_params={},
        bot_order_prefix="ALPHORA",
        trade_journal_path=str(tmp_path / "data" / "trades.jsonl"),
        trade_audit_path=str(tmp_path / "data" / "trade_audit.jsonl"),
        runtime_dir=str(tmp_path / "runtime"),
        state_path=str(tmp_path / "runtime" / "state.json"),
        lock_enabled=False,
        recover_on_boot=True,
        recovery_action=RecoveryAction.PROTECT,
        cancel_orphan_orders_on_boot=True,
        protection_policy=protection_policy,
        protection_working_type=ProtectionWorkingType.MARK_PRICE,
        max_consecutive_errors=3,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.02,
        max_daily_loss_usdt=max_daily_loss_usdt,
        max_daily_trades=max_daily_trades,
        max_consecutive_losses_guard=max_consecutive_losses_guard,
        cooldown_after_loss_seconds=cooldown_after_loss_seconds,
        max_position_hold_seconds=max_position_hold_seconds,
        max_exposure_usdt=max_exposure_usdt,
        stop_trading_after_daily_profit_usdt=stop_trading_after_daily_profit_usdt,
        exit_model=ExitModelType.FIXED_PCT,
        atr_period=14,
        atr_stop_multiplier=1.5,
        atr_take_multiplier=2.0,
        multi_symbol_enabled=False,
        symbol_profiles_path=str(tmp_path / ".alphora_symbols.local.json"),
        portfolio_risk_path=str(tmp_path / ".alphora_portfolio.local.json"),
        trade_plan_shadow=True,
        trade_planning_enabled=False,
        cost_model_enabled=False,
        dynamic_exit_enabled=dynamic_exit_enabled,
        dynamic_sizing_enabled=dynamic_sizing_enabled,
        portfolio_allocator_enabled=False,
        execution_planner_enabled=execution_planner_enabled,
        adaptive_manager_enabled=adaptive_manager_enabled,
        adaptive_execution_enabled=adaptive_execution_enabled,
        learning_store_enabled=learning_store_enabled,
        learning_paper_activation_enabled=learning_paper_activation_enabled,
        learning_paper_reduction_enabled=learning_paper_reduction_enabled,
        learning_paper_block_enabled=learning_paper_block_enabled,
        learning_testnet_observation_enabled=learning_testnet_observation_enabled,
        economic_gate_enabled=True,
        min_net_gain_bps=5.0,
        min_net_reward_risk=1.5,
        max_round_trip_cost_bps=20.0,
        require_profitable_trade_plan=True,
        economic_exit_adjustment_enabled=economic_exit_adjustment_enabled,
        max_economic_take_bps=max_economic_take_bps,
        max_economic_take_atr_multiplier=max_economic_take_atr_multiplier,
        economic_exit_diagnostics_enabled=economic_exit_diagnostics_enabled,
        entry_execution_style="MARKET_SAFE",
        limit_entry_enabled=False,
        limit_entry_timeout_seconds=3.0,
        limit_entry_max_price_drift_bps=5.0,
        limit_entry_fallback_to_market=False,
        limit_entry_post_only=True,
        market_entry_require_economic_gate=True,
        maker_fee_pct=0.0002,
        taker_fee_pct=0.0005,
        estimated_spread_bps=1.0,
        estimated_slippage_bps=2.0,
        funding_enabled=True,
        estimated_funding_rate=0.0,
        candle_hub_mode=candle_hub_mode,
        dynamic_exit_mode=dynamic_exit_mode,
        smart_sizing_mode=smart_sizing_mode,
        smart_execution_mode=smart_execution_mode,
        learning_advisory_mode=learning_advisory_mode,
    )


class _AuditCapture:
    def __init__(self, component: str) -> None:
        self.component = component
        self.events: list[tuple[str, dict]] = []

    def accepts(self, record: EventRecord) -> bool:
        return record.channel is EventChannel.AUDIT and record.component == self.component

    def write(self, record: EventRecord) -> None:
        self.events.append((record.event_type, dict(record.payload)))


@pytest.fixture
def capture_audit_events():
    """``capture_audit_events(component)`` -> the (event_type, payload) of its AUDIT events."""
    captures: list[_AuditCapture] = []

    def capture(component: str) -> list[tuple[str, dict]]:
        sink = _AuditCapture(component)
        captures.append(sink)
        register_event_sink(sink)
        return sink.events

    yield capture
    for sink in captures:
        unregister_event_sink(sink)
