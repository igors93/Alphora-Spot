import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from config import ExecutionMode
from config.symbol_context import PortfolioRiskSettings, symbol_config_from_app_config
from domain.models import (
    FuturesPosition,
    MarketSnapshot,
    PositionDirection,
    TradeAction,
    TradeDecision,
)
from domain.strategy_requirements import StrategyDataRequirements
from domain.symbol_rules import SymbolRules
from futures.costs.model import COST_MODEL_VERSION
from governor.alphora_governor import AlphoraGovernor
from governor.daily_portfolio_metrics import DailyPortfolioMetricsReader
from governor.execution_engine import ExecutionResult, ExecutionStatus
from governor.hot_symbol_worker import GuardDecision, GuardOutcome
from governor.market_data_router import HeavyMarketSnapshot, LightMarketSnapshot
from governor.models import GovernorDecision, SymbolRuntimeStatus
from governor.portfolio_risk_engine import PortfolioRiskEngine
from governor.services import LearningService
from governor.services.intelligent_flow import IntelligentFlowService
from governor.services.trade_planning import TradePlanningService
from market_data.snapshot_provider import (
    ExchangeInfoBootstrapResult,
)
from operational_module_modes import (
    MODULE_SMART_EXECUTION,
    MODULE_SMART_SIZING,
    MODULE_STATE_RECONCILIATION,
    ModuleModeSource,
    OperationalModuleMode,
    ReconciliationMode,
    SmartSizingMode,
)
from scanner.signal_score import SignalPriority, SignalScore
from trading.intelligent_exit_activation import IntelligentExitActivationDecision
from trading.intent import TradeIntent


class FakeClient:
    def ticker_price(self, symbol, *, timeout=None):
        if symbol == "ETHUSDT":
            raise RuntimeError("falha isolada")
        return {"price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        return {"markPrice": "100"}

    def open_orders(self, symbol, *, timeout=None):
        return []

    def open_algo_orders(self, symbol, *, timeout=None):
        return []

    def position_risk(self, symbol, *, timeout=None):
        return [{"symbol": symbol, "positionAmt": "0"}]

    def account_info(self, *, timeout=None):
        return {
            "assets": [{"asset": "USDT", "walletBalance": "123.45", "availableBalance": "123.45"}]
        }

    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                        },
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.10",
                        },
                        {
                            "filterType": "MIN_NOTIONAL",
                            "notional": "5",
                        },
                    ],
                }
            ]
        }


class FakeExecutionEngine:
    def __init__(self, positions=None):
        self.executed = []
        self.execution_kwargs = []
        self.audited = []
        self.positions = positions or {}

    def get_position(self, symbol_config):
        return self.positions.get(symbol_config.symbol) or FuturesPosition(
            symbol=symbol_config.symbol,
            direction=PositionDirection.NONE,
            quantity=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        )

    def execute(
        self,
        *,
        symbol_config,
        heavy_snapshot,
        command,
    ):
        action = TradeAction(command.action)
        self.executed.append((symbol_config.symbol, action.value))
        self.execution_kwargs.append(
            {
                "planned_quantity": command.quantity,
                "trade_plan_id": command.trade_plan_id,
                "planned_stop_price": command.stop_price,
                "planned_take_profit_price": command.take_profit_price,
                "execution_command_id": command.command_id,
                "intent_id": command.intent_id,
            }
        )
        return ExecutionResult(
            command_id=command.command_id,
            status=ExecutionStatus.CONFIRMED,
            symbol=symbol_config.symbol,
            action=action,
            order_ids=(),
            position_changed=action
            in {TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT, TradeAction.CLOSE_POSITION},
            reason=command.reason,
            diagnostics={"heavy_symbol": heavy_snapshot.symbol},
        )

    def audit_symbol_protections(self, symbol_config, *, position=None):
        self.audited.append(symbol_config.symbol)
        position = position or self.get_position(symbol_config)
        return position.is_open

    def sync_position_state(self, symbol_config, position):
        return position


class LifecycleExecutionEngine(FakeExecutionEngine):
    def __init__(self):
        super().__init__()
        self.acquired = []
        self.released = False

    def acquire_symbol_locks(self, symbol_configs):
        self.acquired = [item.symbol for item in symbol_configs]

    def release_symbol_locks(self):
        self.released = True


class LifecycleHub:
    def __init__(self, *, fail_stop=False):
        self.fail_stop = fail_stop
        self.started = False
        self.stopped = False
        self.drained = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        if self.fail_stop:
            raise RuntimeError("falha ao parar hub")

    def has_recent_price(self, symbol, *, now=None):
        return False

    def drain_dirty_symbols(self, *, now=None):
        self.drained += 1
        return []


class FakeStrategyCoordinator:
    global_strategy_name = "tests.FakeStrategy"
    runtime_mode = "GLOBAL_SYMBOL_API"
    state_mode = "PER_SYMBOL_STATE"

    def __init__(self, scores=None, decision=None):
        self.scores = scores or {}
        self.decision = decision or TradeDecision.open_long("confirmed")

    def score(self, symbol_config, market, position):
        value = self.scores.get(symbol_config.symbol)
        if value is not None:
            return value
        return SignalScore(
            symbol=symbol_config.symbol, score=95, action="OPEN_LONG", reason="ready"
        )

    def decide(self, symbol_config, market, position):
        return self.decision

    def requirements_for_symbol(self, symbol, *, symbol_config=None):
        return StrategyDataRequirements(primary_timeframe="1m", required_candles={"1m": 1})

    def intent(self, symbol_config, market, position, *, score=None, decision=None):
        decision = decision or self.decision
        return TradeIntent(
            symbol=symbol_config.symbol,
            action=decision.action.value,
            confidence=score.score if score is not None else 80,
            reason=decision.reason,
            strategy_name=self.global_strategy_name,
            metadata={"test": True},
        )


class TrendAwareStrategyCoordinator(FakeStrategyCoordinator):
    def score(self, symbol_config, market, position):
        assert "trend_context" in market.indicators
        return super().score(symbol_config, market, position)

    def decide(self, symbol_config, market, position):
        assert "trend_context" in market.indicators
        return super().decide(symbol_config, market, position)


class IntentStrategyCoordinator(FakeStrategyCoordinator):
    def intent(self, symbol_config, market, position, *, score=None, decision=None):
        decision = decision or self.decision
        return TradeIntent(
            symbol=symbol_config.symbol,
            action=decision.action.value,
            confidence=score.score if score is not None else 80,
            reason=decision.reason,
            strategy_name=self.global_strategy_name,
            metadata={"test": True},
        )


class FakeRouter:
    def __init__(self, position=None):
        self.position = position
        self.heavy_calls = []

    def get_light_snapshot(self, symbol, *, now=None, operational_snapshot=None):
        return LightMarketSnapshot(
            symbol=symbol,
            market=MarketSnapshot(symbol=symbol, price=Decimal("100"), prices=[Decimal("100")]),
            timestamp=now or 0,
            data_age_seconds=0,
            stale=False,
        )

    def get_heavy_snapshot(
        self,
        symbol,
        *,
        now=None,
        cached_position=None,
        cached_account_info=None,
        cached_open_orders=None,
        operational_snapshot=None,
    ):
        self.heavy_calls.append(symbol)
        position = (
            cached_position
            or self.position
            or FuturesPosition(
                symbol=symbol,
                direction=PositionDirection.NONE,
                quantity=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )
        )
        return HeavyMarketSnapshot(
            symbol=symbol,
            market=MarketSnapshot(symbol=symbol, price=Decimal("100"), prices=[Decimal("100")]),
            mark_price=Decimal("100"),
            exchange_rules=SymbolRules(
                symbol=symbol,
                quantity_step=Decimal("0.001"),
                min_quantity=Decimal("0.001"),
                min_notional=Decimal("5"),
                price_tick=Decimal("0.10"),
            ),
            position=position,
            open_orders=[],
            open_algo_orders=[],
            account_info={},
            timestamp=now or 0,
            data_age_seconds=0,
            stale=False,
        )


class FakeHotWorker:
    def evaluate(self, **kwargs):
        return GuardDecision(
            outcome=GuardOutcome.ALLOW,
            reason="HOT_CHECK_PASSED",
            checks={"intent_id": kwargs["intent"].intent_id},
        )


class DecideHotWorker:
    def __init__(self, decision):
        self.decision = decision

    def evaluate(self, **kwargs):
        intent = kwargs.get("intent")
        if intent is not None and intent.action == "HOLD":
            return GuardDecision(
                GuardOutcome.DEFER,
                "STRATEGY_HOLD",
                checks={"strategy_decision": "HOLD"},
            )
        return GuardDecision(
            outcome=GuardOutcome.ALLOW,
            reason="HOT_CHECK_PASSED",
            checks={"strategy_decision": self.decision.action.value},
        )


class RecordingHotWorker:
    def __init__(self):
        self.opened_at = None

    def evaluate(self, **kwargs):
        self.opened_at = kwargs["heavy_snapshot"].position.opened_at
        return GuardDecision(GuardOutcome.BLOCK, "captured", checks={})


class FakePortfolio:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def evaluate_trade_intent(
        self, intent, symbol_contexts, account_snapshot, *, override_notional=None
    ):
        return GovernorDecision(
            allowed=self.allowed,
            symbol=intent.symbol,
            action=intent.action,
            reason="APPROVED" if self.allowed else "MAX_OPEN_POSITIONS",
        )

    def snapshot(self, account_snapshot=None):
        from governor.models import PortfolioSnapshot

        return PortfolioSnapshot()


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


def _ai_metadata(*, action_candidate="OPEN_SHORT", direction_candidate="SHORT"):
    return {
        "source": "AI",
        "model_source": "SYMBOL_REFINED",
        "model_path": "models/ai/refined/BTCUSDT/btc_refined.json",
        "long_score": -0.2,
        "short_score": 0.8,
        "action_candidate": action_candidate,
        "direction_candidate": direction_candidate,
    }


def _metrics_reader(config, profiles):
    return DailyPortfolioMetricsReader(
        config=config,
        symbol_configs=profiles,
        events_journal=FakeJournal(),
    )


def _write_jsonl(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event) + "\n")


def test_governor_initializes_market_data_hub_when_enabled(config_factory):
    config = config_factory(symbols=("BTCUSDT", "ETHUSDT"))
    profiles = [
        symbol_config_from_app_config(config, symbol="BTCUSDT"),
        symbol_config_from_app_config(config, symbol="ETHUSDT"),
    ]

    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=profiles)

    assert governor.market_data_hub is not None
    assert governor.market_data_hub.symbols == ("BTCUSDT", "ETHUSDT")
    assert governor.market_data_router.market_data_hub is governor.market_data_hub


def test_governor_does_not_initialize_market_data_hub_when_disabled(config_factory):
    config = replace(config_factory(), market_ws_enabled=False)
    profile = symbol_config_from_app_config(config)

    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    assert governor.market_data_hub is None
    assert governor.market_data_router.market_data_hub is None


def test_governor_bootstraps_exchange_info_before_market_warmup(
    config_factory,
    monkeypatch,
):
    config = replace(
        config_factory(),
        recover_on_boot=False,
        market_ws_warmup_seconds=0.0,
    )
    profile = symbol_config_from_app_config(config)

    governor = AlphoraGovernor(
        config=config,
        client=FakeClient(),
        symbol_configs=[profile],
    )

    execution = LifecycleExecutionEngine()
    hub = LifecycleHub()
    governor.execution_engine = execution
    governor.market_data_hub = hub

    provider = governor.snapshot_provider
    assert provider is not None

    call_order = []

    def bootstrap():
        call_order.append("bootstrap")
        return ExchangeInfoBootstrapResult(
            attempts=1,
            duration_ms=1.0,
            symbol_count=1,
            from_cache=False,
            stale=False,
        )

    def warmup():
        call_order.append("warmup")

    monkeypatch.setattr(
        provider,
        "bootstrap_exchange_info",
        bootstrap,
    )
    monkeypatch.setattr(
        governor,
        "_warmup_market_data_hub",
        warmup,
    )

    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    (runtime_dir / "governor_stop.request").write_text(
        "stop",
        encoding="utf-8",
    )

    governor.start()

    assert call_order[:2] == [
        "bootstrap",
        "warmup",
    ]


def test_exchange_info_bootstrap_failure_keeps_fallback_active(
    config_factory,
    monkeypatch,
):
    config = replace(
        config_factory(),
        recover_on_boot=False,
        market_ws_warmup_seconds=0.0,
    )
    profile = symbol_config_from_app_config(config)

    governor = AlphoraGovernor(
        config=config,
        client=FakeClient(),
        symbol_configs=[profile],
    )

    governor.events_journal = FakeJournal()

    provider = governor.snapshot_provider
    assert provider is not None

    def fail_bootstrap():
        raise RuntimeError("synthetic exchange info bootstrap failure")

    monkeypatch.setattr(
        provider,
        "bootstrap_exchange_info",
        fail_bootstrap,
    )

    governor._bootstrap_exchange_info()

    matching_events = [
        payload
        for event_type, payload in governor.events_journal.events
        if event_type == "coalestra_exchange_info_bootstrap_failed"
    ]

    assert len(matching_events) == 1
    assert matching_events[0]["status"] == "DEGRADED"
    assert matching_events[0]["diagnostics"]["error_type"] == "RuntimeError"


def test_governor_stops_market_data_hub_in_finally(config_factory):
    config = replace(config_factory(), recover_on_boot=False, market_ws_warmup_seconds=0.0)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = LifecycleExecutionEngine()
    hub = LifecycleHub()
    governor.execution_engine = execution
    governor.market_data_hub = hub

    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "governor_stop.request").write_text("stop", encoding="utf-8")

    governor.start()

    assert hub.started is True
    assert hub.stopped is True
    assert execution.acquired == [profile.symbol]
    assert execution.released is True


def test_governor_releases_locks_when_market_data_hub_stop_fails(config_factory):
    config = replace(config_factory(), recover_on_boot=False, market_ws_warmup_seconds=0.0)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = LifecycleExecutionEngine()
    hub = LifecycleHub(fail_stop=True)
    governor.execution_engine = execution
    governor.market_data_hub = hub

    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "governor_stop.request").write_text("stop", encoding="utf-8")

    governor.start()

    assert hub.stopped is True
    assert execution.released is True


def test_governor_writes_final_state_before_closing_snapshot(
    config_factory,
    monkeypatch,
):
    config = replace(
        config_factory(),
        recover_on_boot=False,
        market_ws_warmup_seconds=0.0,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(
        config=config,
        client=FakeClient(),
        symbol_configs=[profile],
    )
    execution = LifecycleExecutionEngine()
    hub = LifecycleHub()
    governor.execution_engine = execution
    governor.market_data_hub = hub

    provider = governor.snapshot_provider
    assert provider is not None

    shutdown_order = []
    provider_closed = False
    original_provider_close = provider.close

    def write_state(
        *,
        context=None,
        cached_portfolio_only=False,
    ):
        assert context is None
        assert cached_portfolio_only is True
        assert provider_closed is False
        shutdown_order.append("state_written")

    def close_provider():
        nonlocal provider_closed
        provider_closed = True
        shutdown_order.append("coalestra_closed")
        original_provider_close()

    monkeypatch.setattr(governor, "_write_state", write_state)
    monkeypatch.setattr(provider, "close", close_provider)

    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "governor_stop.request").write_text(
        "stop",
        encoding="utf-8",
    )

    governor.start()

    assert shutdown_order == [
        "state_written",
        "coalestra_closed",
    ]
    assert governor._running is False
    assert execution.released is True


