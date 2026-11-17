"""Composition root for the Governor runtime.

This module is the only place that decides which long-lived Governor components
are created and which instances are shared across the execution stack.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from binance.futures_client import BinanceFuturesClient
from binance.rest_weight import shared_rest_weight_ledger
from config import AppConfig
from config.symbol_context import PortfolioRiskSettings, SymbolTradingConfig
from config.ws_endpoints import resolve_public_market_ws_base_url, resolve_user_data_ws_base_url
from domain.models import FuturesPosition
from domain.strategy_requirements import StrategyDataRequirements
from domain.timeframe import is_supported_timeframe, normalize_timeframe
from execution.state_reconciliation import StateReconciliationEngine
from execution.trade_audit import TradeAuditRecorder
from futures.execution.order_policy import OrderExecutionPolicy
from futures.planning.economic_gate import EconomicTradeGate
from futures.planning.trade_plan_execution_validator import TradePlanExecutionValidator
from futures.trend import TrendContextEngine
from governor.adaptive_adjustment_activation import AdaptiveAdjustmentActivationPolicy
from governor.adaptive_adjustment_plan import AdaptiveAdjustmentPlanner
from governor.adaptive_manager import AdaptivePositionManager
from governor.control_requests import GovernorControlRequests
from governor.cycle.telemetry import CycleMetricsState
from governor.cycle.wakeup import MarketWakeup
from governor.daily_portfolio_metrics import DailyPortfolioMetricsReader
from governor.evaluation.stage_telemetry import StageTelemetry
from governor.execution_engine import ExecutionEngine
from governor.governor_helpers import _enum_value
from governor.hot_symbol_worker import HotSymbolWorker
from governor.market_data_hub import MarketDataHub
from governor.market_data_router import MarketDataRouter
from governor.opportunity_ranker import OpportunityRanker
from governor.portfolio_risk_engine import PortfolioRiskEngine
from governor.portfolio_snapshot_provider import PortfolioSnapshotProvider
from governor.pre_execution_guard import PreExecutionGuard
from governor.services.learning import LearningReportState
from governor.services.symbol_health import build_symbol_quarantine
from governor.smart_execution import SmartExecutionEngine
from governor.smart_sizing import SmartSizingEngine
from governor.state_writer import GovernorStateWriter
from governor.strategy_coordinator import StrategyCoordinator
from governor.symbol_state_machine import SymbolStateTracker
from governor.trade_pipeline import TradePipeline
from governor.user_data_cache import UserDataShadowCache
from governor.user_data_stream import UserDataStreamHub
from market_data.candle_hub import (
    CANDLE_HUB_MODE_ACTIVE_PAPER,
    CANDLE_HUB_MODE_ACTIVE_REAL,
    CANDLE_HUB_MODE_ACTIVE_TESTNET,
    CANDLE_HUB_MODE_OFF,
    CandleDataHub,
)
from market_data.candle_provider import CandleDataProvider
from operational_module_modes import operational_module_modes_from_config
from scanner.priority_scheduler import PriorityScheduler
from trading.intelligent_execution_planner import IntelligentExecutionPlanner
from trading.intelligent_exit_activation import IntelligentExitActivationPolicy
from trading.intelligent_exit_builder import IntelligentExitBuilder
from trading.intelligent_exit_comparator import IntelligentExitComparator
from trading.intelligent_order_activation import IntelligentOrderActivationPolicy
from trading.intelligent_order_builder import IntelligentOrderBuilder
from trading.intelligent_order_comparator import IntelligentOrderComparator
from trading.learning_advisory import LearningAdvisoryEngine
from trading.learning_execution_shadow import LearningExecutionShadowPolicy
from trading.learning_paper_activation import LearningPaperActivationSettings
from trading.learning_paper_execution import LearningPaperExecutionPolicy
from trading.learning_plan_guard import LearningPlanGuard
from trading.learning_report import TradePlanLearningReporter
from trading.learning_shadow_impact_report import LearningShadowImpactReporter
from trading.learning_testnet_execution import (
    TESTNET_EXECUTION_BLOCK_LEVEL,
    TESTNET_EXECUTION_REDUCE_LEVEL,
    LearningTestnetExecutionPolicy,
    LearningTestnetExecutionSettings,
)
from trading.learning_testnet_observation import (
    LearningTestnetObservationPolicy,
    LearningTestnetObservationSettings,
)
from utils.events import EventChannel, observe_event
from utils.journals import JournalRegistry
from utils.logger import get_logger
from utils.trade_journal import TradeJournal

EventCallback = Callable[[dict[str, Any]], None]


class GovernorBindings(Protocol):
    """Callbacks supplied by ``AlphoraGovernor`` to infrastructure components."""

    def _on_user_data_stream_event(self, event: dict[str, Any]) -> None: ...

    def _position_for_symbol(self, symbol_config: SymbolTradingConfig) -> FuturesPosition: ...

    def _daily_portfolio_metrics(self) -> dict[str, Any]: ...

    def _safe_account_info(self) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class ComponentGroup:
    """Named attributes in one architectural responsibility group."""

    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class GovernorComponents:
    """All long-lived Governor attributes, grouped by responsibility."""

    data: ComponentGroup
    decision: ComponentGroup
    risk: ComponentGroup
    execution: ComponentGroup
    state: ComponentGroup

    def items(self) -> Iterable[tuple[str, Any]]:
        seen: set[str] = set()
        for group in (self.data, self.decision, self.risk, self.execution, self.state):
            for name, value in group.values.items():
                if name in seen:
                    raise ValueError(f"Duplicate Governor component attribute: {name}")
                seen.add(name)
                yield name, value

    def install_on(self, governor: Any) -> None:
        for name, value in self.items():
            setattr(governor, name, value)


def build_governor_components(
    config: AppConfig,
    client: BinanceFuturesClient,
    *,
    governor: GovernorBindings,
    symbol_configs: Iterable[SymbolTradingConfig],
    portfolio_settings: PortfolioRiskSettings | None = None,
    max_symbols_per_cycle: int = 10,
) -> GovernorComponents:
    """Build the complete Governor object graph without starting any runtime loop."""

    enabled_symbols = tuple(item for item in symbol_configs if item.enabled)
    if not enabled_symbols:
        raise ValueError("Governador exige pelo menos um perfil habilitado")
    if getattr(client, "spot_mode", False):
        for symbol_config in enabled_symbols:
            client.register_bot_order_prefix(symbol_config.bot_order_prefix)

    logger = get_logger("governor.alphora_governor")
    settings = portfolio_settings or PortfolioRiskSettings()
    operational_mode = (
        "GOVERNOR_SINGLE_SYMBOL" if len(enabled_symbols) == 1 else "GOVERNOR_MULTI_SYMBOL"
    )
    data_dir = Path(config.trade_journal_path).parent
    governor_events_path = data_dir / "governor_events.jsonl"

    # Shared ownership is established before the execution facade is built: the registry
    # owns every journal file of the process, one TradeJournal per path.
    journals = JournalRegistry()
    events_journal = journals.journal(governor_events_path)
    trade_audit_recorder = TradeAuditRecorder.from_config(
        config,
        operational_mode=operational_mode,
        journals=journals,
    )
    state_reconciliation_engine = StateReconciliationEngine()

    data, strategy_coordinator = _data_components(
        config,
        client,
        governor=governor,
        symbol_configs=enabled_symbols,
        logger=logger,
    )
    market_data_router: MarketDataRouter = data["market_data_router"]
    execution_engine = ExecutionEngine(
        base_config=config,
        client=client,
        operational_mode=operational_mode,
        exchange_info_provider=market_data_router.get_exchange_info,
        governor_journal=events_journal,
        trade_audit_recorder=trade_audit_recorder,
        state_reconciliation_engine=state_reconciliation_engine,
        journals=journals,
    )
    economic_trade_gate = EconomicTradeGate.from_config(config)
    learning_settings = _LearningSettings.from_config(config)
    daily_metrics_reader = DailyPortfolioMetricsReader(
        config=config,
        symbol_configs=list(enabled_symbols),
        events_journal=events_journal,
    )
    portfolio_snapshot_provider = PortfolioSnapshotProvider(
        symbol_configs=list(enabled_symbols),
        position_for_symbol=governor._position_for_symbol,
        daily_metrics=governor._daily_portfolio_metrics,
        account_info=governor._safe_account_info,
    )
    data["scheduler"].ensure_symbols([item.symbol for item in enabled_symbols], time.time())

    return GovernorComponents(
        data=ComponentGroup(data),
        decision=ComponentGroup(
            _decision_components(config, strategy_coordinator, learning_settings)
        ),
        risk=ComponentGroup(
            {
                "portfolio_settings": settings,
                "economic_trade_gate": economic_trade_gate,
                "trade_plan_execution_validator": TradePlanExecutionValidator(economic_trade_gate),
                "order_execution_policy": OrderExecutionPolicy.from_config(config),
                "portfolio_risk_engine": PortfolioRiskEngine(settings),
                "pre_execution_guard": PreExecutionGuard(),
                "operational_module_modes": operational_module_modes_from_config(config),
                "smart_sizing_engine": SmartSizingEngine(
                    settings=getattr(config, "smart_sizing_settings", None)
                ),
                "smart_execution_engine": SmartExecutionEngine(
                    settings=getattr(config, "smart_execution_settings", None)
                ),
                "state_reconciliation_engine": state_reconciliation_engine,
                "daily_metrics_reader": daily_metrics_reader,
                "portfolio_snapshot_provider": portfolio_snapshot_provider,
                "symbol_quarantine": build_symbol_quarantine(config),
            }
        ),
        execution=ComponentGroup(
            {
                "execution_engine": execution_engine,
                "trade_audit_recorder": trade_audit_recorder,
                "_risk_guard_for_symbol": execution_engine.risk_guard_for_symbol,
            }
        ),
        state=ComponentGroup(
            _state_components(
                config,
                logger=logger,
                journals=journals,
                events_journal=events_journal,
                governor_events_path=governor_events_path,
                data_dir=data_dir,
                max_symbols_per_cycle=max_symbols_per_cycle,
                learning_settings=learning_settings,
            )
        ),
    )


@dataclass(frozen=True)
class _LearningSettings:
    """Activation settings of the learning layer, read once from the config."""

    enabled: bool
    paper: LearningPaperActivationSettings
    testnet_observation: LearningTestnetObservationSettings
    testnet_execution: LearningTestnetExecutionSettings

    @classmethod
    def from_config(cls, config: AppConfig) -> _LearningSettings:
        return cls(
            enabled=config.learning_store_enabled,
            paper=LearningPaperActivationSettings.from_config(config),
            testnet_observation=LearningTestnetObservationSettings.from_config(config),
            testnet_execution=LearningTestnetExecutionSettings.from_config(config),
        )


def _data_components(
    config: AppConfig,
    client: BinanceFuturesClient,
    *,
    governor: GovernorBindings,
    symbol_configs: tuple[SymbolTradingConfig, ...],
    logger: Any,
) -> tuple[dict[str, Any], StrategyCoordinator]:
    """Market and private data sources, and the strategy coordinator they are built for."""

    scheduler = PriorityScheduler(max_interval_seconds=300, max_data_age_seconds=30)
    market_wakeup = MarketWakeup(config=config)
    market_data_hub = _build_market_data_hub(
        config=config,
        symbol_configs=symbol_configs,
        wakeup_callback=market_wakeup.on_market_shock,
    )
    user_data_stream_hub = _build_user_data_stream_hub(
        config=config,
        client=client,
        event_callback=governor._on_user_data_stream_event,
    )
    user_data_shadow_cache = UserDataShadowCache()
    strategy_coordinator = StrategyCoordinator(
        config=config,
        symbol_configs=list(symbol_configs),
    )
    shared_rest_weight_ledger().set_weight_limit(config.rest_weight_limit_1m)
    candle_data_hub = _build_candle_data_hub(config, strategy_coordinator)
    market_data_router = MarketDataRouter(
        client,
        market_data_hub=market_data_hub,
        price_window=config.market_ws_price_window,
        candle_data_hub=candle_data_hub,
        user_data_cache=user_data_shadow_cache,
        logger=logger,
        snapshot_execution_mode=config.execution_mode,
        snapshot_environment=config.environment,
        snapshot_fallback_allowed=config.snapshot_fallback_allowed,
        snapshot_settings_overrides={
            "deadline_seconds": config.snapshot_deadline_seconds,
            "rest_timeout_seconds": config.snapshot_rest_timeout_seconds,
            "rest_connect_timeout_seconds": config.snapshot_rest_connect_timeout_seconds,
            "rest_read_timeout_seconds": config.snapshot_rest_read_timeout_seconds,
            "rest_transport_timeout_seconds": config.snapshot_rest_transport_timeout_seconds,
            "rest_max_attempts": config.snapshot_rest_max_attempts,
            "rest_retry_delay_seconds": config.snapshot_rest_retry_delay_seconds,
            "execution_max_age_seconds": config.snapshot_execution_max_age_seconds,
            "execution_max_skew_seconds": config.snapshot_execution_max_skew_seconds,
            "evaluation_max_age_seconds": config.snapshot_evaluation_max_age_seconds,
        },
    )
    data = {
        "logger": logger,
        "scheduler": scheduler,
        "market_wakeup": market_wakeup,
        "market_data_hub": market_data_hub,
        "user_data_stream_hub": user_data_stream_hub,
        "user_data_shadow_cache": user_data_shadow_cache,
        "_last_user_data_cache_audits": {},
        "candle_data_hub": candle_data_hub,
        "market_data_router": market_data_router,
        "snapshot_provider": market_data_router.snapshot_provider,
        "trend_context_engine": TrendContextEngine.from_config(config),
    }
    return data, strategy_coordinator


def _decision_components(
    config: AppConfig,
    strategy_coordinator: StrategyCoordinator,
    learning: _LearningSettings,
) -> dict[str, Any]:
    """Strategy, planning, intelligent flow, adaptive management and learning policies.

    A layer that is switched off by the config is installed as ``None``.
    """

    intelligent_flow = _enum_value(config.architecture_mode) == "INTELLIGENT_FLOW"
    adaptive = config.adaptive_manager_enabled
    testnet_execution_active = learning.testnet_execution.intelligent_activation_level in {
        TESTNET_EXECUTION_REDUCE_LEVEL,
        TESTNET_EXECUTION_BLOCK_LEVEL,
    }
    return {
        "strategy_coordinator": strategy_coordinator,
        "hot_symbol_worker": HotSymbolWorker(),
        "trade_pipeline": (
            TradePipeline(config=config)
            if config.trade_plan_shadow or config.trade_planning_enabled
            else None
        ),
        "intelligent_order_builder": IntelligentOrderBuilder() if intelligent_flow else None,
        "intelligent_order_comparator": IntelligentOrderComparator() if intelligent_flow else None,
        "intelligent_exit_builder": IntelligentExitBuilder() if intelligent_flow else None,
        "intelligent_exit_comparator": IntelligentExitComparator() if intelligent_flow else None,
        "intelligent_exit_activation_policy": (
            IntelligentExitActivationPolicy() if intelligent_flow else None
        ),
        "intelligent_execution_planner": (
            IntelligentExecutionPlanner() if intelligent_flow else None
        ),
        "intelligent_order_activation_policy": (
            IntelligentOrderActivationPolicy() if intelligent_flow else None
        ),
        "adaptive_manager": AdaptivePositionManager() if adaptive else None,
        "adaptive_adjustment_planner": AdaptiveAdjustmentPlanner() if adaptive else None,
        "adaptive_adjustment_activation_policy": (
            AdaptiveAdjustmentActivationPolicy() if intelligent_flow else None
        ),
        "learning_advisory": LearningAdvisoryEngine() if learning.enabled else None,
        "learning_plan_guard": LearningPlanGuard() if learning.enabled else None,
        "learning_execution_shadow_policy": (
            LearningExecutionShadowPolicy() if learning.enabled else None
        ),
        "learning_paper_execution_policy": (
            LearningPaperExecutionPolicy(settings=learning.paper) if learning.enabled else None
        ),
        "learning_testnet_observation_policy": (
            LearningTestnetObservationPolicy(settings=learning.testnet_observation)
            if learning.enabled and config.learning_testnet_observation_enabled
            else None
        ),
        "learning_testnet_execution_policy": (
            LearningTestnetExecutionPolicy(settings=learning.testnet_execution)
            if learning.enabled and testnet_execution_active
            else None
        ),
        "opportunity_ranker": OpportunityRanker(),
    }


def _state_components(
    config: AppConfig,
    *,
    logger: Any,
    journals: JournalRegistry,
    events_journal: TradeJournal,
    governor_events_path: Path,
    data_dir: Path,
    max_symbols_per_cycle: int,
    learning_settings: _LearningSettings,
) -> dict[str, Any]:
    """Journals, counters and the runtime state the Governor reads and writes."""

    state_path = Path(config.runtime_dir) / "governor_state.json"
    learning_enabled = learning_settings.enabled
    return {
        "max_symbols_per_cycle": max(1, int(max_symbols_per_cycle)),
        "_cycle_sequence": 0,
        "_symbol_eval_sequence": 0,
        "_current_cycle_id": "",
        "_current_symbol_eval_id": "",
        "governor_events_path": governor_events_path,
        "journals": journals,
        "events_journal": events_journal,
        "symbol_state_tracker": SymbolStateTracker(journal=events_journal, logger=logger),
        "score_journal": journals.journal(data_dir / "scanner_scores.jsonl"),
        "control_requests": GovernorControlRequests(
            runtime_dir=config.runtime_dir,
            events_journal=events_journal,
        ),
        "learning_reporter": (
            TradePlanLearningReporter(
                source_journal_path=config.trade_journal_path,
                output_journal=events_journal,
            )
            if learning_enabled
            else None
        ),
        "learning_shadow_impact_reporter": (
            LearningShadowImpactReporter(
                source_journal_path=governor_events_path,
                output_journal=events_journal,
                paper_activation_settings=learning_settings.paper,
            )
            if learning_enabled
            else None
        ),
        "learning_state": LearningReportState(),
        "state_path": state_path,
        "state_writer": GovernorStateWriter(state_path),
        "paused_symbols": set(),
        "stopped_symbols": set(),
        "snapshots": {},
        "_running": False,
        "_new_positions_this_cycle": 0,
        "_current_cycle_context": None,
        "_last_portfolio_state_payload": None,
        "_last_portfolio_state_captured_at": None,
        "cycle_metrics_state": CycleMetricsState(),
        "stage_telemetry": StageTelemetry(),
        "_last_defensive_mode_active": False,
        "_last_fast_lane_symbol_at": {},
    }


def _build_market_data_hub(
    *,
    config: AppConfig,
    symbol_configs: tuple[SymbolTradingConfig, ...],
    wakeup_callback: EventCallback,
) -> MarketDataHub | None:
    if not config.market_ws_enabled:
        return None
    return MarketDataHub(
        symbols=[item.symbol for item in symbol_configs],
        base_url=resolve_public_market_ws_base_url(config),
        stream=config.market_ws_stream,
        max_price_age_seconds=config.market_ws_max_price_age_seconds,
        heartbeat_seconds=config.market_ws_heartbeat_seconds,
        reconnect_seconds=config.market_ws_reconnect_seconds,
        dirty_symbols_limit=config.market_ws_dirty_symbols_limit,
        price_window=config.market_ws_price_window,
        max_symbols_per_drain=config.market_ws_max_symbols_per_drain,
        market_shock_detector_enabled=config.market_shock_detector_enabled,
        market_shock_window_seconds=config.market_shock_window_seconds,
        market_shock_move_bps_threshold=config.market_shock_move_bps_threshold,
        market_shock_critical_bps_threshold=config.market_shock_critical_bps_threshold,
        market_shock_cooldown_seconds=config.market_shock_cooldown_seconds,
        market_wakeup_callback=wakeup_callback,
    )


def _build_user_data_stream_hub(
    *,
    config: AppConfig,
    client: BinanceFuturesClient,
    event_callback: EventCallback,
) -> UserDataStreamHub | None:
    if not config.user_data_stream_enabled:
        return None
    return UserDataStreamHub(
        client=client,
        base_url=resolve_user_data_ws_base_url(config),
        keepalive_seconds=config.user_data_stream_keepalive_seconds,
        reconnect_seconds=config.user_data_stream_reconnect_seconds,
        heartbeat_seconds=config.user_data_stream_heartbeat_seconds,
        max_pending_events=config.user_data_stream_max_pending_events,
        shadow_only=config.user_data_stream_shadow_only,
        event_callback=event_callback,
    )


def trend_timeframe(config: AppConfig, primary_timeframe: str) -> str:
    """The timeframe whose closed candles feed the trend: configured, else the primary one."""

    configured = str(getattr(config, "trend_timeframe", "") or "").strip()
    if not configured:
        return primary_timeframe
    if not is_supported_timeframe(configured):
        raise ValueError(f"ALPHORA_TREND_TIMEFRAME invalido: {configured}")
    return normalize_timeframe(configured)


def with_trend_requirements(
    config: AppConfig,
    requirements_by_symbol: dict[str, StrategyDataRequirements],
) -> dict[str, StrategyDataRequirements]:
    """Ask the candle hub for enough closed candles to compute the trend of each symbol."""

    if not getattr(config, "trend_context_enabled", True):
        return requirements_by_symbol
    window = TrendContextEngine.from_config(config).required_price_window()
    return {
        symbol: requirement.with_minimum_candles(
            trend_timeframe(config, requirement.primary_timeframe), window
        )
        for symbol, requirement in requirements_by_symbol.items()
    }


def _build_candle_data_hub(
    config: AppConfig,
    strategy_coordinator: StrategyCoordinator,
) -> CandleDataHub | None:
    configured = str(getattr(config, "candle_hub_mode", "AUTO") or "AUTO").upper().strip()
    if configured != "AUTO":
        mode = configured
    else:
        mode = {
            "PAPER_LIVE": CANDLE_HUB_MODE_ACTIVE_PAPER,
            "TESTNET_LIVE": CANDLE_HUB_MODE_ACTIVE_TESTNET,
            "REAL_LIVE": CANDLE_HUB_MODE_ACTIVE_REAL,
        }.get(config.execution_mode.value, CANDLE_HUB_MODE_OFF)

    if mode == CANDLE_HUB_MODE_OFF:
        observe_event(
            "candle_hub_disabled",
            component=__name__,
            category="candle_hub",
            channel=EventChannel.AUDIT,
            configured_mode=str(getattr(config, "candle_hub_mode", "AUTO") or "AUTO"),
            resolved_mode=mode,
            execution_mode=config.execution_mode.value,
        )
        return None

    requirements_by_symbol = with_trend_requirements(
        config, strategy_coordinator.requirements_by_symbol()
    )
    if not requirements_by_symbol:
        observe_event(
            "candle_hub_disabled",
            component=__name__,
            category="candle_hub",
            channel=EventChannel.AUDIT,
            severity="WARNING",
            configured_mode=str(getattr(config, "candle_hub_mode", "AUTO") or "AUTO"),
            resolved_mode=mode,
            reason="NO_STRATEGY_DATA_REQUIREMENTS",
        )
        return None

    hub = CandleDataHub(
        CandleDataProvider(base_url=config.candle_klines_url),
        requirements_by_symbol=requirements_by_symbol,
        mode=mode,
    )
    hub.bootstrap()
    observe_event(
        "candle_hub_started",
        component=__name__,
        category="candle_hub",
        channel=EventChannel.AUDIT,
        mode=mode,
        klines_url=config.candle_klines_url,
        environment=config.environment.value,
        symbols=sorted(requirements_by_symbol),
        requirements={
            symbol: {
                "primary_timeframe": requirement.primary_timeframe,
                "required_candles": dict(requirement.required_candles),
                "evaluation_policy": requirement.evaluation_policy,
                "closed_candles_only": requirement.closed_candles_only,
            }
            for symbol, requirement in requirements_by_symbol.items()
        },
    )
    return hub
