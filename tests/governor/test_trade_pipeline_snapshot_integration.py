import time
from dataclasses import replace
from decimal import Decimal

from config.enums import ExecutionMode
from config.symbol_context import symbol_config_from_app_config
from domain.models import FuturesPosition, PositionDirection, TradeDecision
from governor.alphora_governor import AlphoraGovernor
from governor.pre_execution_guard import PreExecutionGuard
from governor.stages.execution import ExecutionRevalidation
from market_data.snapshot_provider import (
    ACCOUNT,
    SnapshotCycleSession,
    open_algo_orders,
    open_orders,
    position,
)
from market_data.snapshot_provider.sources import SOURCE_USER_DATA
from tests.governor.test_alphora_governor import (
    FakeClient,
    FakeExecutionEngine,
    FakeHotWorker,
    FakeJournal,
    FakePortfolio,
    FakeStrategyCoordinator,
)


class RecordingPreExecutionGuard:
    def __init__(self):
        self.positions = []
        self._delegate = PreExecutionGuard()

    def evaluate(self, **kwargs):
        self.positions.append(kwargs.get("position"))
        return self._delegate.evaluate(**kwargs)


class RecordingPortfolio(FakePortfolio):
    def __init__(self, allowed=True):
        super().__init__(allowed=allowed)
        self.account_snapshots = []

    def evaluate_trade_intent(
        self, intent, symbol_contexts, account_snapshot, *, override_notional=None
    ):
        self.account_snapshots.append(dict(account_snapshot or {}))
        return super().evaluate_trade_intent(
            intent,
            symbol_contexts,
            account_snapshot,
            override_notional=override_notional,
        )


class RecordingExecutionEngine(FakeExecutionEngine):
    def __init__(self):
        super().__init__()
        self.heavy_open_orders = []

    def execute(self, *, symbol_config, heavy_snapshot, command):
        self.heavy_open_orders.append(list(heavy_snapshot.open_orders))
        return super().execute(
            symbol_config=symbol_config,
            heavy_snapshot=heavy_snapshot,
            command=command,
        )


def _governor(config_factory, **config_overrides):
    config = replace(config_factory(**config_overrides), market_ws_enabled=False)
    profile = symbol_config_from_app_config(config)
    governor = AlphoraGovernor(config=config, client=FakeClient(), symbol_configs=[profile])
    governor.events_journal = FakeJournal()
    governor.strategy_coordinator = FakeStrategyCoordinator()
    governor.hot_symbol_worker = FakeHotWorker()
    governor.portfolio_risk_engine = FakePortfolio(allowed=True)
    return governor


def _disable_snapshot_provider(governor):
    provider = governor.snapshot_provider
    if provider is not None:
        provider.close()
    governor.snapshot_provider = None
    governor.market_data_router.snapshot_provider = None


def test_pre_execution_revalidation_updates_position_before_guard(config_factory, monkeypatch):
    governor = _governor(config_factory)
    guard = RecordingPreExecutionGuard()
    execution = FakeExecutionEngine()
    governor.pre_execution_guard = guard
    governor.execution_engine = execution

    original = SnapshotCycleSession.revalidate_for_execution

    def publish_external_position_then_revalidate(self, symbol, *, exit_only=False):
        def refreshed_position_risk(requested_symbol, *, timeout=None):
            return [{"symbol": requested_symbol, "positionAmt": "0.25", "entryPrice": "100"}]

        monkeypatch.setattr(governor.client, "position_risk", refreshed_position_risk)
        observed_at = time.time()
        external_position = FuturesPosition(
            symbol=symbol,
            direction=PositionDirection.LONG,
            quantity=Decimal("0.25"),
            entry_price=Decimal("100"),
            unrealized_pnl=Decimal("0"),
        )
        governor.user_data_shadow_cache.reconcile_position_from_rest(
            symbol=symbol,
            position=external_position,
            received_at=observed_at,
        )
        self.provider.publisher.publish(
            position(symbol),
            external_position,
            source=SOURCE_USER_DATA,
            observed_at=observed_at,
        )
        return original(self, symbol, exit_only=exit_only)

    monkeypatch.setattr(
        SnapshotCycleSession,
        "revalidate_for_execution",
        publish_external_position_then_revalidate,
    )

    try:
        governor.run_cycle(now=time.time())

        assert guard.positions
        assert guard.positions[-1].is_open is True
        assert execution.executed == []
        assert any(
            event_type == "coalestra_pre_execution_revalidated"
            for event_type, _payload in governor.events_journal.events
        )
    finally:
        governor.snapshot_provider.close()