def test_governor_cleanup_continues_when_final_state_write_fails(
    config_factory,
    monkeypatch,
):
    config = replace(
        config_factory(),
        recover_on_boot=False,
        market_ws_warmup_seconds=0.0,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(
        config=config,
        client=FakeClient(),
        symbol_configs=[profile],
    )
    execution = LifecycleExecutionEngine()
    hub = LifecycleHub()
    governor.execution_engine = execution
    governor.market_data_hub = hub

    provider = governor.snapshot_provider
    assert provider is not None

    provider_closed = False
    original_provider_close = provider.close

    def fail_state_write(
        *,
        context=None,
        cached_portfolio_only=False,
    ):
        assert context is None
        assert cached_portfolio_only is True
        raise RuntimeError("synthetic final state write failure")

    def close_provider():
        nonlocal provider_closed
        provider_closed = True
        original_provider_close()

    monkeypatch.setattr(governor, "_write_state", fail_state_write)
    monkeypatch.setattr(provider, "close", close_provider)

    runtime_dir = Path(config.runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "governor_stop.request").write_text(
        "stop",
        encoding="utf-8",
    )

    governor.start()

    assert provider_closed is True
    assert hub.stopped is True
    assert execution.released is True
    assert governor._running is False


def test_final_state_uses_cached_portfolio_without_network(
    config_factory,
    monkeypatch,
):
    config = replace(
        config_factory(),
        recover_on_boot=False,
        market_ws_warmup_seconds=0.0,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(
        config=config,
        client=FakeClient(),
        symbol_configs=[profile],
    )

    cached_portfolio = {
        "total_open_positions": 1,
        "max_open_positions": 3,
        "total_exposure_usdt": "100",
        "max_total_exposure_usdt": "1000",
        "exposure_by_symbol": {"BTCUSDT": "100"},
        "exposure_by_direction": {"LONG": "100"},
        "exposure_by_group": {"BTC": "100"},
        "daily_realized_pnl": "2.5",
        "daily_trades": 1,
        "consecutive_losses": 0,
        "blocked_symbols": [],
        "timestamp": 123.0,
    }

    governor._last_portfolio_state_payload = cached_portfolio
    governor._last_portfolio_state_captured_at = 123.0

    written_payload = {}

    def fail_if_network_is_used(*, context=None):
        raise AssertionError("Final state must not build a new portfolio snapshot")

    def capture_state(payload):
        written_payload.update(payload)

    monkeypatch.setattr(
        governor,
        "_portfolio_account_snapshot",
        fail_if_network_is_used,
    )
    monkeypatch.setattr(
        governor.state_writer,
        "write",
        capture_state,
    )

    governor._write_final_state_safely()

    assert written_payload["portfolio"] == cached_portfolio
    assert written_payload["portfolio_metadata"]["available"] is True
    assert written_payload["portfolio_metadata"]["source"] == "LAST_CONFIRMED_SNAPSHOT"


def test_final_state_without_cached_portfolio_does_not_use_network(
    config_factory,
    monkeypatch,
):
    config = replace(
        config_factory(),
        recover_on_boot=False,
        market_ws_warmup_seconds=0.0,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(
        config=config,
        client=FakeClient(),
        symbol_configs=[profile],
    )

    written_payload = {}

    def fail_if_network_is_used(*, context=None):
        raise AssertionError("Final state must not build a new portfolio snapshot")

    def capture_state(payload):
        written_payload.update(payload)

    monkeypatch.setattr(
        governor,
        "_portfolio_account_snapshot",
        fail_if_network_is_used,
    )
    monkeypatch.setattr(
        governor.state_writer,
        "write",
        capture_state,
    )

    governor._write_final_state_safely()

    assert written_payload["portfolio_metadata"] == {
        "available": False,
        "source": "UNAVAILABLE",
        "captured_at": None,
        "age_seconds": None,
    }
    assert "FINAL_STATE_PORTFOLIO_UNAVAILABLE" in written_payload["portfolio"]["blocked_symbols"]


def test_regular_state_write_refreshes_portfolio_cache(
    config_factory,
    monkeypatch,
):
    config = replace(
        config_factory(),
        recover_on_boot=False,
        market_ws_warmup_seconds=0.0,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(
        config=config,
        client=FakeClient(),
        symbol_configs=[profile],
    )

    account_snapshot = {
        "open_positions": [],
        "available_balance_usdt": "123.45",
        "daily_realized_pnl": "4.25",
        "daily_trades": 2,
        "consecutive_losses": 0,
        "blocked_symbols": [],
    }
    written_payload = {}

    def account_snapshot_provider(*, context=None):
        return account_snapshot

    def capture_state(payload):
        written_payload.update(payload)

    monkeypatch.setattr(
        governor,
        "_portfolio_account_snapshot",
        account_snapshot_provider,
    )
    monkeypatch.setattr(
        governor.state_writer,
        "write",
        capture_state,
    )

    governor._write_state()

    assert governor._last_portfolio_state_payload is not None
    assert governor._last_portfolio_state_payload == written_payload["portfolio"]
    assert written_payload["portfolio_metadata"]["available"] is True
    assert written_payload["portfolio_metadata"]["source"] == "CURRENT_SNAPSHOT"
    assert written_payload["portfolio"]["daily_realized_pnl"] == "4.25"


def test_dirty_symbols_enter_governor_candidates_without_duplicates(config_factory):
    config = config_factory(symbols=("BTCUSDT", "ETHUSDT"))
    profiles = [
        symbol_config_from_app_config(config, symbol="BTCUSDT"),
        symbol_config_from_app_config(config, symbol="ETHUSDT"),
    ]
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=profiles)
    now = time.time()
    governor.scheduler.update(
        SignalScore("BTCUSDT", 80, reason="hot"),
        now=now,
    )

    ranked = governor._opportunity_service().rank_due_symbols(
        now + 1.1,
        open_symbols=set(),
        dirty_symbols=["BTCUSDT", "BTCUSDT"],
    )

    assert ranked.count("BTCUSDT") == 1


def test_cold_dirty_symbol_does_not_wake_before_scheduler_due(config_factory):
    config = config_factory(symbols=("BTCUSDT",))
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.scheduler.update(SignalScore("BTCUSDT", 10, reason="cold"), now=100)

    ranked = governor._opportunity_service().rank_due_symbols(
        120,
        open_symbols=set(),
        dirty_symbols=["BTCUSDT"],
    )

    assert ranked == []


def test_cold_symbol_still_enters_when_scheduler_due(config_factory):
    config = config_factory(symbols=("BTCUSDT",))
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.scheduler.update(SignalScore("BTCUSDT", 10, reason="cold"), now=100)

    ranked = governor._opportunity_service().rank_due_symbols(
        190,
        open_symbols=set(),
        dirty_symbols=["BTCUSDT"],
    )

    assert ranked == ["BTCUSDT"]


def test_warm_dirty_symbol_can_wake_after_minimum_interval(config_factory):
    config = config_factory(symbols=("BTCUSDT",))
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.scheduler.update(
        SignalScore("BTCUSDT", 55, reason="warm", suggested_next_check_seconds=20),
        now=100,
    )

    ranked = governor._opportunity_service().rank_due_symbols(
        105,
        open_symbols=set(),
        dirty_symbols=["BTCUSDT"],
    )

    assert ranked == ["BTCUSDT"]


def test_hot_dirty_symbol_can_wake_after_short_interval(config_factory):
    config = config_factory(symbols=("BTCUSDT",))
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.scheduler.update(
        SignalScore("BTCUSDT", 80, reason="hot", suggested_next_check_seconds=20),
        now=100,
    )

    ranked = governor._opportunity_service().rank_due_symbols(
        101,
        open_symbols=set(),
        dirty_symbols=["BTCUSDT"],
    )

    assert ranked == ["BTCUSDT"]


def test_ready_dirty_symbol_wakes_immediately(config_factory):
    config = config_factory(symbols=("BTCUSDT",))
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.scheduler.update(
        SignalScore("BTCUSDT", 95, reason="ready", suggested_next_check_seconds=20),
        now=100,
    )

    ranked = governor._opportunity_service().rank_due_symbols(
        100.1,
        open_symbols=set(),
        dirty_symbols=["BTCUSDT"],
    )

    assert ranked == ["BTCUSDT"]


def test_open_position_enters_even_when_cold_dirty_symbol_cannot_wake(config_factory):
    config = config_factory(symbols=("BTCUSDT",))
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.scheduler.update(SignalScore("BTCUSDT", 10, reason="cold"), now=100)

    ranked = governor._opportunity_service().rank_due_symbols(
        120,
        open_symbols={"BTCUSDT"},
        dirty_symbols=["BTCUSDT"],
    )

    assert ranked == ["BTCUSDT"]


def test_governor_isolates_symbol_errors(config_factory):
    config = config_factory(symbols=("BTCUSDT", "ETHUSDT"))
    profiles = [
        symbol_config_from_app_config(config, symbol="BTCUSDT"),
        symbol_config_from_app_config(config, symbol="ETHUSDT"),
    ]
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=profiles)

    governor.run_cycle(now=time.time() + 1)

    assert governor.snapshots["ETHUSDT"].status is SymbolRuntimeStatus.ERROR
    assert governor.snapshots["BTCUSDT"].status is not SymbolRuntimeStatus.ERROR


def test_governor_executes_only_when_portfolio_approves(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]


def test_governor_records_trend_context_observe_without_changing_execution(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.events_journal = FakeJournal()
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = TrendAwareStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    trend_events = [
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trend_context_evaluated"
    ]
    assert trend_events
    assert trend_events[-1]["regime"] == "LOW_DATA"
    assert trend_events[-1]["execution_changed"] is False
    assert trend_events[-1]["order_mutation"] is False
    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]


def _flow_decisions(governor):
    return [
        data
        for event_type, data in governor.events_journal.events
        if event_type == "governor_evaluate_symbol_flow_decision"
    ]


def test_evaluate_symbol_flow_reaches_execution_in_allowed_modes(config_factory):
    from config import ExecutionMode

    for execution_mode in (ExecutionMode.PAPER_LIVE, ExecutionMode.TESTNET_LIVE):
        config = config_factory(execution_mode=execution_mode)
        profile = symbol_config_from_app_config(config)
        governor = AlphoraGovernor(
            config=config,
            client=FakeClient(),
            symbol_configs=[profile],
        )
        execution = FakeExecutionEngine()
        ready = SignalScore(
            symbol=profile.symbol,
            score=95,
            action="OPEN_LONG",
            reason="ready",
        )
        governor.events_journal = FakeJournal()
        governor.execution_engine = execution
        governor.market_data_router = FakeRouter()
        governor.strategy_coordinator = FakeStrategyCoordinator({profile.symbol: ready})
        governor.hot_symbol_worker = FakeHotWorker()
        governor.portfolio_risk_engine = FakePortfolio(allowed=True)

        governor.run_cycle(now=time.time() + 1)

        assert execution.executed == [(profile.symbol, "OPEN_LONG")]
        approved = _flow_decisions(governor)[-1]
        assert approved["stage"] == "EXECUTION_APPROVED"
        assert approved["classification"] == "operacional"
        assert approved["reason"] == "APPROVED"
        assert approved["execution_will_run"] is True
        assert approved["execution_changed"] is False
        assert approved["order_mutation"] is False


def test_evaluate_symbol_flow_records_light_only_observation(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.execution_engine = FakeExecutionEngine()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: SignalScore(symbol=profile.symbol, score=10, reason="cold")}
    )

    governor.run_cycle(now=time.time() + 1)

    decision = _flow_decisions(governor)[-1]
    assert decision["stage"] == "LIGHT_ONLY"
    assert decision["classification"] == "observacao"
    assert decision["reason"] == "cold"
    assert decision["execution_will_run"] is False


def test_evaluate_symbol_flow_records_request_budget_degradation(config_factory):
    config = replace(
        config_factory(),
        governor_request_budget_max_heavy_checks_per_cycle=0,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    governor.events_journal = FakeJournal()
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="OPEN_LONG",
                reason="ready",
            )
        }
    )

    governor.run_cycle(now=time.time() + 1)

    decision = _flow_decisions(governor)[-1]
    assert execution.executed == []
    assert decision["stage"] == "REQUEST_BUDGET"
    assert decision["classification"] == "degradacao"
    assert decision["reason"] == "HEAVY_CHECK_BUDGET_DISABLED_OR_ZERO"
    assert decision["execution_will_run"] is False


def test_evaluate_symbol_flow_records_manual_reconcile_absolute_block(config_factory):
    from futures.operational import write_manual_reconcile_required

    config = config_factory()
    profile = symbol_config_from_app_config(config)
    write_manual_reconcile_required(
        runtime_dir=profile.runtime_dir,
        symbol=profile.symbol,
        reason="entry_order_status_unknown",
        context={},
        last_known_position=None,
    )
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    governor.events_journal = FakeJournal()
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="OPEN_LONG",
                reason="ready",
            )
        }
    )

    governor.run_cycle(now=time.time() + 1)

    decision = _flow_decisions(governor)[-1]
    assert execution.executed == []
    assert decision["stage"] == "MANUAL_RECONCILE"
    assert decision["classification"] == "absoluto"
    assert decision["reason"] == "MANUAL_RECONCILE_REQUIRED"
    assert decision["execution_will_run"] is False


def test_trade_plan_shadow_records_plan_without_changing_execution(config_factory):
    config = replace(
        config_factory(),
        trade_plan_shadow=True,
        trade_planning_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    event_types = [event_type for event_type, _data in governor.events_journal.events]
    assert "trade_intent_created" in event_types
    assert "trade_plan_shadow_created" in event_types
    assert "trade_plan_cost_diagnostics" in event_types
    assert "trade_plan_shadow_compared" in event_types
    assert "trade_plan_execution_ready" in event_types
    assert "trade_plan_exit_execution_ready" in event_types

    shadow_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_shadow_created"
    )
    assert shadow_event["plan_id"].startswith("tp_")
    assert shadow_event["plan_version"] == "1.0.0"
    assert shadow_event["source"] == "SHADOW"
    assert shadow_event["created_at"]
    assert shadow_event["trade_plan"]["plan_id"] == shadow_event["plan_id"]

    cost_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_cost_diagnostics"
    )
    assert cost_event["plan_id"] == shadow_event["plan_id"]
    assert cost_event["plan_version"] == shadow_event["plan_version"]
    assert cost_event["source"] == shadow_event["source"]
    assert cost_event["cost_summary"]["cost_model_version"] == COST_MODEL_VERSION
    assert "round_trip_cost" in cost_event["cost_summary"]
    assert "components" in cost_event["diagnostics"]

    comparison_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_shadow_compared"
    )
    assert comparison_event["plan_id"] == shadow_event["plan_id"]
    assert comparison_event["plan_version"] == shadow_event["plan_version"]
    assert comparison_event["source"] == shadow_event["source"]
    assert comparison_event["reference_flow"] == "PROFILE_REFERENCE"
    assert comparison_event["comparison"]["plan_id"] == shadow_event["plan_id"]
    assert comparison_event["diagnostics"]["shadow_only"] is True
    assert comparison_event["diagnostics"]["execution_unchanged"] is True

    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_execution_ready"
    )
    assert result_event["dynamic_sizing_enabled"] is False
    assert result_event["plan_available"] is True
    assert result_event["planned_quantity_passed"] is None
    assert result_event["planned_quantity_applied"] is False
    assert result_event["quantity_unchanged"] is True
    assert result_event["reason"] == "DYNAMIC_SIZING_DISABLED"

    exit_ready_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_exit_execution_ready"
    )
    assert exit_ready_event["dynamic_exit_enabled"] is False
    assert exit_ready_event["plan_available"] is True
    assert exit_ready_event["planned_stop_price_passed"] is None
    assert exit_ready_event["planned_take_profit_price_passed"] is None
    assert exit_ready_event["planned_exit_applied"] is False
    assert exit_ready_event["exit_unchanged"] is True
    assert exit_ready_event["reason"] == "DYNAMIC_EXIT_DISABLED"

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None
    assert execution.execution_kwargs[0]["trade_plan_id"].startswith("tp_")
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_governor_blocks_execution_when_economic_gate_rejects(config_factory):
    config = replace(
        config_factory(),
        take_profit_pct=0.0001,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")

    class CountingPortfolio(FakePortfolio):
        def __init__(self):
            super().__init__(allowed=True)
            self.calls = 0

        def evaluate_trade_intent(
            self, intent, symbol_contexts, account_snapshot, *, override_notional=None
        ):
            self.calls += 1
            return super().evaluate_trade_intent(
                intent, symbol_contexts, account_snapshot, override_notional=override_notional
            )

    portfolio = CountingPortfolio()
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = portfolio

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == []
    assert portfolio.calls == 0
    assert any(
        event_type == "economic_gate_evaluated_initial"
        for event_type, _data in governor.events_journal.events
    )

    blocked_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_blocked_by_economic_gate"
    )
    assert blocked_event["symbol"] == "BTCUSDT"
    assert blocked_event["action"] == "OPEN_LONG"
    assert blocked_event["reason"] == "ECONOMIC_TAKE_REQUIRED_TOO_FAR"
    assert blocked_event["block_reason"] == "ECONOMIC_TAKE_REQUIRED_TOO_FAR"
    assert blocked_event["economic_exit_blocked_unrealistic_take"] is True
    assert blocked_event["diagnostics"]["exit_diagnostics"]["required_take_bps"] is not None

    flow_decision = _flow_decisions(governor)[-1]
    assert flow_decision["stage"] == "ECONOMIC_GATE"
    assert flow_decision["execution_will_run"] is False


def test_governor_executes_with_economically_adjusted_exit(config_factory):
    config = replace(
        config_factory(),
        stop_loss_pct=0.003,
        take_profit_pct=0.004,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_stop_price"] == Decimal("99.70")
    assert execution.execution_kwargs[0]["planned_take_profit_price"] == Decimal("100.90")

    initial_gate = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "economic_gate_evaluated_initial"
    )
    final_gate = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "economic_gate_evaluated_final"
    )
    assert initial_gate["allowed"] is True
    assert initial_gate["economic_exit_adjusted"] is True
    assert final_gate["allowed"] is True
    assert final_gate["final_take_profit_price"] == Decimal("100.90")

    payload_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "execution_payload_sent"
    )
    assert payload_event["economic_gate_allowed"] is True
    assert payload_event["planned_take_profit_price"] == "100.90"


def test_governor_restores_valid_economic_exit_when_intelligent_exit_is_worse(
    config_factory,
    monkeypatch,
):
    config = config_factory(dynamic_exit_enabled=True)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    def final_exit_too_close(**_kwargs):
        return IntelligentExitActivationDecision(
            status="APPLIED",
            action="APPLY_PAPER_INTELLIGENT_EXIT",
            reason="test final exit too close",
            symbol=profile.symbol,
            trade_action="OPEN_LONG",
            trade_plan_id="tp_test",
            final_stop_price=Decimal("99.00"),
            final_take_profit_price=Decimal("100.01"),
            applied=True,
            exit_applied=True,
            can_use_for_execution=True,
        )

    monkeypatch.setattr(
        IntelligentFlowService,
        "evaluate_exit_activation",
        lambda _flow, **kwargs: final_exit_too_close(**kwargs),
    )

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_stop_price"] == Decimal("99.00")
    assert execution.execution_kwargs[0]["planned_take_profit_price"] == Decimal("102.00")

    rejected_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_candidate_rejected_by_economic_gate"
    )
    assert rejected_event["reason"] == "EXPECTED_NET_GAIN_NOT_POSITIVE"
    assert rejected_event["candidate_source"] == "INTELLIGENT_EXIT"

    restored_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "planned_exit_restored_to_economic_trade_plan"
    )
    assert restored_event["candidate_take_profit_price"] == Decimal("100.01")
    assert restored_event["restored_take_profit_price"] == Decimal("102.00")

    final_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "economic_gate_evaluated_final"
    )
    assert final_event["allowed"] is True
    assert final_event["final_take_profit_price"] == Decimal("102.00")


def test_governor_does_not_worsen_valid_economic_exit_even_if_candidate_passes(
    config_factory,
    monkeypatch,
):
    config = config_factory(dynamic_exit_enabled=True)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    def final_exit_profitable_but_worse(**_kwargs):
        return IntelligentExitActivationDecision(
            status="APPLIED",
            action="APPLY_PAPER_INTELLIGENT_EXIT",
            reason="test final exit profitable but worse",
            symbol=profile.symbol,
            trade_action="OPEN_LONG",
            trade_plan_id="tp_test",
            final_stop_price=Decimal("99.00"),
            final_take_profit_price=Decimal("101.90"),
            applied=True,
            exit_applied=True,
            can_use_for_execution=True,
        )

    monkeypatch.setattr(
        IntelligentFlowService,
        "evaluate_exit_activation",
        lambda _flow, **kwargs: final_exit_profitable_but_worse(**kwargs),
    )

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_stop_price"] == Decimal("99.00")
    assert execution.execution_kwargs[0]["planned_take_profit_price"] == Decimal("102.00")

    rejected_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_candidate_rejected_by_economic_gate"
    )
    assert rejected_event["reason"] == "CANDIDATE_REWARD_RISK_BELOW_ECONOMIC_TRADE_PLAN"


def test_governor_blocks_when_intelligent_and_original_exits_are_invalid(
    config_factory,
    monkeypatch,
):
    config = replace(
        config_factory(),
        dynamic_exit_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    def invalid_original_exit(_trade_pipeline_result, *, action):
        assert action == "OPEN_LONG"
        return {
            "stop_price": Decimal("99.00"),
            "take_profit_price": Decimal("100.01"),
        }, "TEST_INVALID_ORIGINAL_EXIT"

    def invalid_intelligent_exit(**_kwargs):
        return IntelligentExitActivationDecision(
            status="APPLIED",
            action="APPLY_PAPER_INTELLIGENT_EXIT",
            reason="test final exit still invalid",
            symbol=profile.symbol,
            trade_action="OPEN_LONG",
            trade_plan_id="tp_test",
            final_stop_price=Decimal("99.00"),
            final_take_profit_price=Decimal("100.02"),
            applied=True,
            exit_applied=True,
            can_use_for_execution=True,
        )

    monkeypatch.setattr(
        TradePlanningService,
        "planned_exit_for_execution",
        lambda _planning, *args, **kwargs: invalid_original_exit(*args, **kwargs),
    )
    monkeypatch.setattr(
        IntelligentFlowService,
        "evaluate_exit_activation",
        lambda _flow, **kwargs: invalid_intelligent_exit(**kwargs),
    )

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == []
    assert not any(
        event_type == "planned_exit_restored_to_economic_trade_plan"
        for event_type, _data in governor.events_journal.events
    )
    blocked_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_blocked_by_final_economic_gate"
    )
    assert blocked_event["reason"] == "EXPECTED_NET_GAIN_NOT_POSITIVE"
    assert blocked_event["final_take_profit_price"] == Decimal("100.02")


def test_governor_executes_with_exit_approved_by_final_economic_gate(
    config_factory,
    monkeypatch,
):
    config = replace(
        config_factory(),
        dynamic_exit_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    def final_exit_still_profitable(**_kwargs):
        return IntelligentExitActivationDecision(
            status="APPLIED",
            action="APPLY_PAPER_INTELLIGENT_EXIT",
            reason="test final exit approved",
            symbol=profile.symbol,
            trade_action="OPEN_LONG",
            trade_plan_id="tp_test",
            final_stop_price=Decimal("99.00"),
            final_take_profit_price=Decimal("103.00"),
            applied=True,
            exit_applied=True,
            can_use_for_execution=True,
        )

    monkeypatch.setattr(
        IntelligentFlowService,
        "evaluate_exit_activation",
        lambda _flow, **kwargs: final_exit_still_profitable(**kwargs),
    )

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_stop_price"] == Decimal("99.00")
    assert execution.execution_kwargs[0]["planned_take_profit_price"] == Decimal("103.00")
    final_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "economic_gate_evaluated_final"
    )
    assert final_event["allowed"] is True
    assert final_event["final_take_profit_price"] == Decimal("103.00")


def test_governor_does_not_block_close_position_by_economic_gate(config_factory):
    now = time.time() + 1
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.5"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=now - 12.5,
    )
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine(positions={profile.symbol: position})
    ready = SignalScore(
        symbol="BTCUSDT",
        score=95,
        action="CLOSE_POSITION",
        reason="close risk",
    )
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter(position=position)
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: ready},
        TradeDecision.close("close risk"),
    )
    governor.hot_symbol_worker = DecideHotWorker(TradeDecision.close("close risk"))
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=now)

    assert execution.executed == [("BTCUSDT", "CLOSE_POSITION")]
    final_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "economic_gate_evaluated_final"
    )
    assert final_event["allowed"] is True
    assert final_event["reason"] == "CLOSE_POSITION_BYPASSES_ECONOMIC_GATE"

    requested_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "close_position_requested"
    )
    assert requested_event["close_source"] == "STRATEGY"
    assert requested_event["reason"] == "close risk"
    assert requested_event["seconds_since_open"] == 12.5

    sent_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "close_position_sent"
    )
    assert sent_event["close_source"] == "STRATEGY"
    assert sent_event["seconds_since_open"] == 12.5


def test_trade_plan_dynamic_sizing_passes_planned_quantity_to_execution(config_factory):
    config = replace(
        config_factory(dynamic_sizing_enabled=True),
        trade_plan_shadow=True,
        trade_planning_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_execution_ready"
    )
    assert result_event["dynamic_sizing_enabled"] is True
    assert result_event["planned_quantity_applied"] is True
    assert result_event["planned_quantity_passed"] is not None
    assert result_event["reason"] == "TRADE_PLAN_QUANTITY_READY"

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is not None
    assert execution.execution_kwargs[0]["trade_plan_id"].startswith("tp_")


def test_governor_smart_sizing_reduce_only_reduces_planned_quantity(config_factory):
    config = replace(
        config_factory(),
        smart_sizing_mode=SmartSizingMode.REDUCE_ONLY,
        operational_module_mode_sources={
            MODULE_SMART_SIZING: ModuleModeSource.DESKTOP.value,
        },
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol="BTCUSDT",
        score=95,
        action="OPEN_LONG",
        reason="ready",
        metadata={
            "reward_risk_liquido": "0.8",
            "cost_to_reward_ratio": "0.1",
            "risk_abs": "1",
            "reward_abs": "2",
        },
    )
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.25")
    applied = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "smart_sizing_applied"
    )
    assert applied["mode"] == SmartSizingMode.REDUCE_ONLY.value
    assert applied["base_notional"] == Decimal("50.000")
    assert applied["final_notional"] == Decimal("25.0000")
    assert applied["affected_result"] is True


class WalletClient(FakeClient):
    def __init__(self, wallet_balance):
        self.wallet_balance = wallet_balance

    def account_info(self, *, timeout=None):
        asset = {"asset": "USDT", "availableBalance": "1000"}
        if self.wallet_balance is not None:
            asset["walletBalance"] = self.wallet_balance
        return {"assets": [asset]}


def _run_smart_sizing_cycle(config_factory, *, wallet_balance, closed_pnls):
    config = replace(
        config_factory(),
        smart_sizing_mode=SmartSizingMode.REDUCE_ONLY,
        operational_module_mode_sources={
            MODULE_SMART_SIZING: ModuleModeSource.DESKTOP.value,
        },
    )
    profile = symbol_config_from_app_config(config)
    today = datetime.now(timezone.utc).date().isoformat()
    _write_jsonl(
        Path(profile.journal_path),
        [
            {
                "timestamp": f"{today}T00:0{index}:00+00:00",
                "event_type": "position_closed",
                "data": {"net_pnl": pnl},
            }
            for index, pnl in enumerate(closed_pnls)
        ],
    )
    governor = AlphoraGovernor(
        config=config,
        client=WalletClient(wallet_balance),
        symbol_configs=[profile],
    )
    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol="BTCUSDT",
        score=95,
        action="OPEN_LONG",
        reason="ready",
        metadata={
            "reward_risk_liquido": "2",
            "cost_to_reward_ratio": "0.1",
            "risk_abs": "1",
            "reward_abs": "2",
        },
    )
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)
    return governor, execution


def _smart_sizing_event(governor):
    return next(
        (event_type, data)
        for event_type, data in governor.events_journal.events
        if event_type.startswith("smart_sizing_")
    )


def test_governor_smart_sizing_reduces_size_at_elevated_daily_drawdown(config_factory):
    # Realized peak +20 then -80: 80 USDT below the day's high on a 1000 wallet = 8%.
    governor, execution = _run_smart_sizing_cycle(
        config_factory,
        wallet_balance="1000",
        closed_pnls=["20", "-80"],
    )

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.375")
    event_type, data = _smart_sizing_event(governor)
    assert event_type == "smart_sizing_applied"
    assert data["final_notional"] == Decimal("37.50000")
    assert "ELEVATED_DRAWDOWN" in data["diagnostics"]["diagnostics"]["applied_reasons"]
    assert Decimal(data["diagnostics"]["diagnostics"]["current_drawdown"]) == Decimal("0.08")


def test_governor_smart_sizing_reduces_size_at_high_daily_drawdown(config_factory):
    # Realized peak +20 then -130: 150 USDT below the day's high on a 1000 wallet = 15%.
    governor, execution = _run_smart_sizing_cycle(
        config_factory,
        wallet_balance="1000",
        closed_pnls=["20", "-130"],
    )

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.125")
    event_type, data = _smart_sizing_event(governor)
    assert event_type == "smart_sizing_applied"
    assert "HIGH_DRAWDOWN" in data["diagnostics"]["diagnostics"]["applied_reasons"]