def test_pre_execution_revalidation_failure_blocks_execution(config_factory, monkeypatch):
    governor = _governor(config_factory)
    execution = FakeExecutionEngine()
    governor.execution_engine = execution

    def fail_revalidation(self, symbol, *, exit_only=False):
        raise RuntimeError("synthetic revalidation failure")

    monkeypatch.setattr(
        SnapshotCycleSession,
        "revalidate_for_execution",
        fail_revalidation,
    )

    try:
        governor.run_cycle(now=time.time())

        assert execution.executed == []
        assert any(
            event_type == "coalestra_pre_execution_revalidation_failed"
            for event_type, _payload in governor.events_journal.events
        )
    finally:
        governor.snapshot_provider.close()


def test_entry_without_snapshot_session_blocks_execution(config_factory):
    governor = _governor(config_factory)
    execution = FakeExecutionEngine()
    governor.execution_engine = execution
    _disable_snapshot_provider(governor)

    governor.run_cycle(now=time.time())

    assert execution.executed == []
    assert any(
        payload.get("reason") == "COALESTRA_SESSION_UNAVAILABLE"
        for _event_type, payload in governor.events_journal.events
    )


def test_close_without_snapshot_session_is_degraded_and_audited(
    config_factory,
    monkeypatch,
):
    governor = _governor(config_factory)
    execution = FakeExecutionEngine()
    governor.execution_engine = execution
    governor.strategy_coordinator = FakeStrategyCoordinator(
        decision=TradeDecision.close("close without coalestra session")
    )

    def open_position_risk(symbol, *, timeout=None):
        return [{"symbol": symbol, "positionAmt": "0.1", "entryPrice": "100"}]

    monkeypatch.setattr(governor.client, "position_risk", open_position_risk)
    _disable_snapshot_provider(governor)

    governor.run_cycle(now=time.time())

    assert execution.executed == [("BTCUSDT", "CLOSE_POSITION")]
    assert any(
        event_type == "coalestra_exit_degraded_without_session"
        for event_type, _payload in governor.events_journal.events
    )


def test_pre_execution_revalidation_refreshes_account_and_open_orders(
    config_factory,
    monkeypatch,
):
    governor = _governor(config_factory)
    portfolio = RecordingPortfolio(allowed=True)
    execution = RecordingExecutionEngine()
    governor.portfolio_risk_engine = portfolio
    governor.execution_engine = execution

    original = SnapshotCycleSession.revalidate_for_execution

    def publish_private_updates_then_revalidate(self, symbol, *, exit_only=False):
        observed_at = time.time()

        def refreshed_account_info(*, timeout=None):
            return {"assets": [{"asset": "USDT", "availableBalance": "777"}]}

        def refreshed_open_orders(requested_symbol, *, timeout=None):
            return [{"symbol": requested_symbol, "clientOrderId": "ALPHORA-external"}]

        monkeypatch.setattr(governor.client, "account_info", refreshed_account_info)
        monkeypatch.setattr(governor.client, "open_orders", refreshed_open_orders)
        monkeypatch.setattr(
            governor.client, "open_algo_orders", lambda _symbol, *, timeout=None: []
        )
        governor.user_data_shadow_cache.reconcile_account_from_rest(
            account_info=refreshed_account_info(),
            received_at=observed_at,
        )
        governor.user_data_shadow_cache.reconcile_open_orders_from_rest(
            symbol=symbol,
            open_orders=refreshed_open_orders(symbol),
            received_at=observed_at,
        )
        self.provider.publisher.publish(
            ACCOUNT,
            {"assets": [{"asset": "USDT", "availableBalance": "777"}]},
            source=SOURCE_USER_DATA,
            observed_at=observed_at,
        )
        self.provider.publisher.publish(
            open_orders(symbol),
            [{"symbol": symbol, "clientOrderId": "ALPHORA-external"}],
            source=SOURCE_USER_DATA,
            observed_at=observed_at,
        )
        self.provider.publisher.publish(
            open_algo_orders(symbol),
            [],
            source=SOURCE_USER_DATA,
            observed_at=observed_at,
        )
        return original(self, symbol, exit_only=exit_only)

    monkeypatch.setattr(
        SnapshotCycleSession,
        "revalidate_for_execution",
        publish_private_updates_then_revalidate,
    )

    try:
        governor.run_cycle(now=time.time())

        assert len(portfolio.account_snapshots) >= 2
        assert portfolio.account_snapshots[-1]["available_balance_usdt"] == "777"
        assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
        assert execution.heavy_open_orders[-1][0]["clientOrderId"] == "ALPHORA-external"
    finally:
        governor.snapshot_provider.close()