def test_governor_smart_sizing_blocks_entry_when_drawdown_is_unavailable(config_factory):
    governor, execution = _run_smart_sizing_cycle(
        config_factory,
        wallet_balance=None,
        closed_pnls=[],
    )

    assert execution.executed == []
    event_type, data = _smart_sizing_event(governor)
    assert event_type == "smart_sizing_blocked"
    assert data["reason"] == "SMART_SIZING_DRAWDOWN_UNAVAILABLE"
    assert data["diagnostics"]["diagnostics"]["drawdown"]["reason"] == (
        "WALLET_BALANCE_UNAVAILABLE"
    )


def test_governor_smart_execution_active_blocks_high_spread(config_factory):
    config = replace(
        config_factory(),
        smart_execution_mode=OperationalModuleMode.ACTIVE,
        operational_module_mode_sources={
            MODULE_SMART_EXECUTION: ModuleModeSource.DESKTOP.value,
        },
        economic_gate_enabled=False,
        estimated_spread_bps=30.0,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == []
    blocked = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "smart_execution_blocked"
    )
    assert blocked["mode"] == OperationalModuleMode.ACTIVE.value
    assert blocked["reason"] == "SMART_EXECUTION_SPREAD_TOO_HIGH"
    assert _flow_decisions(governor)[-1]["stage"] == "SMART_EXECUTION"


def test_governor_smart_execution_active_blocks_limit_without_runtime_support(
    config_factory,
):
    config = replace(
        config_factory(),
        smart_execution_mode=OperationalModuleMode.ACTIVE,
        operational_module_mode_sources={
            MODULE_SMART_EXECUTION: ModuleModeSource.DESKTOP.value,
        },
        economic_gate_enabled=False,
        estimated_spread_bps=1.0,
        limit_entry_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == []
    blocked = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "smart_execution_blocked"
    )
    assert blocked["reason"] == "SMART_EXECUTION_LIMIT_NOT_IMPLEMENTED"
    assert blocked["requested_entry_order_type"] == "LIMIT"
    assert _flow_decisions(governor)[-1]["reason"] == ("SMART_EXECUTION_LIMIT_NOT_IMPLEMENTED")


def test_governor_reconciliation_block_prevents_new_entry(config_factory):
    config = replace(
        config_factory(),
        state_reconciliation_mode=ReconciliationMode.BLOCK,
        operational_module_mode_sources={
            MODULE_STATE_RECONCILIATION: ModuleModeSource.DESKTOP.value,
        },
    )
    profile = symbol_config_from_app_config(config)
    exchange_position = FuturesPosition(
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        quantity=Decimal("0.1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter(position=exchange_position)
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == []
    block = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "reconciliation_block_applied"
    )
    assert block["mode"] == ReconciliationMode.BLOCK.value
    assert block["should_block_new_entries"] is True
    assert _flow_decisions(governor)[-1]["reason"] == "RECONCILIATION_BLOCK"


def test_trade_plan_dynamic_exit_passes_planned_exit_to_execution(config_factory):
    config = replace(
        config_factory(dynamic_exit_enabled=True),
        trade_plan_shadow=True,
        trade_planning_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    economic_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_allowed_by_economic_gate"
    )
    assert economic_event["allowed"] is True
    assert economic_event["expected_net_gain"] > 0

    policy_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "order_execution_policy_selected"
    )
    assert policy_event["execution_style"] == "MARKET_SAFE"
    assert policy_event["entry_order_type"] == "MARKET"

    exit_ready_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_exit_execution_ready"
    )
    assert exit_ready_event["dynamic_exit_enabled"] is True
    assert exit_ready_event["planned_exit_applied"] is True
    assert exit_ready_event["planned_stop_price_passed"] is not None
    assert exit_ready_event["planned_take_profit_price_passed"] is not None
    assert exit_ready_event["reason"] == "TRADE_PLAN_EXIT_READY"

    assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_stop_price"] is not None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is not None
    assert execution.execution_kwargs[0]["trade_plan_id"].startswith("tp_")


def test_cold_without_position_uses_light_check_only(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    router = FakeRouter()
    governor.execution_engine = execution
    governor.market_data_router = router
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: SignalScore(symbol=profile.symbol, score=10, reason="cold")}
    )

    governor.run_cycle(now=time.time() + 1)

    assert router.heavy_calls == []
    assert execution.executed == []
    assert governor.snapshots[profile.symbol].status is SymbolRuntimeStatus.COLD
    assert governor.snapshots[profile.symbol].priority is SignalPriority.COLD


def test_warm_without_probable_action_does_not_execute_or_heavy_check(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    router = FakeRouter()
    governor.execution_engine = execution
    governor.market_data_router = router
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: SignalScore(symbol=profile.symbol, score=55, reason="warm")}
    )

    governor.run_cycle(now=time.time() + 1)

    assert router.heavy_calls == []
    assert execution.executed == []
    assert governor.snapshots[profile.symbol].status is SymbolRuntimeStatus.WARM
    assert governor.snapshots[profile.symbol].priority is SignalPriority.WARM


def test_hot_calls_heavy_snapshot_and_decide(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    router = FakeRouter()
    execution = FakeExecutionEngine()
    governor.execution_engine = execution
    governor.market_data_router = router
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: SignalScore(symbol=profile.symbol, score=82, reason="hot")},
        TradeDecision.open_long("confirmed hot"),
    )
    governor.hot_symbol_worker = DecideHotWorker(TradeDecision.open_long("confirmed hot"))

    governor.run_cycle(now=time.time() + 1)

    assert router.heavy_calls == [profile.symbol]
    assert execution.executed == [(profile.symbol, "OPEN_LONG")]


def test_ready_calls_heavy_snapshot_and_decide(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    router = FakeRouter()
    execution = FakeExecutionEngine()
    governor.execution_engine = execution
    governor.market_data_router = router
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="OPEN_LONG",
                reason="ready",
            )
        },
        TradeDecision.open_long("confirmed ready"),
    )
    governor.hot_symbol_worker = DecideHotWorker(TradeDecision.open_long("confirmed ready"))

    governor.run_cycle(now=time.time() + 1)

    assert router.heavy_calls == [profile.symbol]
    assert execution.executed == [(profile.symbol, "OPEN_LONG")]


def test_ai_open_short_reaches_governor_and_is_not_ignored(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    router = FakeRouter()
    execution = FakeExecutionEngine()
    score_journal = FakeJournal()
    governor.events_journal = FakeJournal()
    governor.score_journal = score_journal
    governor.execution_engine = execution
    governor.market_data_router = router
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="OPEN_SHORT",
                reason="AI_OPEN_SHORT",
                metadata=_ai_metadata(),
            )
        },
        TradeDecision.open_short("AI_OPEN_SHORT"),
    )
    governor.hot_symbol_worker = DecideHotWorker(TradeDecision.open_short("AI_OPEN_SHORT"))
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    assert router.heavy_calls == [profile.symbol]
    assert execution.executed == [(profile.symbol, "OPEN_SHORT")]
    scanner_event = score_journal.events[0][1]
    assert scanner_event["model_source"] == "SYMBOL_REFINED"
    assert scanner_event["long_score"] == -0.2
    assert scanner_event["short_score"] == 0.8
    approved = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_approved"
    )
    assert approved["action"] == "OPEN_SHORT"
    assert approved["governor_final_action"] == "OPEN_SHORT"
    assert approved["action_candidate"] == "OPEN_SHORT"
    assert approved["direction_candidate"] == "SHORT"


def test_open_score_without_strategy_confirmation_does_not_execute(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="OPEN_LONG",
                reason="ready",
            )
        },
        TradeDecision.hold("strategy denied"),
    )

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == []
    assert governor.snapshots[profile.symbol].reason == "STRATEGY_HOLD"


def test_open_position_with_cold_score_is_audited_and_can_close(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    opened = FuturesPosition(
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine(positions={profile.symbol: opened})
    router = FakeRouter(position=opened)
    governor.execution_engine = execution
    governor.market_data_router = router
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {profile.symbol: SignalScore(symbol=profile.symbol, score=10, reason="cold")},
        TradeDecision.close("close despite cold"),
    )

    governor.run_cycle(now=time.time() + 1)

    assert execution.audited == [profile.symbol]
    assert router.heavy_calls == [profile.symbol]
    assert execution.executed == [(profile.symbol, "CLOSE_POSITION")]


def test_close_position_works_for_short_position(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    opened = FuturesPosition(
        symbol=profile.symbol,
        direction=PositionDirection.SHORT,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine(positions={profile.symbol: opened})
    router = FakeRouter(position=opened)
    governor.execution_engine = execution
    governor.market_data_router = router
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="CLOSE_POSITION",
                reason="AI_CLOSE_POSITION",
                metadata=_ai_metadata(
                    action_candidate="OPEN_LONG",
                    direction_candidate="LONG",
                ),
            )
        },
        TradeDecision.close("AI_CLOSE_POSITION"),
    )

    governor.run_cycle(now=time.time() + 1)

    assert execution.audited == [profile.symbol]
    assert router.heavy_calls == [profile.symbol]
    assert execution.executed == [(profile.symbol, "CLOSE_POSITION")]


def test_governor_passes_synced_position_to_hot_worker(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    opened_position = FuturesPosition(
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )
    hot_worker = RecordingHotWorker()

    def sync_position_state(_symbol_config, position):
        return FuturesPosition(
            symbol=position.symbol,
            direction=position.direction,
            quantity=position.quantity,
            entry_price=position.entry_price,
            unrealized_pnl=position.unrealized_pnl,
            opened_at=123.0,
        )

    execution.sync_position_state = sync_position_state
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter(position=opened_position)
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="CLOSE_POSITION",
                reason="ready close",
            )
        },
        TradeDecision.close("ready close"),
    )
    governor.hot_symbol_worker = hot_worker

    governor.run_cycle(now=time.time() + 1)

    assert hot_worker.opened_at == 123.0


def test_governor_records_block_when_portfolio_denies(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol="BTCUSDT",
        score=95,
        action="OPEN_LONG",
        reason="ready",
        metadata=_ai_metadata(action_candidate="OPEN_LONG", direction_candidate="LONG"),
    )
    governor.events_journal = FakeJournal()
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=False)

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == []
    assert governor.snapshots["BTCUSDT"].status is SymbolRuntimeStatus.BLOCKED
    assert governor.snapshots["BTCUSDT"].reason == "MAX_OPEN_POSITIONS"
    decision = _flow_decisions(governor)[-1]
    assert decision["stage"] == "PORTFOLIO_GOVERNOR"
    assert decision["classification"] == "operacional"
    assert decision["reason"] == "MAX_OPEN_POSITIONS"
    assert decision["execution_will_run"] is False
    blocked = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_blocked_by_governor"
    )
    assert blocked["model_source"] == "SYMBOL_REFINED"
    assert blocked["long_score"] == -0.2
    assert blocked["short_score"] == 0.8
    assert blocked["governor_final_action"] == "BLOCKED"
    assert blocked["governor_block_reason"] == "MAX_OPEN_POSITIONS"


def test_governor_marks_warmup_separately_and_schedules_short_interval(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine()
    warmup = SignalScore(
        symbol="BTCUSDT",
        score=0,
        reason="Aguardando historico minimo para score RSI: 2/15",
    )
    now = time.time() + 1
    governor.execution_engine = execution
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator({profile.symbol: warmup})

    governor.run_cycle(now=now)

    snapshot = governor.snapshots["BTCUSDT"]
    assert snapshot.status is SymbolRuntimeStatus.WARMUP
    assert snapshot.next_check_at == now + 5.0
    assert execution.executed == []


def test_governor_uses_governor_max_symbols_per_cycle_not_max_active_symbols(
    config_factory,
    monkeypatch,
):
    config = config_factory(symbols=("BTCUSDT",))
    profiles = [
        symbol_config_from_app_config(config, symbol=symbol)
        for symbol in ("BTCUSDT", "ETHUSDT", "APTUSDT", "SOLUSDT", "BNBUSDT")
    ]
    monkeypatch.setattr(
        "governor.alphora_governor.resolve_governor_symbol_configs",
        lambda *args, **kwargs: profiles,
    )
    monkeypatch.setattr(
        "governor.alphora_governor.load_portfolio_risk_settings",
        lambda path: PortfolioRiskSettings(governor_max_symbols_per_cycle=5),
    )

    governor = AlphoraGovernor.from_config(config, FakeClient())

    assert config.max_active_symbols == 1
    assert governor.max_symbols_per_cycle == 5


def test_ranking_prioritizes_open_position_before_ready(config_factory):
    config = config_factory(symbols=("BTCUSDT", "ETHUSDT"))
    profiles = [
        symbol_config_from_app_config(config, symbol="BTCUSDT"),
        symbol_config_from_app_config(config, symbol="ETHUSDT"),
    ]
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=profiles)
    governor.scheduler.update(SignalScore("BTCUSDT", 10, reason="cold"), now=100)
    governor.scheduler.update(SignalScore("ETHUSDT", 95, reason="ready"), now=100)

    ranked = governor._opportunity_service().rank_due_symbols(100, open_symbols={"BTCUSDT"})

    assert ranked[:2] == ["BTCUSDT", "ETHUSDT"]


def test_portfolio_max_new_positions_per_cycle_blocks_second_open_in_same_cycle(config_factory):
    class MultiSymbolClient(FakeClient):
        def ticker_price(self, symbol, *, timeout=None):
            return {"price": "100"}

        def exchange_info(self, *, timeout=None):
            data = super().exchange_info(timeout=timeout)
            data["symbols"].append(
                {
                    "symbol": "ETHUSDT",
                    "filters": [
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "stepSize": "0.001",
                            "minQty": "0.001",
                        },
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.10",
                        },
                        {
                            "filterType": "MIN_NOTIONAL",
                            "notional": "5",
                        },
                    ],
                }
            )
            return data

    config = config_factory(symbols=("BTCUSDT", "ETHUSDT"))
    profiles = [
        symbol_config_from_app_config(config, symbol="BTCUSDT"),
        symbol_config_from_app_config(config, symbol="ETHUSDT"),
    ]
    governor = AlphoraGovernor(
        config=config,
        client=MultiSymbolClient(),
        symbol_configs=profiles,
        portfolio_settings=PortfolioRiskSettings(
            max_open_positions=5,
            max_new_positions_per_cycle=1,
        ),
        max_symbols_per_cycle=2,
    )
    ready_scores = {
        symbol: SignalScore(symbol=symbol, score=95, action="OPEN_LONG", reason="ready")
        for symbol in ("BTCUSDT", "ETHUSDT")
    }
    execution = FakeExecutionEngine()
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = FakeStrategyCoordinator(ready_scores)
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = PortfolioRiskEngine(governor.portfolio_settings)

    governor.run_cycle(now=time.time() + 1)

    assert len(execution.executed) == 1
    blocked = [
        snapshot
        for snapshot in governor.snapshots.values()
        if snapshot.status is SymbolRuntimeStatus.BLOCKED
    ]
    assert blocked[0].reason == "MULTIPLE_READY_BUT_PORTFOLIO_LIMITED"