def test_governor_collects_snapshot_health_on_cycle_end_and_heartbeat(
    config_factory,
    monkeypatch,
):
    governor = _governor(config_factory)
    labels = []

    def collect(label):
        labels.append(label)
        return {"available": True}

    monkeypatch.setattr(governor.snapshot_provider, "collect_health", collect)

    try:
        governor.run_cycle(now=time.time())
        governor._wait_until_next_cycle()

        assert "cycle_end" in labels
        assert "heartbeat" in labels
    finally:
        governor.snapshot_provider.close()


def test_mark_price_beyond_short_stop_blocks_before_execution(
    config_factory,
    monkeypatch,
):
    governor = _governor(
        config_factory,
        execution_mode=ExecutionMode.TESTNET_LIVE,
    )
    execution = FakeExecutionEngine()
    governor.execution_engine = execution
    governor.strategy_coordinator = FakeStrategyCoordinator(
        decision=TradeDecision.open_short("short protection probe")
    )
    monkeypatch.setattr(
        governor.client,
        "mark_price",
        lambda _symbol, *, timeout=None: {"markPrice": "102"},
    )

    try:
        governor.run_cycle(now=time.time())

        assert execution.executed == []
        blocked = [
            payload
            for event_type, payload in governor.events_journal.events
            if event_type == "trade_blocked_by_pre_execution_guard"
        ]
        assert blocked
        assert blocked[-1]["reason"] == "PROTECTION_TRIGGER_ALREADY_BREACHED_PRE_ENTRY"
        protection = blocked[-1]["diagnostics"]["pre_entry_protection"]
        assert protection["market_price"] == Decimal("100")
        assert protection["mark_price"] == Decimal("102")
        assert protection["stop_price"] == Decimal("101.00")
        assert protection["breached_triggers"] == ["STOP_MARKET"]
        assert not any(
            event_type == "command_dispatched"
            for event_type, _payload in governor.events_journal.events
        )
    finally:
        governor.snapshot_provider.close()


def test_pre_dispatch_soft_drift_rebuilds_plan_with_current_price(
    config_factory,
    monkeypatch,
):
    governor = _governor(config_factory)
    execution = FakeExecutionEngine()
    governor.execution_engine = execution
    original = ExecutionRevalidation.revalidate

    def revalidate_with_price_move(revalidation, evaluation):
        result = original(revalidation, evaluation)
        evaluation.heavy = replace(
            evaluation.heavy,
            market=replace(evaluation.heavy.market, price=Decimal("100.20")),
            mark_price=Decimal("100.20"),
        )
        return result

    monkeypatch.setattr(
        ExecutionRevalidation,
        "revalidate",
        revalidate_with_price_move,
    )

    try:
        governor.run_cycle(now=time.time())

        assert execution.executed == [("BTCUSDT", "OPEN_LONG")]
        started = [
            payload
            for event_type, payload in governor.events_journal.events
            if event_type == "pre_dispatch_replan_started"
        ]
        completed = [
            payload
            for event_type, payload in governor.events_journal.events
            if event_type == "pre_dispatch_replan_completed"
        ]
        assert started and completed
        assert started[-1]["price_drift_bps"] == Decimal("20.00")
        assert completed[-1]["trade_plan_id"] != completed[-1]["supersedes_trade_plan_id"]
        assert execution.execution_kwargs[-1]["planned_stop_price"] is None
        assert execution.execution_kwargs[-1]["planned_take_profit_price"] is None
        dispatched = [
            payload
            for event_type, payload in governor.events_journal.events
            if event_type == "command_dispatched"
        ]
        assert dispatched[-1]["effective_stop_price"] == Decimal("99.10")
        assert dispatched[-1]["effective_take_profit_price"] == Decimal("102.30")
    finally:
        governor.snapshot_provider.close()


def test_pre_dispatch_hard_drift_blocks_before_execution(
    config_factory,
    monkeypatch,
):
    governor = _governor(config_factory)
    execution = FakeExecutionEngine()
    governor.execution_engine = execution
    original = ExecutionRevalidation.revalidate

    def revalidate_with_large_price_move(revalidation, evaluation):
        result = original(revalidation, evaluation)
        evaluation.heavy = replace(
            evaluation.heavy,
            market=replace(evaluation.heavy.market, price=Decimal("101")),
        )
        return result

    monkeypatch.setattr(
        ExecutionRevalidation,
        "revalidate",
        revalidate_with_large_price_move,
    )

    try:
        governor.run_cycle(now=time.time())

        assert execution.executed == []
        blocked = [
            payload
            for event_type, payload in governor.events_journal.events
            if event_type == "governor_evaluate_symbol_flow_decision"
            and payload.get("reason") == "PRE_DISPATCH_PRICE_DRIFT_TOO_HIGH"
        ]
        assert blocked
        assert not any(
            event_type == "command_dispatched"
            for event_type, _payload in governor.events_journal.events
        )
    finally:
        governor.snapshot_provider.close()