def test_portfolio_snapshot_uses_available_balance_when_account_info_available(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.execution_engine = FakeExecutionEngine()

    snapshot = governor._portfolio_account_snapshot()

    assert snapshot["available_balance_usdt"] == "123.45"
    assert snapshot["new_positions_this_cycle"] == 0


def test_daily_metrics_reads_paper_trades_files(config_factory, tmp_path):
    config = config_factory(execution_mode=ExecutionMode.PAPER_LIVE)
    profile = symbol_config_from_app_config(config)
    today = datetime.now(timezone.utc).date().isoformat()
    paper_path = (
        tmp_path / "data" / f"paper_trades_{profile.symbol.lower()}_{config.environment_key}.jsonl"
    )
    _write_jsonl(
        paper_path,
        [
            {"timestamp": f"{today}T10:00:00Z", "event_type": "paper_position_opened"},
            {
                "timestamp": f"{today}T10:05:00Z",
                "event_type": "paper_position_closed",
                "data": {"net_pnl": "4.25"},
            },
        ],
    )
    reader = _metrics_reader(config, [profile])

    metrics = reader.read()

    assert metrics["daily_trades"] == 1
    assert metrics["daily_realized_pnl"] == "4.25"
    assert metrics["consecutive_losses"] == 0


def test_daily_metrics_orders_events_by_timestamp_for_consecutive_losses(config_factory, tmp_path):
    config = config_factory(
        execution_mode=ExecutionMode.PAPER_LIVE,
        symbols=("APTUSDT", "BTCUSDT"),
    )
    data_dir = tmp_path / "data"
    profiles = [
        replace(
            symbol_config_from_app_config(config, symbol="APTUSDT"),
            journal_path=str(data_dir / "trades_aptusdt_testnet.jsonl"),
        ),
        replace(
            symbol_config_from_app_config(config, symbol="BTCUSDT"),
            journal_path=str(data_dir / "trades_btcusdt_testnet.jsonl"),
        ),
    ]
    today = datetime.now(timezone.utc).date().isoformat()
    _write_jsonl(
        data_dir / f"paper_trades_aptusdt_{config.environment_key}.jsonl",
        [
            {
                "timestamp": f"{today}T10:05:00Z",
                "event_type": "paper_position_closed",
                "data": {"net_pnl": "-1.00"},
            }
        ],
    )
    _write_jsonl(
        data_dir / f"paper_trades_btcusdt_{config.environment_key}.jsonl",
        [
            {
                "timestamp": f"{today}T10:00:00Z",
                "event_type": "paper_position_closed",
                "data": {"net_pnl": "2.00"},
            }
        ],
    )
    reader = _metrics_reader(config, profiles)

    metrics = reader.read()

    assert metrics["daily_realized_pnl"] == "1.00"
    assert metrics["consecutive_losses"] == 1


def test_daily_metrics_tracks_unknown_live_close_pnl(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    today = datetime.now(timezone.utc).date().isoformat()
    _write_jsonl(
        Path(profile.journal_path),
        [
            {
                "timestamp": f"{today}T10:00:00Z",
                "event_type": "position_closed",
                "data": {"net_pnl": None, "pnl_status": "unknown_live_close"},
            }
        ],
    )
    reader = _metrics_reader(config, [profile])

    metrics = reader.read()

    assert metrics["daily_realized_pnl"] == "0"
    assert metrics["pnl_unknown_closes"] == 1


def test_daily_metrics_reuses_cache_until_journal_signature_changes(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    today = datetime.now(timezone.utc).date().isoformat()
    path = Path(profile.journal_path)
    _write_jsonl(
        path,
        [
            {
                "timestamp": f"{today}T10:00:00Z",
                "event_type": "position_closed",
                "data": {"net_pnl": "1.00"},
            }
        ],
    )
    reader = _metrics_reader(config, [profile])

    first = reader.read()
    second = reader.read()
    with path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "timestamp": f"{today}T10:05:00Z",
                    "event_type": "position_closed",
                    "data": {"net_pnl": "2.00"},
                }
            )
            + "\n"
        )
    third = reader.read()

    assert first == second
    assert third["daily_realized_pnl"] == "3.00"


class FailingAccountClient(FakeClient):
    def account_info(self, *, timeout=None):
        raise RuntimeError("erro simulado ao consultar saldo")


def test_account_info_failure_marks_snapshot_and_logs_portuguese_reason(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(
        config=config,
        client=FailingAccountClient(),
        symbol_configs=[profile],
    )
    governor.execution_engine = FakeExecutionEngine()
    governor.events_journal = FakeJournal()

    snapshot = governor._portfolio_account_snapshot()

    assert snapshot["account_info_failed"] is True
    assert snapshot["balance_unavailable_blocks_new_trades"] is True
    assert "Não foi possível confirmar o saldo" in snapshot["motivo_pt"]
    assert "Verifique chave API" in snapshot["como_corrigir"]

    assert any(
        event_type == "account_info_failed"
        and data["bloqueia_novas_entradas"] is True
        and data["bloqueia_fechamentos"] is False
        and "Não foi possível confirmar o saldo" in data["motivo_pt"]
        for event_type, data in governor.events_journal.events
    )


def test_portfolio_blocks_new_trade_when_balance_is_not_confirmed(config_factory):
    from governor.portfolio_risk_engine import (
        BALANCE_UNAVAILABLE_REASON,
        PortfolioRiskEngine,
    )

    config = config_factory()
    profile = symbol_config_from_app_config(config)
    intent = TradeIntent(
        symbol=profile.symbol,
        action="OPEN_LONG",
        score=95,
        priority=SignalScore(
            symbol=profile.symbol, score=95, action="OPEN_LONG", reason="ready"
        ).priority,
        confidence=95,
        reason="entrada confirmada",
        strategy_name="tests.PortfolioStrategy",
        market_price="100",
        suggested_next_check_seconds=0,
        metadata={"notional": "50"},
    )

    decision = PortfolioRiskEngine(
        PortfolioRiskSettings(max_open_positions=5)
    ).evaluate_trade_intent(
        intent,
        [profile],
        {
            "account_info_failed": True,
            "account_info_error": "erro simulado ao consultar saldo",
            "open_positions": [],
            "new_positions_this_cycle": 0,
        },
    )

    assert decision.allowed is False
    assert decision.reason.startswith(BALANCE_UNAVAILABLE_REASON)
    assert decision.checks["bloqueia_novas_entradas"] is True
    assert decision.checks["bloqueia_fechamentos"] is False
    assert "Não foi possível confirmar o saldo" in decision.checks["motivo_pt"]
    assert "Verifique chave API" in decision.checks["como_corrigir"]


def test_portfolio_still_allows_close_when_balance_is_not_confirmed(config_factory):
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    intent = TradeIntent(
        symbol=profile.symbol,
        action="CLOSE_POSITION",
        score=95,
        priority=SignalScore(
            symbol=profile.symbol, score=95, action="CLOSE_POSITION", reason="close"
        ).priority,
        confidence=95,
        reason="fechar por segurança",
        strategy_name="tests.PortfolioStrategy",
        market_price="100",
        suggested_next_check_seconds=0,
        metadata={},
    )

    decision = PortfolioRiskEngine(
        PortfolioRiskSettings(max_open_positions=5)
    ).evaluate_trade_intent(
        intent,
        [profile],
        {
            "account_info_failed": True,
            "open_positions": [],
        },
    )

    assert decision.allowed is True
    assert decision.reason == "APPROVED_NO_NEW_POSITION"


def test_adaptive_manager_shadow_records_open_position_evaluation(config_factory):
    config = config_factory(adaptive_manager_enabled=True)
    profile = symbol_config_from_app_config(config)
    opened = FuturesPosition(
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("1"),
        opened_at=1.0,
    )
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine(positions={profile.symbol: opened})
    router = FakeRouter(position=opened)
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = router
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=20,
                reason="open position",
            )
        }
    )

    governor.run_cycle(now=time.time() + 1)

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_management_shadow_evaluated"
    )
    assert event["symbol"] == profile.symbol
    assert event["evaluation"].source == "SHADOW"
    assert event["diagnostics"]["shadow_only"] is True


def test_adaptive_adjustment_plan_shadow_is_created_for_open_position(config_factory):
    config = config_factory(adaptive_manager_enabled=True)
    profile = symbol_config_from_app_config(config)
    opened = FuturesPosition(
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("1"),
        opened_at=1.0,
    )
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    execution = FakeExecutionEngine(positions={profile.symbol: opened})
    router = FakeRouter(position=opened)
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = router
    governor.strategy_coordinator = FakeStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=20,
                reason="open position",
            )
        }
    )

    governor.run_cycle(now=time.time() + 1)

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_plan_shadow_created"
    )
    assert event["symbol"] == profile.symbol
    assert event["adjustment_plan"].shadow_only is True
    assert event["execution_enabled"] is False
    assert event["plan_id"].startswith("adj_")


def test_governor_records_learning_recommendations_when_enabled(config_factory):
    config = config_factory(learning_store_enabled=True)
    profile = symbol_config_from_app_config(config)
    trades_path = Path(config.trade_journal_path)
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "event_type": "trade_plan_outcome_recorded",
                "data": {
                    "outcome": {
                        "symbol": profile.symbol,
                        "trade_plan_id": "tp_test",
                        "outcome_status": "WIN",
                        "net_pnl": "1.00",
                        "adaptive_adjustments_applied": 0,
                    }
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()

    governor._learning().record_recommendations()

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "learning_recommendation_report_created"
    )
    assert event["total_outcomes"] == 1
    assert event["can_use_for_execution"] is False
    assert event["recommendations_count"] >= 1


def test_governor_records_learning_advisory_shadow_when_enabled(config_factory):
    config = config_factory(learning_store_enabled=True)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.learning_state.latest_report_payload = {
        "status": "READY",
        "total_outcomes": 25,
        "can_use_for_execution": False,
        "recommendations": [
            {
                "action": "REDUCE_SYMBOL_PRIORITY_REVIEW",
                "severity": "WARNING",
                "reason": "Simbolo com PnL negativo e baixa taxa de acerto",
                "scope": "SYMBOL",
                "symbol": profile.symbol,
                "confidence": 70,
            }
        ],
    }

    governor._learning().record_advisory_shadow(
        symbol_config=profile,
        action="OPEN_LONG",
    )

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "learning_advisory_shadow_evaluated"
    )
    assert event["symbol"] == profile.symbol
    assert event["status"] == "WARNING"
    assert event["severity"] == "WARNING"
    assert event["would_block_trade"] is True
    assert event["can_use_for_execution"] is False
    assert event["shadow_only"] is True


def test_governor_attaches_learning_advisory_to_trade_plan(config_factory):
    from governor.trade_pipeline import TradePipelineResult
    from trading.learning_advisory import LearningAdvisoryEvaluation
    from trading.plan import TradePlan

    config = config_factory(learning_store_enabled=True)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()

    result = TradePipelineResult(
        intent=None,
        context=None,
        plan=TradePlan(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="ALLOW_NORMAL",
            strategy_name="tests.strategy",
            signal_confidence=80,
            quality_score=70,
            entry_reference_price=Decimal("100"),
            quantity=Decimal("0.1"),
            notional=Decimal("10"),
            stop_price=Decimal("98"),
            take_profit_price=Decimal("104"),
            estimated_entry_fee=Decimal("0.01"),
            estimated_exit_fee=Decimal("0.01"),
            estimated_spread_cost=Decimal("0.01"),
            estimated_slippage_cost=Decimal("0.01"),
            estimated_funding_cost=Decimal("0"),
            estimated_round_trip_cost=Decimal("0.04"),
            expected_net_gain=Decimal("0.36"),
            expected_net_loss=Decimal("-0.24"),
            reward_risk=Decimal("1.5"),
            size_multiplier=Decimal("1"),
            execution_style="MARKET",
            management_style="STATIC",
            reason="test",
            block_reason=None,
            diagnostics={},
        ),
        comparison=None,
    )
    advisory = LearningAdvisoryEvaluation(
        symbol=profile.symbol,
        action="OPEN_LONG",
        status="WARNING",
        severity="WARNING",
        reason="LEARNING_RECOMMENDATIONS_REQUIRE_ATTENTION",
        recommendations_count=1,
        relevant_recommendations_count=1,
        total_outcomes=25,
        would_block_trade=True,
        would_reduce_priority=True,
        can_use_for_execution=False,
        shadow_only=True,
    )

    updated = governor._learning().attach_advisory_to_trade_plan(
        result=result,
        learning_advisory=advisory,
    )

    assert "learning_advisory" in updated.plan.diagnostics
    assert updated.plan.diagnostics["learning_advisory"]["status"] == "WARNING"

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_learning_advisory_attached"
    )
    assert event["symbol"] == profile.symbol
    assert event["advisory_status"] == "WARNING"
    assert event["would_block_trade"] is True
    assert event["can_use_for_execution"] is False

    guard_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_learning_guard_shadow_evaluated"
    )
    assert guard_event["symbol"] == profile.symbol
    assert guard_event["guard_status"] == "WOULD_BLOCK"
    assert guard_event["suggested_action"] == "BLOCK_TRADE_SHADOW"
    assert guard_event["would_block_trade"] is True
    assert guard_event["can_use_for_execution"] is False


def test_governor_records_learning_execution_shadow(config_factory):
    from governor.trade_pipeline import TradePipelineResult
    from trading.plan import TradePlan

    config = config_factory(learning_store_enabled=True)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()

    result = TradePipelineResult(
        intent=None,
        context=None,
        plan=TradePlan(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="ALLOW_NORMAL",
            strategy_name="tests.strategy",
            signal_confidence=80,
            quality_score=70,
            entry_reference_price=Decimal("100"),
            quantity=Decimal("0.2"),
            notional=Decimal("20"),
            stop_price=Decimal("98"),
            take_profit_price=Decimal("104"),
            estimated_entry_fee=Decimal("0.01"),
            estimated_exit_fee=Decimal("0.01"),
            estimated_spread_cost=Decimal("0.01"),
            estimated_slippage_cost=Decimal("0.01"),
            estimated_funding_cost=Decimal("0"),
            estimated_round_trip_cost=Decimal("0.04"),
            expected_net_gain=Decimal("0.36"),
            expected_net_loss=Decimal("-0.24"),
            reward_risk=Decimal("1.5"),
            size_multiplier=Decimal("1"),
            execution_style="MARKET",
            management_style="STATIC",
            reason="test",
            block_reason=None,
            diagnostics={
                "learning_guard": {
                    "guard_id": "lg_test",
                    "status": "WOULD_REDUCE_SIZE",
                    "suggested_action": "REDUCE_SIZE_SHADOW",
                    "reason": "LEARNING_ADVISORY_WOULD_REDUCE_SIZE",
                    "suggested_size_multiplier": "0.5",
                    "would_reduce_size": True,
                }
            },
        ),
        comparison=None,
    )

    governor._learning().record_execution_shadow(
        trade_pipeline_result=result,
        action="OPEN_LONG",
        planned_quantity=Decimal("0.2"),
    )

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "learning_execution_shadow_evaluated"
    )
    assert event["symbol"] == profile.symbol
    assert event["status"] == "WOULD_REDUCE_SIZE"
    assert event["suggested_action"] == "REDUCE_SIZE_SHADOW"
    assert event["original_quantity"] == Decimal("0.2")
    assert event["shadow_quantity"] == Decimal("0.10")
    assert event["can_use_for_execution"] is False
    assert event["shadow_only"] is True


def test_governor_records_learning_shadow_impact_report_when_enabled(config_factory):
    config = config_factory(learning_store_enabled=True)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    journal_path = governor.events_journal.path
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "event_type": "learning_execution_shadow_evaluated",
                    "data": {
                        "symbol": profile.symbol,
                        "action": "OPEN_LONG",
                        "plan_id": "tp_test",
                        "decision_id": "lex_test",
                        "status": "WOULD_BLOCK",
                        "suggested_action": "BLOCK_EXECUTION_SHADOW",
                        "original_quantity": "0.1",
                        "shadow_quantity": "0",
                        "suggested_size_multiplier": "0",
                        "would_block_trade": True,
                        "would_reduce_size": False,
                        "can_use_for_execution": False,
                        "shadow_only": True,
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        file.write(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:01+00:00",
                    "event_type": "trade_plan_outcome_recorded",
                    "data": {
                        "symbol": profile.symbol,
                        "trade_plan_id": "tp_test",
                        "outcome_id": "out_test",
                        "outcome_status": "LOSS",
                        "net_pnl": "-1.00",
                        "source": "PAPER",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    governor._learning().record_shadow_impact_report()

    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    report_events = [
        event for event in events if event["event_type"] == "learning_shadow_impact_report_created"
    ]

    assert len(report_events) == 1
    assert report_events[0]["data"]["total_shadow_decisions"] == 1
    assert report_events[0]["data"]["matched_outcomes"] == 1
    assert report_events[0]["data"]["would_block_count"] == 1
    assert report_events[0]["data"]["estimated_loss_avoided_by_blocks"] == "1.00"
    assert report_events[0]["data"]["confidence_status"] == "INSUFFICIENT_DATA"
    assert report_events[0]["data"]["activation_status"] == "OBSERVE_MORE"
    assert report_events[0]["data"]["paper_activation_status"] == "DISABLED"
    assert report_events[0]["data"]["paper_activation_enabled"] is False
    assert report_events[0]["data"]["can_apply_reduction_in_paper"] is False
    assert report_events[0]["data"]["eligible_for_paper_reduction"] is False
    assert report_events[0]["data"]["can_activate_testnet"] is False
    assert report_events[0]["data"]["can_use_for_execution"] is False


def test_governor_applies_learning_paper_reduction(config_factory):
    from config import ExecutionMode
    from trading.learning_execution_shadow import LearningExecutionShadowDecision

    config = config_factory(
        execution_mode=ExecutionMode.PAPER_LIVE,
        learning_store_enabled=True,
        learning_paper_activation_enabled=True,
        learning_paper_reduction_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.learning_state.latest_shadow_impact_report_payload = {
        "can_apply_reduction_in_paper": True,
        "can_apply_block_in_paper": False,
    }

    decision = governor._learning().evaluate_paper_execution(
        learning_execution_decision=LearningExecutionShadowDecision(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="WOULD_REDUCE_SIZE",
            suggested_action="REDUCE_SIZE_SHADOW",
            reason="test",
            plan_id="tp_test",
            original_quantity=Decimal("0.2"),
            shadow_quantity=Decimal("0.1"),
            suggested_size_multiplier=Decimal("0.5"),
            would_reduce_size=True,
        )
    )

    assert decision is not None
    assert decision.status == "REDUCTION_APPLIED"
    assert decision.reduced is True
    assert decision.final_quantity == Decimal("0.1")
    assert decision.can_use_for_execution is True

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "learning_paper_execution_evaluated"
    )
    assert event["status"] == "REDUCTION_APPLIED"
    assert event["final_quantity"] == Decimal("0.1")
    assert event["can_activate_testnet"] is False
    assert event["can_activate_real"] is False


def test_governor_applies_learning_paper_block(config_factory):
    from config import ExecutionMode
    from trading.learning_execution_shadow import LearningExecutionShadowDecision

    config = config_factory(
        execution_mode=ExecutionMode.PAPER_LIVE,
        learning_store_enabled=True,
        learning_paper_activation_enabled=True,
        learning_paper_reduction_enabled=True,
        learning_paper_block_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.learning_state.latest_shadow_impact_report_payload = {
        "can_apply_reduction_in_paper": True,
        "can_apply_block_in_paper": True,
    }

    decision = governor._learning().evaluate_paper_execution(
        learning_execution_decision=LearningExecutionShadowDecision(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="WOULD_BLOCK",
            suggested_action="BLOCK_EXECUTION_SHADOW",
            reason="test",
            plan_id="tp_test",
            original_quantity=Decimal("0.2"),
            shadow_quantity=Decimal("0"),
            suggested_size_multiplier=Decimal("0"),
            would_block_trade=True,
        )
    )

    assert decision is not None
    assert decision.status == "BLOCK_APPLIED"
    assert decision.blocked is True
    assert decision.final_quantity == Decimal("0")
    assert decision.can_use_for_execution is True


def test_governor_records_learning_testnet_observation(config_factory):

    from config import ExecutionMode
    from trading.learning_execution_shadow import LearningExecutionShadowDecision

    config = config_factory(
        execution_mode=ExecutionMode.TESTNET_LIVE,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.learning_state.latest_shadow_impact_report_payload = {
        "can_apply_reduction_in_paper": True,
        "can_apply_block_in_paper": False,
    }

    evaluation = governor._learning().record_testnet_observation(
        learning_execution_decision=LearningExecutionShadowDecision(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="WOULD_REDUCE_SIZE",
            suggested_action="REDUCE_SIZE_SHADOW",
            reason="test",
            plan_id="tp_test",
            original_quantity=Decimal("0.2"),
            shadow_quantity=Decimal("0.1"),
            suggested_size_multiplier=Decimal("0.5"),
            would_reduce_size=True,
        )
    )

    assert evaluation is not None
    assert evaluation.status == "OBSERVED_REDUCTION_SIGNAL"
    assert evaluation.can_use_for_execution is False
    assert evaluation.can_activate_testnet is False
    assert evaluation.can_activate_real is False

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "learning_testnet_observation_evaluated"
    )
    assert event["status"] == "OBSERVED_REDUCTION_SIGNAL"
    assert event["would_observe_reduction_signal"] is True
    assert event["can_use_for_execution"] is False


def test_governor_applies_learning_testnet_reduction(config_factory):
    from config import (
        ArchitectureMode,
        ExecutionMode,
        IntelligentActivationLevel,
    )
    from trading.learning_execution_shadow import LearningExecutionShadowDecision

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_REDUCE,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.learning_state.latest_shadow_impact_report_payload = {
        "can_apply_reduction_in_paper": True,
        "can_apply_block_in_paper": False,
    }

    decision = governor._learning().evaluate_testnet_execution(
        learning_execution_decision=LearningExecutionShadowDecision(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="WOULD_REDUCE_SIZE",
            suggested_action="REDUCE_SIZE_SHADOW",
            reason="test",
            plan_id="tp_test",
            original_quantity=Decimal("0.2"),
            shadow_quantity=Decimal("0.1"),
            suggested_size_multiplier=Decimal("0.5"),
            would_reduce_size=True,
        )
    )

    assert decision is not None
    assert decision.status == "REDUCTION_APPLIED"
    assert decision.reduced is True
    assert decision.blocked is False
    assert decision.final_quantity == Decimal("0.1")
    assert decision.can_use_for_execution is True
    assert decision.can_activate_testnet is True
    assert decision.can_activate_real is False

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "learning_testnet_execution_evaluated"
    )
    assert event["status"] == "REDUCTION_APPLIED"
    assert event["final_quantity"] == Decimal("0.1")
    assert event["can_use_for_execution"] is True
    assert event["can_activate_testnet"] is True
    assert event["can_activate_real"] is False


def test_phase1_testnet_reduce_does_not_apply_quantity_when_dynamic_sizing_disabled(
    config_factory,
    monkeypatch,
):
    from config import (
        ArchitectureMode,
        ExecutionMode,
        IntelligentActivationLevel,
    )
    from trading.learning_execution_shadow import LearningExecutionShadowDecision

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_REDUCE,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
        dynamic_sizing_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    def fake_learning_execution_shadow(**_kwargs):
        return LearningExecutionShadowDecision(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="WOULD_REDUCE_SIZE",
            suggested_action="REDUCE_SIZE_SHADOW",
            reason="test_phase1_reduce",
            plan_id="tp_phase1",
            original_quantity=Decimal("0.2"),
            shadow_quantity=Decimal("0.1"),
            suggested_size_multiplier=Decimal("0.5"),
            would_reduce_size=True,
        )

    monkeypatch.setattr(
        LearningService,
        "record_execution_shadow",
        lambda _learning, **kwargs: fake_learning_execution_shadow(**kwargs),
    )

    monkeypatch.setattr(
        LearningService,
        "latest_shadow_impact_report",
        lambda _learning: {
            "can_apply_reduction_in_paper": True,
            "can_apply_block_in_paper": False,
        },
    )

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None

    ignored_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "learning_testnet_reduction_ignored"
    )
    assert ignored_event["reason"] == "DYNAMIC_SIZING_DISABLED"
    assert ignored_event["original_quantity"] == Decimal("0.2")
    assert ignored_event["final_quantity"] == Decimal("0.1")

    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_execution_ready"
    )
    assert result_event["dynamic_sizing_enabled"] is False
    assert result_event["planned_quantity_applied"] is False
    assert result_event["planned_quantity_passed"] is None
    assert result_event["reason"] == "DYNAMIC_SIZING_DISABLED"


def test_phase1_testnet_reduce_applies_quantity_when_dynamic_sizing_enabled(
    config_factory,
    monkeypatch,
):
    from config import (
        ArchitectureMode,
        ExecutionMode,
        IntelligentActivationLevel,
    )
    from trading.learning_execution_shadow import LearningExecutionShadowDecision

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_REDUCE,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
        dynamic_sizing_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    def fake_learning_execution_shadow(**_kwargs):
        return LearningExecutionShadowDecision(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="WOULD_REDUCE_SIZE",
            suggested_action="REDUCE_SIZE_SHADOW",
            reason="test_phase1_reduce",
            plan_id="tp_phase1",
            original_quantity=Decimal("0.2"),
            shadow_quantity=Decimal("0.1"),
            suggested_size_multiplier=Decimal("0.5"),
            would_reduce_size=True,
        )

    monkeypatch.setattr(
        LearningService,
        "record_execution_shadow",
        lambda _learning, **kwargs: fake_learning_execution_shadow(**kwargs),
    )

    monkeypatch.setattr(
        LearningService,
        "latest_shadow_impact_report",
        lambda _learning: {
            "can_apply_reduction_in_paper": True,
            "can_apply_block_in_paper": False,
        },
    )

    governor.run_cycle(now=time.time() + 1)

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.1")

    reduced_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_reduced_by_learning_testnet"
    )
    assert reduced_event["original_quantity"] == Decimal("0.2")
    assert reduced_event["final_quantity"] == Decimal("0.1")

    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_plan_execution_ready"
    )
    assert result_event["dynamic_sizing_enabled"] is True
    assert result_event["planned_quantity_applied"] is True
    assert result_event["planned_quantity_passed"] == "0.1"
    assert result_event["reason"] == "Learning reduziu a quantidade em TESTNET_LIVE"


def test_phase2_intelligent_order_shadow_records_plan_without_changing_execution(
    config_factory,
):
    from config import ArchitectureMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.SHADOW_ONLY,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_plan_shadow_created"
    )

    assert event["status"] == "SHADOW_READY"
    assert event["shadow_only"] is True
    assert event["can_use_for_execution"] is False
    assert event["trade_plan_id"].startswith("tp_")
    assert event["plan_id"].startswith("iop_")
    assert event["recommended_quantity"] is not None
    assert event["recommended_stop_price"] is not None
    assert event["recommended_take_profit_price"] is not None
    comparison_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_plan_compared"
    )

    assert comparison_event["status"] == "COMPARED"
    assert comparison_event["shadow_only"] is True
    assert comparison_event["can_use_for_execution"] is False
    assert comparison_event["execution_unchanged"] is True
    assert comparison_event["trade_plan_id"] == event["trade_plan_id"]
    assert comparison_event["intelligent_order_plan_id"] == event["plan_id"]
    assert comparison_event["comparison_id"].startswith("ioc_")
    assert "quantity" in comparison_event["differences"]
    assert "stop" in comparison_event["differences"]
    assert "take_profit" in comparison_event["differences"]

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase3a_paper_active_applies_intelligent_order_plan_to_execution(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_activation_evaluated"
    )

    assert activation_event["status"] == "APPLIED"
    assert activation_event["applied"] is True
    assert activation_event["quantity_applied"] is True
    assert activation_event["exit_applied"] is True
    assert activation_event["can_use_for_execution"] is True
    assert activation_event["paper_only"] is True
    assert activation_event["can_activate_testnet"] is False
    assert activation_event["can_activate_real"] is False

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.5")
    assert execution.execution_kwargs[0]["planned_stop_price"] is not None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is not None


def test_phase3a_testnet_observe_does_not_apply_intelligent_order_plan(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_OBSERVE,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_activation_evaluated"
    )

    assert activation_event["status"] == "NOT_PAPER_ACTIVE"
    assert activation_event["applied"] is False
    assert activation_event["quantity_applied"] is False
    assert activation_event["exit_applied"] is False
    assert activation_event["can_use_for_execution"] is False
    assert activation_event["can_activate_testnet"] is False
    assert activation_event["can_activate_real"] is False

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase3b_paper_active_records_intelligent_activation_result(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_activation_result"
    )

    assert result_event["status"] == "APPLIED"
    assert result_event["applied"] is True
    assert result_event["quantity_applied"] is True
    assert result_event["exit_applied"] is True
    assert result_event["paper_only"] is True
    assert result_event["can_activate_testnet"] is False
    assert result_event["can_activate_real"] is False
    assert result_event["planned_quantity_passed"] == "0.5"
    assert result_event["planned_stop_price_passed"] is not None
    assert result_event["planned_take_profit_price_passed"] is not None
    assert result_event["planned_quantity_reason"] == ("Plano inteligente aplicado em PAPER_ACTIVE")
    assert result_event["planned_exit_reason"] == ("Plano inteligente aplicado em PAPER_ACTIVE")


def test_phase3b_paper_active_quantity_only_when_exit_disabled(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_activation_result"
    )

    assert result_event["status"] == "APPLIED"
    assert result_event["quantity_applied"] is True
    assert result_event["exit_applied"] is False
    assert result_event["planned_quantity_passed"] == "0.5"
    assert result_event["planned_stop_price_passed"] is None
    assert result_event["planned_take_profit_price_passed"] is None

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.5")
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase3b_paper_active_exit_only_when_sizing_disabled(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_activation_result"
    )

    assert result_event["status"] == "APPLIED"
    assert result_event["quantity_applied"] is False
    assert result_event["exit_applied"] is True
    assert result_event["planned_quantity_passed"] is None
    assert result_event["planned_stop_price_passed"] is not None
    assert result_event["planned_take_profit_price_passed"] is not None

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None
    assert execution.execution_kwargs[0]["planned_stop_price"] is not None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is not None


def test_phase4a_testnet_reduce_applies_intelligent_quantity_without_exit(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_REDUCE,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=False,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_activation_result"
    )

    assert result_event["status"] == "APPLIED"
    assert result_event["quantity_applied"] is True
    assert result_event["exit_applied"] is False
    assert result_event["testnet_only"] is True
    assert result_event["can_activate_testnet"] is True
    assert result_event["can_activate_real"] is False
    assert result_event["planned_quantity_passed"] == "0.5"
    assert result_event["planned_stop_price_passed"] is None
    assert result_event["planned_take_profit_price_passed"] is None

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.5")
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase4a_testnet_block_blocks_learning_block_signal(
    config_factory,
    monkeypatch,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel
    from trading.learning_execution_shadow import LearningExecutionShadowDecision

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_BLOCK,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    def fake_learning_execution_shadow(**_kwargs):
        return LearningExecutionShadowDecision(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="WOULD_BLOCK",
            suggested_action="BLOCK_EXECUTION_SHADOW",
            reason="test_phase4a_block",
            plan_id="tp_phase4a",
            original_quantity=Decimal("0.5"),
            shadow_quantity=Decimal("0"),
            suggested_size_multiplier=Decimal("0"),
            would_block_trade=True,
        )

    monkeypatch.setattr(
        LearningService,
        "record_execution_shadow",
        lambda _learning, **kwargs: fake_learning_execution_shadow(**kwargs),
    )

    monkeypatch.setattr(
        LearningService,
        "latest_shadow_impact_report",
        lambda _learning: {
            "can_apply_reduction_in_paper": True,
            "can_apply_block_in_paper": True,
        },
    )

    governor.run_cycle(now=time.time() + 1)

    blocked_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_blocked_by_learning_testnet"
    )

    assert blocked_event["reason"] == "Learning bloqueou a entrada em TESTNET_BLOCK"
    assert blocked_event["final_quantity"] == Decimal("0")
    assert execution.executed == []


def test_phase4b_testnet_reduce_records_finalized_event(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_REDUCE,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=False,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    finalized_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_execution_finalized"
    )

    assert finalized_event["status"] == "READY_FOR_EXECUTION"
    assert finalized_event["execution_will_run"] is True
    assert finalized_event["risk_allowed"] is True
    assert finalized_event["activation_status"] == "APPLIED"
    assert finalized_event["activation_applied"] is True
    assert finalized_event["quantity_applied"] is True
    assert finalized_event["exit_applied"] is False
    assert finalized_event["paper_only"] is False
    assert finalized_event["testnet_only"] is True
    assert finalized_event["can_activate_testnet"] is True
    assert finalized_event["can_activate_real"] is False
    assert finalized_event["planned_quantity_passed"] == "0.5"
    assert finalized_event["planned_stop_price_passed"] is None
    assert finalized_event["planned_take_profit_price_passed"] is None

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]


def test_phase4b_testnet_block_finalizes_as_blocked_without_execution(
    config_factory,
    monkeypatch,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel
    from trading.learning_execution_shadow import LearningExecutionShadowDecision

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_BLOCK,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    def fake_learning_execution_shadow(**_kwargs):
        return LearningExecutionShadowDecision(
            symbol=profile.symbol,
            action="OPEN_LONG",
            status="WOULD_BLOCK",
            suggested_action="BLOCK_EXECUTION_SHADOW",
            reason="test_phase4b_block",
            plan_id="tp_phase4b",
            original_quantity=Decimal("0.5"),
            shadow_quantity=Decimal("0"),
            suggested_size_multiplier=Decimal("0"),
            would_block_trade=True,
        )

    monkeypatch.setattr(
        LearningService,
        "record_execution_shadow",
        lambda _learning, **kwargs: fake_learning_execution_shadow(**kwargs),
    )

    monkeypatch.setattr(
        LearningService,
        "latest_shadow_impact_report",
        lambda _learning: {
            "can_apply_reduction_in_paper": True,
            "can_apply_block_in_paper": True,
        },
    )

    governor.run_cycle(now=time.time() + 1)

    finalized_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_execution_finalized"
    )

    assert finalized_event["status"] == "BLOCKED_BY_LEARNING_TESTNET"
    assert finalized_event["execution_will_run"] is False
    assert finalized_event["risk_allowed"] is True
    assert finalized_event["testnet_only"] is True
    assert finalized_event["can_activate_testnet"] is True
    assert finalized_event["can_activate_real"] is False

    assert execution.executed == []


def test_phase4b_real_live_does_not_apply_intelligent_order(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.REAL_BLOCK,
        execution_mode=ExecutionMode.REAL_LIVE,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=False,
        learning_store_enabled=True,
    )
    # Scripted test strategies are refused in REAL_LIVE; boot with a real one
    # (the coordinator is replaced by a stub below anyway).
    config = replace(
        config,
        strategy_module="strategies.futures.fixed.donchian_adx_breakout",
        strategy_class="DonchianAdxBreakoutStrategy",
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_activation_evaluated"
    )
    finalized_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_order_execution_finalized"
    )

    assert activation_event["status"] == "NOT_PAPER_ACTIVE"
    assert activation_event["applied"] is False
    assert activation_event["can_activate_testnet"] is False
    assert activation_event["can_activate_real"] is False

    assert finalized_event["execution_will_run"] is True
    assert finalized_event["activation_applied"] is False
    assert finalized_event["paper_only"] is True
    assert finalized_event["testnet_only"] is False
    assert finalized_event["can_activate_testnet"] is False
    assert finalized_event["can_activate_real"] is False

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase5a_intelligent_exit_shadow_records_plan_without_changing_execution(
    config_factory,
):
    from config import ArchitectureMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.SHADOW_ONLY,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    exit_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_plan_shadow_created"
    )

    assert exit_event["status"] == "SHADOW_READY"
    assert exit_event["plan_id"].startswith("iexit_")
    assert exit_event["trade_plan_id"].startswith("tp_")
    assert exit_event["recommended_stop_price"] is not None
    assert exit_event["recommended_take_profit_price"] is not None
    assert exit_event["risk_reward"] is not None
    assert exit_event["protection_complete"] is True
    assert exit_event["shadow_only"] is True
    assert exit_event["can_use_for_execution"] is False

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase5b_intelligent_exit_shadow_records_comparison_without_changing_execution(
    config_factory,
):
    from config import ArchitectureMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.SHADOW_ONLY,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    exit_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_plan_shadow_created"
    )
    comparison_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_plan_compared"
    )

    assert comparison_event["status"] == "COMPARED"
    assert comparison_event["comparison_id"].startswith("iexitcmp_")
    assert comparison_event["trade_plan_id"] == exit_event["trade_plan_id"]
    assert comparison_event["intelligent_exit_plan_id"] == exit_event["plan_id"]
    assert comparison_event["protection_complete"] is True
    assert comparison_event["execution_unchanged"] is True
    assert comparison_event["shadow_only"] is True
    assert comparison_event["can_use_for_execution"] is False
    assert "stop" in comparison_event["differences"]
    assert "take_profit" in comparison_event["differences"]
    assert "risk_reward" in comparison_event["differences"]

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase5c_paper_active_applies_intelligent_exit_to_execution(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_evaluated"
    )
    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_result"
    )

    assert activation_event["status"] == "APPLIED"
    assert activation_event["applied"] is True
    assert activation_event["exit_applied"] is True
    assert activation_event["paper_only"] is True
    assert activation_event["testnet_only"] is False
    assert activation_event["can_activate_testnet"] is False
    assert activation_event["can_activate_real"] is False

    assert result_event["status"] == "APPLIED"
    assert result_event["applied"] is True
    assert result_event["exit_applied"] is True
    assert result_event["planned_stop_price_passed"] is not None
    assert result_event["planned_take_profit_price_passed"] is not None
    assert result_event["planned_exit_reason"] == ("Protecao inteligente aplicada em PAPER_ACTIVE")

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None
    assert execution.execution_kwargs[0]["planned_stop_price"] is not None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is not None


def test_phase5d_testnet_block_applies_intelligent_exit_to_execution(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_BLOCK,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_evaluated"
    )
    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_result"
    )

    assert activation_event["status"] == "APPLIED"
    assert activation_event["activation_action"] == "APPLY_TESTNET_INTELLIGENT_EXIT"
    assert activation_event["applied"] is True
    assert activation_event["exit_applied"] is True
    assert activation_event["paper_only"] is False
    assert activation_event["testnet_only"] is True
    assert activation_event["can_activate_testnet"] is True
    assert activation_event["can_activate_real"] is False

    assert result_event["status"] == "APPLIED"
    assert result_event["applied"] is True
    assert result_event["exit_applied"] is True
    assert result_event["paper_only"] is False
    assert result_event["testnet_only"] is True
    assert result_event["can_activate_testnet"] is True
    assert result_event["can_activate_real"] is False
    assert result_event["planned_stop_price_passed"] is not None
    assert result_event["planned_take_profit_price_passed"] is not None
    assert result_event["planned_exit_reason"] == ("Protecao inteligente aplicada em TESTNET_BLOCK")

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.5")
    assert execution.execution_kwargs[0]["planned_stop_price"] is not None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is not None


def test_phase5d_testnet_reduce_does_not_apply_intelligent_exit_policy(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_REDUCE,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=False,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_evaluated"
    )

    assert activation_event["status"] == "NOT_PAPER_ACTIVE"
    assert activation_event["applied"] is False
    assert activation_event["exit_applied"] is False
    assert activation_event["can_activate_testnet"] is False
    assert activation_event["can_activate_real"] is False

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.5")
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase5e_intelligent_exit_shadow_records_adaptive_candidate(
    config_factory,
):
    from config import ArchitectureMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.SHADOW_ONLY,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=False,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    exit_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_plan_shadow_created"
    )

    assert exit_event["status"] == "SHADOW_READY"
    assert exit_event["adaptive_candidate_available"] is True
    assert exit_event["adaptive_stop_price"] is not None
    assert exit_event["adaptive_take_profit_price"] is not None
    assert exit_event["adaptive_risk_reward"] is not None
    assert exit_event["minimum_reward_risk"] == Decimal("2")
    assert exit_event["minimum_stop_distance_pct"] == Decimal("0.005")
    assert exit_event["recommendation_mode"] is not None
    assert exit_event["protection_quality"] is not None
    assert exit_event["shadow_only"] is True
    assert exit_event["can_use_for_execution"] is False

    # Fase 5E é shadow: a execucao continua sem planned_exit.
    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase5f_paper_active_applies_adaptive_exit_to_execution(
    config_factory,
):
    from dataclasses import replace

    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel
    from trading.intelligent_exit_builder import IntelligentExitBuilder

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    class FakeAdaptiveExitBuilder:
        def build_shadow_plan(self, **kwargs):
            base_plan = IntelligentExitBuilder().build_shadow_plan(**kwargs)
            return replace(
                base_plan,
                recommended_stop_price=Decimal("99"),
                recommended_take_profit_price=Decimal("101"),
                adaptive_stop_price=Decimal("99"),
                adaptive_take_profit_price=Decimal("102"),
                adaptive_risk_reward=Decimal("2"),
                adaptive_candidate_available=True,
                adaptive_changed=True,
                recommendation_mode="ADAPT_REWARD_RISK",
                protection_quality="WEAK",
            )

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.intelligent_exit_builder = FakeAdaptiveExitBuilder()
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_evaluated"
    )
    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_result"
    )

    assert activation_event["status"] == "APPLIED"
    assert activation_event["exit_source"] == "ADAPTIVE"
    assert activation_event["used_adaptive_exit"] is True
    assert activation_event["adaptive_exit_applied"] is True
    assert activation_event["final_stop_price"] == Decimal("99")
    assert activation_event["final_take_profit_price"] == Decimal("102")
    assert activation_event["recommendation_mode"] == "ADAPT_REWARD_RISK"
    assert activation_event["protection_quality"] == "WEAK"

    assert result_event["exit_source"] == "ADAPTIVE"
    assert result_event["used_adaptive_exit"] is True
    assert result_event["adaptive_exit_applied"] is True
    assert result_event["planned_stop_price_passed"] == "99"
    assert result_event["planned_take_profit_price_passed"] == "102"

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] is None
    assert execution.execution_kwargs[0]["planned_stop_price"] == Decimal("99")
    assert execution.execution_kwargs[0]["planned_take_profit_price"] == Decimal("102")


def test_phase5f_testnet_block_applies_adaptive_exit_to_execution(
    config_factory,
):
    from dataclasses import replace

    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel
    from trading.intelligent_exit_builder import IntelligentExitBuilder

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_BLOCK,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=True,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    class FakeAdaptiveExitBuilder:
        def build_shadow_plan(self, **kwargs):
            base_plan = IntelligentExitBuilder().build_shadow_plan(**kwargs)
            return replace(
                base_plan,
                recommended_stop_price=Decimal("99"),
                recommended_take_profit_price=Decimal("101"),
                adaptive_stop_price=Decimal("99"),
                adaptive_take_profit_price=Decimal("102"),
                adaptive_risk_reward=Decimal("2"),
                adaptive_candidate_available=True,
                adaptive_changed=True,
                recommendation_mode="ADAPT_REWARD_RISK",
                protection_quality="WEAK",
            )

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.intelligent_exit_builder = FakeAdaptiveExitBuilder()
    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_evaluated"
    )
    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_result"
    )

    assert activation_event["status"] == "APPLIED"
    assert activation_event["exit_source"] == "ADAPTIVE"
    assert activation_event["used_adaptive_exit"] is True
    assert activation_event["adaptive_exit_applied"] is True
    assert activation_event["paper_only"] is False
    assert activation_event["testnet_only"] is True
    assert activation_event["can_activate_testnet"] is True
    assert activation_event["can_activate_real"] is False
    assert activation_event["final_stop_price"] == Decimal("99")
    assert activation_event["final_take_profit_price"] == Decimal("102")

    assert result_event["exit_source"] == "ADAPTIVE"
    assert result_event["used_adaptive_exit"] is True
    assert result_event["adaptive_exit_applied"] is True
    assert result_event["planned_stop_price_passed"] == "99"
    assert result_event["planned_take_profit_price_passed"] == "102"

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.5")
    assert execution.execution_kwargs[0]["planned_stop_price"] == Decimal("99")
    assert execution.execution_kwargs[0]["planned_take_profit_price"] == Decimal("102")


def test_phase5f_testnet_reduce_still_does_not_apply_adaptive_exit(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_REDUCE,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=False,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_exit_activation_evaluated"
    )

    assert activation_event["status"] == "NOT_PAPER_ACTIVE"
    assert activation_event["applied"] is False
    assert activation_event["exit_applied"] is False
    assert activation_event["can_activate_testnet"] is False
    assert activation_event["can_activate_real"] is False

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.5")
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase6_execution_planner_records_market_now_without_changing_execution(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=True,
        execution_planner_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    execution_plan_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_execution_plan_evaluated"
    )

    assert execution_plan_event["status"] == "READY"
    assert execution_plan_event["execution_style"] == "MARKET_NOW"
    assert execution_plan_event["should_execute"] is True
    assert execution_plan_event["execution_will_run"] is True
    assert execution_plan_event["paper_only"] is True
    assert execution_plan_event["testnet_only"] is False
    assert execution_plan_event["can_activate_real"] is False

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]


def test_economic_gate_blocks_stale_trade_plan_before_execution_planner(
    config_factory,
):
    from dataclasses import replace

    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    class StaleRouter(FakeRouter):
        def get_heavy_snapshot(
            self,
            symbol,
            *,
            now=None,
            cached_position=None,
            cached_account_info=None,
            cached_open_orders=None,
        ):
            snapshot = super().get_heavy_snapshot(
                symbol,
                now=now,
                cached_position=cached_position,
                cached_account_info=cached_account_info,
                cached_open_orders=cached_open_orders,
            )
            return replace(
                snapshot,
                stale=True,
                data_age_seconds=10,
            )

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_sizing_enabled=False,
        dynamic_exit_enabled=True,
        execution_planner_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = StaleRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    gate_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "trade_blocked_by_economic_gate"
    )

    assert gate_event["reason"] == "PLAN_STATUS_NOT_EXECUTABLE"
    assert gate_event["diagnostics"]["plan_status"] == "BLOCK_SAFETY"
    assert gate_event["diagnostics"]["plan_block_reason"] == "STALE_DATA"

    flow_decision = _flow_decisions(governor)[-1]
    assert flow_decision["stage"] == "ECONOMIC_GATE"
    assert flow_decision["execution_will_run"] is False

    assert execution.executed == []


def test_phase6_execution_planner_does_not_activate_in_testnet_reduce(
    config_factory,
):
    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_REDUCE,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_sizing_enabled=True,
        dynamic_exit_enabled=False,
        execution_planner_enabled=True,
        learning_store_enabled=True,
        learning_testnet_observation_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    execution = FakeExecutionEngine()
    ready = SignalScore(
        symbol=profile.symbol,
        score=95,
        action="OPEN_LONG",
        reason="ready",
    )

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = FakeRouter()
    governor.strategy_coordinator = IntentStrategyCoordinator({profile.symbol: ready})
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    execution_plan_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "intelligent_execution_plan_evaluated"
    )

    assert execution_plan_event["status"] == "NOT_ACTIVE_LEVEL"
    assert execution_plan_event["should_execute"] is True
    assert execution_plan_event["can_use_for_execution"] is False

    assert execution.executed == [(profile.symbol, "OPEN_LONG")]
    assert execution.execution_kwargs[0]["planned_quantity"] == Decimal("0.5")
    assert execution.execution_kwargs[0]["planned_stop_price"] is None
    assert execution.execution_kwargs[0]["planned_take_profit_price"] is None


def test_phase7_governor_records_trailing_stop_management_shadow(
    config_factory,
):
    from dataclasses import replace

    from config import ArchitectureMode, IntelligentActivationLevel
    from futures.state import RuntimeState

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )

    class ManagedRouter(FakeRouter):
        def get_heavy_snapshot(
            self,
            symbol,
            *,
            now=None,
            cached_position=None,
            cached_account_info=None,
            cached_open_orders=None,
        ):
            snapshot = super().get_heavy_snapshot(
                symbol,
                now=now,
                cached_position=cached_position,
                cached_account_info=cached_account_info,
                cached_open_orders=cached_open_orders,
            )
            return replace(
                snapshot,
                position=position,
                mark_price=Decimal("101.50"),
            )

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.SHADOW_ONLY,
        adaptive_manager_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    runtime_state = RuntimeState(
        active_trade_id="trade",
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.1"),
        protection_source="TRADE_PLAN",
        trade_plan_id="tp_phase7",
        planned_stop_price=Decimal("100.20"),
        planned_take_profit_price=Decimal("104"),
    )

    execution = FakeExecutionEngine(positions={profile.symbol: position})
    execution.get_runtime_state = lambda _symbol_config: runtime_state

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = ManagedRouter(position=position)
    governor.strategy_coordinator = IntentStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="HOLD",
                reason="position_management",
            )
        }
    )
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    management_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_management_shadow_evaluated"
    )
    plan_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_plan_shadow_created"
    )

    assert management_event["action"] == "TRAILING_STOP_SHADOW"
    assert management_event["management_mode"] == "TRAILING_STOP"
    assert management_event["trailing_stop_price"] is not None
    assert management_event["should_reduce_position"] is False
    assert management_event["should_exit_position"] is False

    assert plan_event["action"] == "TRAILING_STOP"
    assert plan_event["status"] == "SHADOW_READY"
    assert plan_event["trailing_stop_price"] is not None
    assert plan_event["requires_order_change"] is True


def test_phase7_governor_records_partial_reduction_review_shadow(
    config_factory,
):
    from dataclasses import replace

    from config import ArchitectureMode, IntelligentActivationLevel
    from futures.state import RuntimeState

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )

    class ManagedRouter(FakeRouter):
        def get_heavy_snapshot(
            self,
            symbol,
            *,
            now=None,
            cached_position=None,
            cached_account_info=None,
            cached_open_orders=None,
        ):
            snapshot = super().get_heavy_snapshot(
                symbol,
                now=now,
                cached_position=cached_position,
                cached_account_info=cached_account_info,
                cached_open_orders=cached_open_orders,
            )
            return replace(
                snapshot,
                position=position,
                mark_price=Decimal("99.10"),
            )

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.SHADOW_ONLY,
        adaptive_manager_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    runtime_state = RuntimeState(
        active_trade_id="trade",
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.1"),
        protection_source="TRADE_PLAN",
        trade_plan_id="tp_phase7",
        planned_stop_price=Decimal("99"),
        planned_take_profit_price=Decimal("104"),
    )

    execution = FakeExecutionEngine(positions={profile.symbol: position})
    execution.get_runtime_state = lambda _symbol_config: runtime_state

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = ManagedRouter(position=position)
    governor.strategy_coordinator = IntentStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="HOLD",
                reason="position_management",
            )
        }
    )
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    management_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_management_shadow_evaluated"
    )
    plan_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_plan_shadow_created"
    )

    assert management_event["action"] == "PARTIAL_REDUCTION_SHADOW"
    assert management_event["management_mode"] == "PARTIAL_REDUCTION"
    assert management_event["suggested_reduction_fraction"] == Decimal("0.5")
    assert management_event["should_reduce_position"] is True
    assert management_event["should_exit_position"] is False

    assert plan_event["action"] == "PARTIAL_REDUCTION_REVIEW"
    assert plan_event["status"] == "SHADOW_REVIEW_REQUIRED"
    assert plan_event["target_reduction_fraction"] == Decimal("0.5")
    assert plan_event["requires_manual_review"] is True


def test_phase7b_paper_active_applies_adaptive_trailing_adjustment(
    config_factory,
):
    from dataclasses import replace

    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel
    from futures.state import RuntimeState

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )

    class ManagedRouter(FakeRouter):
        def get_heavy_snapshot(
            self,
            symbol,
            *,
            now=None,
            cached_position=None,
            cached_account_info=None,
            cached_open_orders=None,
        ):
            snapshot = super().get_heavy_snapshot(
                symbol,
                now=now,
                cached_position=cached_position,
                cached_account_info=cached_account_info,
                cached_open_orders=cached_open_orders,
            )
            return replace(
                snapshot,
                position=position,
                mark_price=Decimal("101.50"),
            )

    class AdaptiveExecutionEngine(FakeExecutionEngine):
        def __init__(self, positions=None):
            super().__init__(positions=positions)
            self.applied_adaptive_plans = []

        def apply_adaptive_adjustment_plan(self, *, symbol_config, adjustment_plan):
            if not adjustment_plan.execution_enabled:
                return {
                    "status": "SKIPPED",
                    "reason": "PLAN_EXECUTION_DISABLED",
                }

            self.applied_adaptive_plans.append(adjustment_plan)
            return {
                "status": "APPLIED",
                "reason": adjustment_plan.reason,
                "plan_id": adjustment_plan.plan_id,
                "target_stop_price": adjustment_plan.target_stop_price,
                "target_take_profit_price": adjustment_plan.target_take_profit_price,
            }

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_exit_enabled=True,
        adaptive_manager_enabled=True,
        adaptive_execution_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    runtime_state = RuntimeState(
        active_trade_id="trade",
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.1"),
        protection_source="TRADE_PLAN",
        trade_plan_id="tp_phase7b",
        planned_stop_price=Decimal("100.20"),
        planned_take_profit_price=Decimal("104"),
    )

    execution = AdaptiveExecutionEngine(positions={profile.symbol: position})
    execution.get_runtime_state = lambda _symbol_config: runtime_state

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = ManagedRouter(position=position)
    governor.strategy_coordinator = IntentStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="HOLD",
                reason="position_management",
            )
        }
    )
    governor.hot_symbol_worker = RecordingHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_activation_evaluated"
    )
    plan_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_plan_shadow_created"
    )
    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_execution_result"
    )

    assert activation_event["status"] == "APPLIED"
    assert activation_event["execution_enabled"] is True
    assert activation_event["applied"] is True
    assert activation_event["paper_only"] is True
    assert activation_event["can_activate_testnet"] is False
    assert activation_event["can_activate_real"] is False

    assert plan_event["action"] == "TRAILING_STOP"
    assert plan_event["execution_enabled"] is True
    assert plan_event["requires_order_change"] is True
    assert plan_event["requires_manual_review"] is False

    assert result_event["status"] == "APPLIED"
    assert len(execution.applied_adaptive_plans) == 1
    assert execution.applied_adaptive_plans[0].action == "TRAILING_STOP"


def test_phase7b_testnet_block_does_not_apply_adaptive_adjustment(
    config_factory,
):
    from dataclasses import replace

    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel
    from futures.state import RuntimeState

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )

    class ManagedRouter(FakeRouter):
        def get_heavy_snapshot(
            self,
            symbol,
            *,
            now=None,
            cached_position=None,
            cached_account_info=None,
            cached_open_orders=None,
        ):
            snapshot = super().get_heavy_snapshot(
                symbol,
                now=now,
                cached_position=cached_position,
                cached_account_info=cached_account_info,
                cached_open_orders=cached_open_orders,
            )
            return replace(
                snapshot,
                position=position,
                mark_price=Decimal("101.50"),
            )

    class AdaptiveExecutionEngine(FakeExecutionEngine):
        def __init__(self, positions=None):
            super().__init__(positions=positions)
            self.applied_adaptive_plans = []

        def apply_adaptive_adjustment_plan(self, *, symbol_config, adjustment_plan):
            if not adjustment_plan.execution_enabled:
                return {
                    "status": "SKIPPED",
                    "reason": "PLAN_EXECUTION_DISABLED",
                }

            self.applied_adaptive_plans.append(adjustment_plan)
            return {
                "status": "APPLIED",
                "reason": adjustment_plan.reason,
            }

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.TESTNET_BLOCK,
        execution_mode=ExecutionMode.TESTNET_LIVE,
        dynamic_exit_enabled=True,
        adaptive_manager_enabled=True,
        adaptive_execution_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    runtime_state = RuntimeState(
        active_trade_id="trade",
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.1"),
        protection_source="TRADE_PLAN",
        trade_plan_id="tp_phase7b",
        planned_stop_price=Decimal("100.20"),
        planned_take_profit_price=Decimal("104"),
    )

    execution = AdaptiveExecutionEngine(positions={profile.symbol: position})
    execution.get_runtime_state = lambda _symbol_config: runtime_state

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = ManagedRouter(position=position)
    governor.strategy_coordinator = IntentStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="HOLD",
                reason="position_management",
            )
        }
    )
    governor.hot_symbol_worker = RecordingHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_activation_evaluated"
    )
    plan_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_plan_shadow_created"
    )
    result_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_execution_result"
    )

    assert activation_event["status"] == "NOT_PAPER_ACTIVE"
    assert activation_event["execution_enabled"] is False
    assert activation_event["applied"] is False
    assert activation_event["can_activate_testnet"] is False
    assert activation_event["can_activate_real"] is False

    assert plan_event["action"] == "TRAILING_STOP"
    assert plan_event["execution_enabled"] is False

    assert result_event["status"] == "SKIPPED"
    assert execution.applied_adaptive_plans == []


def test_phase7b_partial_reduction_stays_manual_review_in_paper_active(
    config_factory,
):
    from dataclasses import replace

    from config import ArchitectureMode, ExecutionMode, IntelligentActivationLevel
    from futures.state import RuntimeState

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )

    class ManagedRouter(FakeRouter):
        def get_heavy_snapshot(
            self,
            symbol,
            *,
            now=None,
            cached_position=None,
            cached_account_info=None,
            cached_open_orders=None,
        ):
            snapshot = super().get_heavy_snapshot(
                symbol,
                now=now,
                cached_position=cached_position,
                cached_account_info=cached_account_info,
                cached_open_orders=cached_open_orders,
            )
            return replace(
                snapshot,
                position=position,
                mark_price=Decimal("99.10"),
            )

    class AdaptiveExecutionEngine(FakeExecutionEngine):
        def __init__(self, positions=None):
            super().__init__(positions=positions)
            self.applied_adaptive_plans = []

        def apply_adaptive_adjustment_plan(self, *, symbol_config, adjustment_plan):
            if not adjustment_plan.execution_enabled:
                return {
                    "status": "SKIPPED",
                    "reason": "PLAN_EXECUTION_DISABLED",
                }

            self.applied_adaptive_plans.append(adjustment_plan)
            return {
                "status": "APPLIED",
                "reason": adjustment_plan.reason,
            }

    config = config_factory(
        architecture_mode=ArchitectureMode.INTELLIGENT_FLOW,
        intelligent_activation_level=IntelligentActivationLevel.PAPER_ACTIVE,
        execution_mode=ExecutionMode.PAPER_LIVE,
        dynamic_exit_enabled=True,
        adaptive_manager_enabled=True,
        adaptive_execution_enabled=True,
    )
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])

    runtime_state = RuntimeState(
        active_trade_id="trade",
        symbol=profile.symbol,
        direction=PositionDirection.LONG,
        opened_at=1.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.1"),
        protection_source="TRADE_PLAN",
        trade_plan_id="tp_phase7b",
        planned_stop_price=Decimal("99"),
        planned_take_profit_price=Decimal("104"),
    )

    execution = AdaptiveExecutionEngine(positions={profile.symbol: position})
    execution.get_runtime_state = lambda _symbol_config: runtime_state

    governor.execution_engine = execution
    governor.events_journal = FakeJournal()
    governor.market_data_router = ManagedRouter(position=position)
    governor.strategy_coordinator = IntentStrategyCoordinator(
        {
            profile.symbol: SignalScore(
                symbol=profile.symbol,
                score=95,
                action="HOLD",
                reason="position_management",
            )
        }
    )
    governor.hot_symbol_worker = RecordingHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)

    governor.run_cycle(now=time.time() + 1)

    activation_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_activation_evaluated"
    )
    plan_event = next(
        data
        for event_type, data in governor.events_journal.events
        if event_type == "adaptive_adjustment_plan_shadow_created"
    )

    assert plan_event["action"] == "PARTIAL_REDUCTION_REVIEW"
    assert plan_event["requires_manual_review"] is True
    assert plan_event["execution_enabled"] is False

    assert activation_event["status"] == "MANUAL_REVIEW_REQUIRED"
    assert activation_event["execution_enabled"] is False
    assert activation_event["applied"] is False

    assert execution.applied_adaptive_plans == []
