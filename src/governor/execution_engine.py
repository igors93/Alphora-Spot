from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from time import time
from uuid import uuid4

from binance.futures_client import BinanceFuturesClient
from config import AppConfig, TradingSymbolConfig
from config.symbol_context import SymbolTradingConfig
from domain.models import FuturesPosition, PositionDirection, TradeAction
from execution.coordinator import ExecutableTradeLike, ExecutionCoordinator
from execution.engine import FuturesTradingEngine
from execution.exchange_config import configure_margin_leverage_safely
from execution.simulated_broker import SimulatedBroker
from execution.state_reconciliation import StateReconciliationEngine
from execution.trade_audit import TradeAuditRecorder
from futures.exchange_rules import extract_symbol_rules
from futures.performance.cost_model import CostModel
from futures.planned_exit import GLOBAL_FALLBACK_EXIT_SOURCE, STRATEGY_METADATA_EXIT_SOURCE
from futures.position_execution import (
    PositionChangeState,
    PositionExecutionResult,
    PositionExecutionStatus,
    PositionRecoveryStatus,
    freeze_mapping,
)
from futures.process_lock import ProcessLock
from futures.protection import ExpectedProtection
from futures.risk_guard import FuturesRiskGuard
from futures.risk_manager import FuturesRiskManager, TradeNotExecutableError
from governor.adaptive_adjustment_plan import (
    PLAN_STATUS_SHADOW_READY,
    AdaptiveAdjustmentPlan,
)
from governor.market_data_router import HeavyMarketSnapshot
from market_data.market_snapshots import extract_positions_from_risk
from trading.intent import TradeIntent
from trading.outcome import build_trade_plan_outcome_from_trade_result
from utils.journals import JournalRegistry
from utils.logger import get_logger
from utils.trade_journal import TradeJournal


def _new_execution_command_id() -> str:
    return f"ec_{uuid4().hex}"


def _validate_open_exit_prices(
    *,
    action: TradeAction,
    market_price: Decimal,
    stop_price: Decimal | None,
    take_profit_price: Decimal | None,
) -> None:
    if stop_price is not None:
        stop_price = Decimal(str(stop_price))
    if take_profit_price is not None:
        take_profit_price = Decimal(str(take_profit_price))
    if action is TradeAction.OPEN_LONG:
        if stop_price is not None and stop_price >= market_price:
            raise ValueError("Long stop price must be below market price")
        if take_profit_price is not None and take_profit_price <= market_price:
            raise ValueError("Long take-profit price must be above market price")
    if action is TradeAction.OPEN_SHORT:
        if stop_price is not None and stop_price <= market_price:
            raise ValueError("Short stop price must be above market price")
        if take_profit_price is not None and take_profit_price >= market_price:
            raise ValueError("Short take-profit price must be below market price")


@dataclass(frozen=True)
class ExecutionCommand:
    command_id: str
    intent_id: str
    symbol: str
    action: TradeAction
    quantity: Decimal | None
    market_price: Decimal
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    planned_exit_source: str | None
    trade_plan_id: str | None
    reason: str
    protection_plan: ExpectedProtection | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        action = TradeAction(self.action)
        symbol = str(self.symbol or "").upper().strip()
        if not symbol:
            raise ValueError("ExecutionCommand symbol is required")
        market_price = Decimal(str(self.market_price))
        if market_price <= 0:
            raise ValueError("ExecutionCommand market_price must be positive")
        quantity = Decimal(str(self.quantity)) if self.quantity is not None else None
        stop_price = Decimal(str(self.stop_price)) if self.stop_price is not None else None
        take_profit_price = (
            Decimal(str(self.take_profit_price)) if self.take_profit_price is not None else None
        )
        if action in {TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT}:
            if quantity is not None and quantity <= 0:
                raise ValueError("ExecutionCommand quantity must be positive for openings")
            _validate_open_exit_prices(
                action=action,
                market_price=market_price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
            )

        object.__setattr__(self, "action", action)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "market_price", market_price)
        object.__setattr__(self, "stop_price", stop_price)
        if self.protection_plan is not None:
            if self.protection_plan.stop_price <= 0 or self.protection_plan.take_profit_price <= 0:
                raise ValueError("ExecutionCommand protection prices must be positive")
            if action in {TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT}:
                _validate_open_exit_prices(
                    action=action,
                    market_price=market_price,
                    stop_price=self.protection_plan.stop_price,
                    take_profit_price=self.protection_plan.take_profit_price,
                )
        object.__setattr__(self, "take_profit_price", take_profit_price)
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))

    @classmethod
    def from_intent(
        cls,
        *,
        intent: TradeIntent,
        heavy_snapshot: HeavyMarketSnapshot,
        planned_quantity: Decimal | None = None,
        trade_plan_id: str | None = None,
        planned_stop_price: Decimal | None = None,
        planned_take_profit_price: Decimal | None = None,
        planned_exit_source: str | None = None,
        protection_plan: ExpectedProtection | None = None,
    ) -> "ExecutionCommand":
        market_price = (
            Decimal(str(intent.market_price))
            if intent.market_price is not None
            else heavy_snapshot.market.price
        )
        return cls(
            command_id=_new_execution_command_id(),
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            action=TradeAction(intent.action),
            quantity=planned_quantity,
            market_price=market_price,
            stop_price=planned_stop_price,
            take_profit_price=planned_take_profit_price,
            planned_exit_source=planned_exit_source,
            trade_plan_id=trade_plan_id,
            reason=intent.reason,
            protection_plan=protection_plan,
            metadata={
                "strategy_name": intent.strategy_name,
            },
        )


class ExecutionStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    DRY_RUN = "DRY_RUN"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    IGNORED = "IGNORED"
    UNCERTAIN = "UNCERTAIN"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ExecutionResult:
    command_id: str
    status: ExecutionStatus
    symbol: str
    action: TradeAction
    order_ids: tuple[str, ...]
    position_changed: bool
    reason: str
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    position_state: PositionChangeState = PositionChangeState.UNCHANGED
    recovery_status: PositionRecoveryStatus = PositionRecoveryStatus.NONE

    def __post_init__(self) -> None:
        status = ExecutionStatus(self.status)
        action = TradeAction(self.action)
        symbol = str(self.symbol or "").upper().strip()
        if not symbol:
            raise ValueError("ExecutionResult symbol is required")
        order_ids = tuple(
            str(order_id)
            for order_id in self.order_ids
            if order_id is not None and str(order_id).strip()
        )
        position_state = PositionChangeState(self.position_state)
        recovery_status = PositionRecoveryStatus(self.recovery_status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "order_ids", order_ids)
        object.__setattr__(self, "reason", str(self.reason or status.value))
        object.__setattr__(self, "diagnostics", freeze_mapping(self.diagnostics))
        object.__setattr__(self, "position_state", position_state)
        object.__setattr__(self, "recovery_status", recovery_status)

        if status is ExecutionStatus.CONFIRMED:
            if not self.position_changed:
                raise ValueError("CONFIRMED execution requires position_changed=True")
            return
        if self.position_changed:
            raise ValueError(f"{status.value} execution cannot change position")


_POSITION_TO_EXECUTION_STATUS = {
    PositionExecutionStatus.CONFIRMED: ExecutionStatus.CONFIRMED,
    PositionExecutionStatus.DRY_RUN: ExecutionStatus.DRY_RUN,
    PositionExecutionStatus.BLOCKED: ExecutionStatus.BLOCKED,
    PositionExecutionStatus.SKIPPED: ExecutionStatus.SKIPPED,
    PositionExecutionStatus.UNCERTAIN: ExecutionStatus.UNCERTAIN,
    PositionExecutionStatus.FAILED: ExecutionStatus.FAILED,
}


def _execution_result_from_position_result(
    *,
    command: ExecutionCommand,
    symbol: str,
    action: TradeAction,
    result: PositionExecutionResult,
) -> ExecutionResult:
    diagnostics = {
        "intent_id": command.intent_id,
        "trade_plan_id": command.trade_plan_id,
        "position_execution_status": result.status.value,
        "position_execution_reason": result.reason,
        "position_state": result.position_state.value,
        "recovery_status": result.recovery_status.value,
        **dict(result.diagnostics),
    }
    return ExecutionResult(
        command_id=command.command_id,
        status=_POSITION_TO_EXECUTION_STATUS[result.status],
        symbol=symbol,
        action=action,
        order_ids=result.order_ids,
        position_changed=result.position_changed,
        reason=result.reason,
        diagnostics=diagnostics,
        position_state=result.position_state,
        recovery_status=result.recovery_status,
    )


def _filter_exchange_info_symbols(payload: dict, keep: set[str]) -> dict:
    """Drop exchangeInfo symbol entries outside `keep`; empty set keeps all."""
    if not keep:
        return payload
    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        return payload
    slim = dict(payload)
    slim["symbols"] = [
        entry
        for entry in symbols
        if isinstance(entry, dict) and str(entry.get("symbol", "")).upper().strip() in keep
    ]
    return slim


class ExecutionEngine:
    """Per-symbol execution facade over the existing single-symbol engine."""

    def __init__(
        self,
        *,
        base_config: AppConfig,
        client: BinanceFuturesClient,
        operational_mode: str | None = None,
        exchange_info_provider: Callable[[], dict] | None = None,
        governor_journal: TradeJournal | None = None,
        trade_audit_recorder: TradeAuditRecorder | None = None,
        state_reconciliation_engine: StateReconciliationEngine | None = None,
        journals: JournalRegistry | None = None,
    ):
        self.base_config = base_config
        self.journals = journals or JournalRegistry()
        self.client = client
        self.operational_mode = operational_mode or _operational_mode_from_config(base_config)
        self.exchange_info_provider = exchange_info_provider
        self._engines: dict[str, FuturesTradingEngine] = {}
        self._bootstrapped: set[str] = set()
        self._locks: dict[str, ProcessLock] = {}
        self._fallback_exchange_info_cache: dict | None = None
        self._fallback_exchange_info_symbols: set[str] = set()
        self._risk_guards: dict[str, FuturesRiskGuard] = {}
        self.logger = get_logger(__name__)
        data_dir = Path(base_config.trade_journal_path).parent
        self.governor_journal = governor_journal or self.journals.journal(
            data_dir / "governor_events.jsonl"
        )
        self.trade_audit_recorder = trade_audit_recorder or TradeAuditRecorder.from_config(
            base_config, operational_mode=self.operational_mode, journals=self.journals
        )
        self.state_reconciliation_engine = (
            state_reconciliation_engine or StateReconciliationEngine()
        )
        self.coordinator = ExecutionCoordinator[
            ExecutionCommand, HeavyMarketSnapshot, ExecutionResult
        ](
            open_trade=self._execute_open,
            close_trade=self._execute_close,
            ignore_trade=self._execute_ignored,
        )

    def acquire_symbol_locks(self, symbol_configs: list[SymbolTradingConfig]) -> None:
        if not self.base_config.lock_enabled:
            return
        for symbol_config in symbol_configs:
            lock = ProcessLock(
                runtime_dir=symbol_config.runtime_dir,
                symbol=symbol_config.symbol,
                environment=self.base_config.environment_key,
            )
            lock.acquire()
            self._locks[symbol_config.symbol] = lock

    def release_symbol_locks(self) -> None:
        for lock in list(self._locks.values()):
            lock.release()
        self._locks.clear()

    def recover_symbol(self, symbol_config: SymbolTradingConfig) -> None:
        engine = self._get_engine(symbol_config)
        self._ensure_symbol_rules(engine, symbol_config)
        engine.recover()

    def configure_symbol_exchange_safely(self, symbol_config: SymbolTradingConfig) -> None:
        engine = self._get_engine(symbol_config)
        self._ensure_symbol_rules(engine, symbol_config)

        if self.base_config.mutations_enabled:
            configured = configure_margin_leverage_safely(
                client=self.client,
                symbol=symbol_config.symbol,
                margin_type=symbol_config.margin_type.value,
                leverage=symbol_config.leverage,
                logger=self.logger,
                record_event=self.governor_journal.append,
            )
        else:
            configured = False

        payload = {
            "symbol": symbol_config.symbol,
            "margin_type": symbol_config.margin_type.value,
            "leverage": symbol_config.leverage,
            "execution_mode": self.base_config.execution_mode.value,
            "environment": self.base_config.environment.value,
            "configured": configured,
        }
        event_type = (
            "symbol_exchange_configured" if configured else "symbol_exchange_config_skipped"
        )
        self.governor_journal.append(event_type, **payload)
        engine.record_event(event_type, **payload)

    def _ensure_symbol_rules(
        self,
        engine: FuturesTradingEngine,
        symbol_config: SymbolTradingConfig,
    ) -> None:
        if engine.rules is not None:
            return
        exchange_info = self._exchange_info(symbol=symbol_config.symbol)
        engine.rules = extract_symbol_rules(exchange_info, symbol_config.symbol)

    def _exchange_info(self, *, symbol: str | None = None) -> dict:
        if self.exchange_info_provider is not None:
            return self.exchange_info_provider()

        normalized = str(symbol or "").upper().strip()
        cached = self._fallback_exchange_info_cache
        if cached is not None and (
            not normalized or normalized in self._fallback_exchange_info_symbols
        ):
            return cached

        # The full exchangeInfo payload lists every exchange symbol (several
        # MB parsed); keep only the entries this engine was asked about.
        if normalized:
            self._fallback_exchange_info_symbols.add(normalized)
        payload = self.client.exchange_info()
        self._fallback_exchange_info_cache = _filter_exchange_info_symbols(
            payload,
            self._fallback_exchange_info_symbols,
        )
        return self._fallback_exchange_info_cache

    def get_position(self, symbol_config: SymbolTradingConfig) -> FuturesPosition:
        engine = self._get_engine(symbol_config)
        if self.base_config.execution_mode.is_paper and engine.paper_broker is not None:
            return engine.paper_broker.current_position()
        position = engine.get_position()
        return self.sync_position_state(symbol_config, position)

    def get_positions(
        self,
        symbol_configs: list[SymbolTradingConfig],
    ) -> dict[str, FuturesPosition]:
        """Return the effective position view with at most one Binance read.

        PAPER positions remain local to each simulated broker. Live/testnet positions are loaded from
        the global position-risk endpoint once and synchronized into each symbol engine.
        """

        if self.base_config.execution_mode.is_paper:
            return {item.symbol: self.get_position(item) for item in symbol_configs}

        raw_positions = self.client.position_risk(None)
        positions = extract_positions_from_risk(raw_positions)
        resolved: dict[str, FuturesPosition] = {}
        for item in symbol_configs:
            current = positions.get(item.symbol, empty_position(item.symbol))
            resolved[item.symbol] = self.sync_position_state(item, current)
        return resolved

    def sync_position_state(
        self,
        symbol_config: SymbolTradingConfig,
        position: FuturesPosition,
    ) -> FuturesPosition:
        engine = self._get_engine(symbol_config)
        return engine.sync_position_state(position)

    def get_open_orders(self, symbol_config: SymbolTradingConfig) -> dict[str, list[dict]]:
        engine = self._get_engine(symbol_config)
        normal = engine.get_open_orders()
        algo = engine.get_open_algo_orders()
        return {
            "normal": normal.orders if normal.ok else [],
            "algo": algo.orders if algo.ok else [],
        }

    def get_runtime_state(self, symbol_config: SymbolTradingConfig):
        return self._get_engine(symbol_config).get_runtime_state()

    def apply_adaptive_adjustment_plan(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        adjustment_plan: AdaptiveAdjustmentPlan,
    ) -> dict:
        engine = self._get_engine(symbol_config)
        base_payload = {
            "symbol": symbol_config.symbol,
            "plan_id": adjustment_plan.plan_id,
            "plan_action": adjustment_plan.action,
            "plan_status": adjustment_plan.status,
            "trade_plan_id": adjustment_plan.trade_plan_id,
            "requires_order_change": adjustment_plan.requires_order_change,
            "execution_enabled": adjustment_plan.execution_enabled,
            "execution_mode": self.base_config.execution_mode.value,
        }

        if not self.base_config.adaptive_execution_enabled:
            return self._record_adaptive_adjustment_result(
                engine,
                event_type="adaptive_adjustment_execution_skipped",
                status="SKIPPED",
                reason="ADAPTIVE_EXECUTION_DISABLED",
                payload=base_payload,
            )

        if not adjustment_plan.execution_enabled:
            return self._record_adaptive_adjustment_result(
                engine,
                event_type="adaptive_adjustment_execution_skipped",
                status="SKIPPED",
                reason="PLAN_EXECUTION_DISABLED",
                payload=base_payload,
            )

        if not self.base_config.dynamic_exit_enabled:
            return self._record_adaptive_adjustment_result(
                engine,
                event_type="adaptive_adjustment_execution_skipped",
                status="SKIPPED",
                reason="DYNAMIC_EXIT_DISABLED",
                payload=base_payload,
            )

        if adjustment_plan.status != PLAN_STATUS_SHADOW_READY:
            return self._record_adaptive_adjustment_result(
                engine,
                event_type="adaptive_adjustment_execution_skipped",
                status="SKIPPED",
                reason="PLAN_NOT_READY",
                payload=base_payload,
            )

        if not adjustment_plan.requires_order_change:
            return self._record_adaptive_adjustment_result(
                engine,
                event_type="adaptive_adjustment_execution_skipped",
                status="SKIPPED",
                reason="NO_ORDER_CHANGE_REQUIRED",
                payload=base_payload,
            )

        if not self.base_config.execution_mode.is_paper:
            return self._record_adaptive_adjustment_result(
                engine,
                event_type="adaptive_adjustment_execution_blocked",
                status="BLOCKED",
                reason="LIVE_ADAPTIVE_EXECUTION_NOT_SUPPORTED",
                payload=base_payload,
            )

        return self._paper_apply_adaptive_adjustment_plan(
            engine,
            adjustment_plan=adjustment_plan,
            payload=base_payload,
        )

    def _record_adaptive_adjustment_result(
        self,
        engine: FuturesTradingEngine,
        *,
        event_type: str,
        status: str,
        reason: str,
        payload: dict,
    ) -> dict:
        result = {
            **payload,
            "status": status,
            "reason": reason,
        }
        self.governor_journal.append(event_type, **result)
        engine.record_event(event_type, **result)
        return result

    def _paper_apply_adaptive_adjustment_plan(
        self,
        engine: FuturesTradingEngine,
        *,
        adjustment_plan: AdaptiveAdjustmentPlan,
        payload: dict,
    ) -> dict:
        if engine.paper_broker is None or not engine.paper_broker.current_position().is_open:
            return self._record_adaptive_adjustment_result(
                engine,
                event_type="adaptive_adjustment_execution_skipped",
                status="SKIPPED",
                reason="PAPER_POSITION_NOT_OPEN",
                payload=payload,
            )

        updated_position = engine.paper_broker.adjust_protection(
            stop_price=adjustment_plan.target_stop_price,
            take_profit_price=adjustment_plan.target_take_profit_price,
            current_price=adjustment_plan.current_price,
            adjustment_plan_id=adjustment_plan.plan_id,
            reason=adjustment_plan.reason,
        )
        result = {
            **payload,
            "status": "APPLIED",
            "reason": adjustment_plan.reason,
            "target_stop_price": adjustment_plan.target_stop_price,
            "target_take_profit_price": adjustment_plan.target_take_profit_price,
            "updated_stop_price": updated_position.stop_price,
            "updated_take_profit_price": updated_position.take_profit_price,
        }
        self.governor_journal.append("adaptive_adjustment_execution_applied", **result)
        engine.paper_journal.append("paper_adaptive_adjustment_applied", **result)
        engine.record_event("adaptive_adjustment_execution_applied", **result)
        return result

    def execute(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        command: ExecutionCommand,
    ) -> ExecutionResult:
        return self.coordinator.execute(
            symbol_config=symbol_config,
            heavy_snapshot=heavy_snapshot,
            command=command,
        )

    def execute_trade(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        trade: ExecutableTradeLike[ExecutionCommand],
    ) -> ExecutionResult:
        return self.coordinator.execute_trade(
            symbol_config=symbol_config,
            heavy_snapshot=heavy_snapshot,
            trade=trade,
        )

    def _execute_open(
        self,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        command: ExecutionCommand,
    ) -> ExecutionResult:
        action = TradeAction(command.action)
        position_result = self.open_position(
            symbol_config=symbol_config,
            action=action,
            price=command.market_price,
            reason=command.reason,
            heavy_snapshot=heavy_snapshot,
            planned_quantity=command.quantity,
            trade_plan_id=command.trade_plan_id,
            planned_stop_price=command.stop_price,
            planned_take_profit_price=command.take_profit_price,
            planned_exit_source=command.planned_exit_source,
            prevalidated_protection=command.protection_plan,
        )
        return _execution_result_from_position_result(
            command=command,
            symbol=symbol_config.symbol,
            action=action,
            result=position_result,
        )

    def _execute_close(
        self,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        command: ExecutionCommand,
    ) -> ExecutionResult:
        action = TradeAction(command.action)
        position_result = self.close_position(
            symbol_config=symbol_config,
            position=heavy_snapshot.position,
            reason=command.reason,
            price=command.market_price,
        )
        return _execution_result_from_position_result(
            command=command,
            symbol=symbol_config.symbol,
            action=action,
            result=position_result,
        )

    @staticmethod
    def _execute_ignored(
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        command: ExecutionCommand,
    ) -> ExecutionResult:
        action = TradeAction(command.action)
        return ExecutionResult(
            command_id=command.command_id,
            status=ExecutionStatus.IGNORED,
            symbol=symbol_config.symbol,
            action=action,
            order_ids=(),
            position_changed=False,
            reason="ACTION_NOT_EXECUTABLE",
            diagnostics={},
        )

    def open_position(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: TradeAction,
        price: Decimal,
        reason: str,
        heavy_snapshot: HeavyMarketSnapshot,
        planned_quantity: Decimal | None = None,
        trade_plan_id: str | None = None,
        planned_stop_price: Decimal | None = None,
        planned_take_profit_price: Decimal | None = None,
        planned_exit_source: str | None = None,
        prevalidated_protection: ExpectedProtection | None = None,
    ) -> PositionExecutionResult:
        engine = self._get_engine(symbol_config)
        engine.rules = heavy_snapshot.exchange_rules
        if self.base_config.execution_mode.is_paper:
            return self._paper_open_position(
                engine,
                action=action,
                price=price,
                reason=reason,
                planned_quantity=planned_quantity,
                trade_plan_id=trade_plan_id,
                planned_stop_price=planned_stop_price,
                planned_take_profit_price=planned_take_profit_price,
                planned_exit_source=planned_exit_source,
            )

        return engine.open_position(
            action,
            price,
            reason,
            planned_quantity=planned_quantity,
            trade_plan_id=trade_plan_id,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
            planned_exit_source=planned_exit_source,
            prevalidated_protection=prevalidated_protection,
        )

    def close_position(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        position: FuturesPosition,
        reason: str,
        price: Decimal | None = None,
    ) -> PositionExecutionResult:
        engine = self._get_engine(symbol_config)
        runtime_state = getattr(engine, "runtime_state", None)
        if self.base_config.execution_mode.is_paper:
            close_price = price if price is not None else self._current_price(engine, position)
            result = self._paper_close_position(engine, price=close_price, reason=reason)
            if result.position_changed:
                self.trade_audit_recorder.record_close(
                    symbol=symbol_config.symbol,
                    trade_id=(runtime_state.active_trade_id if runtime_state is not None else None),
                    trade_plan_id=(
                        runtime_state.trade_plan_id if runtime_state is not None else None
                    ),
                    exit_time=str(time()),
                    exit_reason=reason,
                    entry_price=position.entry_price,
                    quantity=position.quantity,
                    direction=position.direction.value,
                    diagnostics={
                        "price": close_price,
                        "runtime_state_available": runtime_state is not None,
                        "execution_result": result,
                    },
                )
                self._record_close_position_confirmed(
                    symbol_config=symbol_config,
                    position=position,
                    reason=reason,
                    price=close_price,
                    execution_result=result,
                )
            return result

        result = engine.close_position(position, reason)
        if result.position_changed:
            closed_position = result.diagnostics.get("closed_position")
            if not isinstance(closed_position, FuturesPosition):
                closed_position = position
            self.trade_audit_recorder.record_close(
                symbol=symbol_config.symbol,
                trade_id=(runtime_state.active_trade_id if runtime_state is not None else None),
                trade_plan_id=(runtime_state.trade_plan_id if runtime_state is not None else None),
                exit_time=str(time()),
                exit_reason=reason,
                entry_price=closed_position.entry_price,
                quantity=closed_position.quantity,
                direction=closed_position.direction.value,
                diagnostics={
                    "price": price,
                    "runtime_state_available": runtime_state is not None,
                    "execution_result": result,
                },
            )
            self._record_close_position_confirmed(
                symbol_config=symbol_config,
                position=closed_position,
                reason=reason,
                price=price,
                execution_result=result,
            )
        return result

    def _record_close_position_confirmed(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        position: FuturesPosition,
        reason: str,
        price: Decimal | None,
        execution_result: PositionExecutionResult,
    ) -> None:
        self.governor_journal.append(
            "close_position_confirmed",
            symbol=symbol_config.symbol,
            action="CLOSE_POSITION",
            reason=reason,
            close_source="EXECUTION_ENGINE",
            status=execution_result.status.value,
            position_changed=execution_result.position_changed,
            order_ids=execution_result.order_ids,
            diagnostics=execution_result.diagnostics,
            seconds_since_open=_seconds_since_open(position),
            position_direction=getattr(position.direction, "value", None),
            entry_price=position.entry_price,
            exit_price=price,
            quantity=position.quantity,
        )

    def audit_symbol_protections(
        self,
        symbol_config: SymbolTradingConfig,
        *,
        position: FuturesPosition | None = None,
    ) -> bool:
        engine = self._get_engine(symbol_config)
        position = position or self.get_position(symbol_config)
        if position.is_open and self.base_config.execution_mode.is_paper:
            # PAPER has no exchange-side STOP/TAKE orders: the simulated
            # protections must be triggered here, every cycle.
            if self._enforce_paper_protections(engine):
                return self.get_position(symbol_config).is_open
            return position.is_open
        if position.is_open and bool(getattr(self.client, "spot_mode", False)):
            if self._enforce_spot_protections(symbol_config, engine, position):
                return self.get_position(symbol_config).is_open
        if position.is_open:
            engine.audit_protections(position, reason="governor_cycle")
        return position.is_open

    def _enforce_spot_protections(
        self,
        symbol_config: SymbolTradingConfig,
        engine: FuturesTradingEngine,
        position: FuturesPosition,
    ) -> bool:
        """Close a live/testnet Spot position when its virtual stop/take is crossed."""
        runtime_state = getattr(engine, "runtime_state", None)
        stop_raw = getattr(runtime_state, "planned_stop_price", None) if runtime_state else None
        take_raw = (
            getattr(runtime_state, "planned_take_profit_price", None) if runtime_state else None
        )
        if not stop_raw and not take_raw:
            return False

        try:
            trigger_price = Decimal(str(engine.get_trigger_reference_price()))
        except Exception as error:
            self.logger.warning(
                "Spot protection check skipped: price unavailable | symbol=%s | error=%s",
                engine.config.symbol,
                error,
            )
            return False
        if trigger_price <= 0:
            return False

        stop_price = Decimal(str(stop_raw)) if stop_raw else None
        take_price = Decimal(str(take_raw)) if take_raw else None

        stop_hit = stop_price is not None and stop_price > 0 and trigger_price <= stop_price
        take_hit = take_price is not None and take_price > 0 and trigger_price >= take_price
        if not stop_hit and not take_hit:
            return False

        exit_reason = "spot_stop_loss" if stop_hit else "spot_take_profit"
        result = self.close_position(
            symbol_config=symbol_config,
            position=position,
            reason=f"{exit_reason}: trigger_price={trigger_price}",
            price=trigger_price,
        )
        engine.record_event(
            "spot_protection_triggered",
            symbol=engine.config.symbol,
            exit_reason=exit_reason,
            trigger_price=trigger_price,
            stop_price=stop_price,
            take_profit_price=take_price,
            status=result.status.value,
        )
        return bool(result.position_changed)

    def _enforce_paper_protections(self, engine: FuturesTradingEngine) -> bool:
        """Close a paper position whose simulated stop/take was crossed.

        Returns True when the position was closed. Stops fill at the observed
        price (never better than the stop); takes fill at the take price
        (never better than the target), mirroring the backtest's conservative
        fills.
        """
        broker = engine.paper_broker
        paper_position = broker.position if broker is not None else None
        if paper_position is None:
            return False

        try:
            trigger_price = Decimal(str(engine.get_trigger_reference_price()))
        except Exception as error:  # a price read failure must not fault the symbol
            self.logger.warning(
                "Paper protection check skipped: price unavailable | symbol=%s | error=%s",
                engine.config.symbol,
                error,
            )
            return False
        if trigger_price <= 0:
            return False

        stop_price = paper_position.stop_price
        take_price = paper_position.take_profit_price
        if paper_position.direction == PositionDirection.LONG:
            stop_hit = trigger_price <= stop_price
            take_hit = trigger_price >= take_price
            stop_fill = min(trigger_price, stop_price)
        elif paper_position.direction == PositionDirection.SHORT:
            stop_hit = trigger_price >= stop_price
            take_hit = trigger_price <= take_price
            stop_fill = max(trigger_price, stop_price)
        else:
            return False

        if not stop_hit and not take_hit:
            return False

        if stop_hit:
            fill_price = stop_fill
            exit_reason = "paper_stop_loss"
        else:
            fill_price = take_price
            exit_reason = "paper_take_profit"

        result = self._paper_close_position(
            engine,
            price=fill_price,
            reason=f"{exit_reason}: trigger_price={trigger_price}",
            exit_reason=exit_reason,
        )
        engine.record_event(
            "paper_protection_triggered",
            symbol=engine.config.symbol,
            exit_reason=exit_reason,
            trigger_price=trigger_price,
            fill_price=fill_price,
            stop_price=stop_price,
            take_profit_price=take_price,
            status=result.status.value,
        )
        return bool(result.position_changed)

    def cancel_robot_orders(self, symbol_config: SymbolTradingConfig) -> bool:
        engine = self._get_engine(symbol_config)
        return engine.cancel_robot_orders(reason="governor_cancel_robot_orders")

    def emergency_flatten(self, symbol_config: SymbolTradingConfig) -> None:
        self.emergency_flatten_symbol(symbol_config)

    def emergency_flatten_symbol(self, symbol_config: SymbolTradingConfig) -> None:
        position = self.get_position(symbol_config)
        if position.is_open:
            self.close_position(
                symbol_config=symbol_config,
                position=position,
                reason="governor_emergency_flatten",
            )

    def risk_guard_for_symbol(self, symbol_config: SymbolTradingConfig) -> FuturesRiskGuard:
        guard = self._risk_guards.get(symbol_config.symbol)
        if guard is None:
            guard = FuturesRiskGuard(
                self.build_symbol_app_config(symbol_config),
                extra_journal_paths=self._risk_guard_extra_journal_paths(symbol_config),
            )
            self._risk_guards[symbol_config.symbol] = guard
        return guard

    def _paper_journal_path(self, symbol_config: SymbolTradingConfig) -> Path:
        return Path(symbol_config.journal_path).parent / (
            f"paper_trades_{symbol_config.symbol.lower()}_{self.base_config.environment_key}.jsonl"
        )

    def _risk_guard_extra_journal_paths(
        self, symbol_config: SymbolTradingConfig
    ) -> tuple[Path, ...]:
        # Paper fills are journaled apart from the live journal; the daily
        # limits and the anti-HFT entry spacing must see them too.
        if self.base_config.execution_mode.is_paper:
            return (self._paper_journal_path(symbol_config),)
        return ()

    def _current_price(
        self,
        engine: FuturesTradingEngine,
        position: FuturesPosition,
    ) -> Decimal:
        from binance.futures_client import (
            BinanceFuturesAPIError,
            BinanceFuturesNetworkError,
        )

        try:
            return engine.get_current_price()
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as error:
            self.logger.warning(
                "Could not fetch current price for paper close | symbol=%s | error=%s | "
                "falling back to entry_price",
                engine.config.symbol,
                error,
            )
            return position.entry_price if position.entry_price > 0 else Decimal("0")

    def _paper_open_position(
        self,
        engine: FuturesTradingEngine,
        *,
        action: TradeAction,
        price: Decimal,
        reason: str,
        planned_quantity: Decimal | None = None,
        trade_plan_id: str | None = None,
        planned_stop_price: Decimal | None = None,
        planned_take_profit_price: Decimal | None = None,
        planned_exit_source: str | None = None,
    ) -> PositionExecutionResult:
        if engine.rules is None:
            return PositionExecutionResult.failed(
                "SYMBOL_RULES_NOT_LOADED",
                diagnostics={"symbol": engine.config.symbol},
            )

        if engine.paper_broker is None:
            engine.paper_broker = SimulatedBroker(
                symbol=engine.config.symbol,
                risk_manager=engine.risk_manager,
                symbol_rules=engine.rules,
                cost_model=_paper_cost_model(engine.config),
                exit_model=engine.exit_model,
            )

        position = engine.paper_broker.current_position()
        if position.is_open:
            engine.record_event("paper_open_skipped_position_exists", symbol=engine.config.symbol)
            return PositionExecutionResult.skipped(
                "PAPER_POSITION_ALREADY_OPEN",
                diagnostics={"symbol": engine.config.symbol, "position": position},
            )

        try:
            quantity, quantity_source = engine.select_entry_quantity(
                price=price,
                planned_quantity=planned_quantity,
                trade_plan_id=trade_plan_id,
            )
        except TradeNotExecutableError as error:
            engine.record_event(
                "paper_open_blocked",
                reason="planned_quantity_invalid",
                symbol=engine.config.symbol,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                error=str(error),
                calculation=error.calculation,
            )
            return PositionExecutionResult.blocked(
                "PLANNED_QUANTITY_INVALID",
                diagnostics={
                    "symbol": engine.config.symbol,
                    "trade_plan_id": trade_plan_id,
                    "planned_quantity": planned_quantity,
                    "error": str(error),
                    "calculation": error.calculation,
                },
            )

        notional = quantity * price
        guard_decision = engine.risk_guard.can_open_position(notional=notional)
        if not guard_decision.allowed:
            engine.record_event(
                "trade_blocked_by_symbol_risk",
                reason=guard_decision.reason,
                metrics=guard_decision.metrics,
                symbol=engine.config.symbol,
                execution_mode=engine.config.execution_mode.value,
                notional=notional,
            )
            return PositionExecutionResult.blocked(
                str(guard_decision.reason or "RISK_GUARD_BLOCKED"),
                diagnostics={
                    "symbol": engine.config.symbol,
                    "metrics": guard_decision.metrics,
                    "notional": notional,
                    "execution_mode": engine.config.execution_mode.value,
                },
            )

        planned_exit_applied = (
            engine.config.dynamic_exit_enabled
            and planned_stop_price is not None
            and planned_take_profit_price is not None
        )
        paper_position = engine.paper_broker.open_position(
            action=action,
            timestamp=str(time()),
            price=price,
            reason=reason,
            quantity=quantity,
            stop_price=(planned_stop_price if engine.config.dynamic_exit_enabled else None),
            take_profit_price=(
                planned_take_profit_price if engine.config.dynamic_exit_enabled else None
            ),
            trade_plan_id=trade_plan_id,
            dynamic_exit_used=planned_exit_applied,
            exit_source=planned_exit_source or GLOBAL_FALLBACK_EXIT_SOURCE,
            recommended_stop_price=planned_stop_price,
            recommended_take_profit_price=planned_take_profit_price,
        )
        engine.paper_journal.append(
            "paper_position_opened",
            symbol=engine.config.symbol,
            direction=paper_position.direction.value,
            entry_price=paper_position.entry_price,
            quantity=paper_position.quantity,
            quantity_source=quantity_source,
            planned_quantity=planned_quantity,
            planned_quantity_applied=quantity_source == "TRADE_PLAN",
            dynamic_sizing_enabled=engine.config.dynamic_sizing_enabled,
            trade_plan_id=trade_plan_id,
            stop_price=paper_position.stop_price,
            take_profit_price=paper_position.take_profit_price,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
            planned_exit_applied=planned_exit_applied,
            planned_exit_source=planned_exit_source,
            dynamic_exit_enabled=engine.config.dynamic_exit_enabled,
            reason=reason,
        )
        if planned_exit_applied and planned_exit_source == STRATEGY_METADATA_EXIT_SOURCE:
            payload = {
                "symbol": engine.config.symbol,
                "trade_plan_id": trade_plan_id,
                "stop_price": paper_position.stop_price,
                "take_profit_price": paper_position.take_profit_price,
                "planned_exit_source": planned_exit_source,
                "execution_mode": engine.config.execution_mode.value,
            }
            self.governor_journal.append(
                "protection_order_created_from_strategy_exit",
                **payload,
            )
            engine.record_event(
                "protection_order_created_from_strategy_exit",
                **payload,
            )
        self.trade_audit_recorder.record_open(
            symbol=engine.config.symbol,
            trade_plan_id=trade_plan_id,
            entry_time=paper_position.entry_time,
            entry_reason=reason,
            entry_price=paper_position.entry_price,
            quantity=paper_position.quantity,
            direction=paper_position.direction.value,
            stop_price=paper_position.stop_price,
            take_profit_price=paper_position.take_profit_price,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
            diagnostics={
                "source": "PAPER_BROKER",
                "quantity_source": quantity_source,
                "planned_quantity": planned_quantity,
                "planned_exit_applied": planned_exit_applied,
                "planned_exit_source": planned_exit_source,
            },
        )
        self.trade_audit_recorder.record_protected(
            symbol=engine.config.symbol,
            trade_plan_id=trade_plan_id,
            entry_time=paper_position.entry_time,
            entry_reason=reason,
            entry_price=paper_position.entry_price,
            quantity=paper_position.quantity,
            direction=paper_position.direction.value,
            stop_price=paper_position.stop_price,
            take_profit_price=paper_position.take_profit_price,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
            protection_source="PAPER_SIMULATED_PROTECTION",
            protection_model="PAPER_BROKER",
            diagnostics={"source": "PAPER_BROKER"},
        )
        return PositionExecutionResult.confirmed(
            "PAPER_POSITION_OPEN_CONFIRMED",
            diagnostics={
                "symbol": engine.config.symbol,
                "quantity": paper_position.quantity,
                "entry_price": paper_position.entry_price,
                "quantity_source": quantity_source,
                "trade_plan_id": trade_plan_id,
            },
        )

    def _paper_close_position(
        self,
        engine: FuturesTradingEngine,
        *,
        price: Decimal,
        reason: str,
        exit_reason: str = "governor_close",
    ) -> PositionExecutionResult:
        if engine.paper_broker is None or not engine.paper_broker.current_position().is_open:
            engine.record_event("paper_close_skipped_no_position", symbol=engine.config.symbol)
            return PositionExecutionResult.skipped(
                "PAPER_POSITION_NOT_OPEN",
                diagnostics={"symbol": engine.config.symbol},
            )

        paper_position = engine.paper_broker.position
        trade = engine.paper_broker.close_position(
            timestamp=str(time()),
            price=price,
            exit_reason=exit_reason,
            strategy_reason_exit=reason,
        )

        outcome = build_trade_plan_outcome_from_trade_result(
            trade_result=trade,
            trade_plan_id=paper_position.trade_plan_id if paper_position is not None else None,
            active_trade_id=None,
            planned_stop_price=paper_position.stop_price if paper_position is not None else None,
            planned_take_profit_price=(
                paper_position.take_profit_price if paper_position is not None else None
            ),
            adaptive_adjustments_applied=(
                paper_position.adaptive_adjustments_applied if paper_position is not None else 0
            ),
            last_adaptive_adjustment_plan_id=(
                paper_position.last_adaptive_adjustment_plan_id
                if paper_position is not None
                else None
            ),
        )

        engine.paper_journal.append(
            "paper_position_closed",
            trade=trade,
            net_pnl=trade.net_pnl,
            trade_plan_id=outcome.trade_plan_id,
            outcome_id=outcome.outcome_id,
        )
        engine.paper_journal.append(
            "trade_plan_outcome_recorded",
            outcome=outcome,
            trade_plan_id=outcome.trade_plan_id,
            outcome_status=outcome.outcome_status,
            net_pnl=outcome.net_pnl,
        )
        self.governor_journal.append(
            "trade_plan_outcome_recorded",
            symbol=outcome.symbol,
            trade_plan_id=outcome.trade_plan_id,
            outcome_id=outcome.outcome_id,
            outcome_status=outcome.outcome_status,
            net_pnl=outcome.net_pnl,
            source=outcome.source,
        )
        self.trade_audit_recorder.record_completed(
            symbol=trade.symbol,
            trade_id=outcome.active_trade_id or outcome.trade_plan_id,
            trade_plan_id=outcome.trade_plan_id,
            entry_time=trade.entry_time,
            exit_time=trade.exit_time,
            entry_reason=trade.strategy_reason_entry,
            exit_reason=reason or trade.strategy_reason_exit or trade.exit_reason,
            entry_price=trade.entry_price,
            close_price=trade.exit_price,
            quantity=trade.quantity,
            direction=trade.direction.value,
            stop_price=paper_position.stop_price if paper_position is not None else None,
            take_profit_price=(
                paper_position.take_profit_price if paper_position is not None else None
            ),
            planned_stop_price=outcome.planned_stop_price,
            planned_take_profit_price=outcome.planned_take_profit_price,
            gross_pnl=trade.gross_pnl,
            net_pnl=trade.net_pnl,
            pnl_status=outcome.pnl_status,
            pnl_source=outcome.pnl_source,
            estimated_spread_cost=trade.spread_cost,
            estimated_slippage_cost=trade.slippage_cost,
            estimated_funding_cost=trade.funding_cost,
            diagnostics={
                "source": "PAPER_BROKER",
                "outcome_id": outcome.outcome_id,
                "fees": trade.fees,
                "return_pct": trade.return_pct,
            },
        )
        return PositionExecutionResult.confirmed(
            "PAPER_POSITION_CLOSE_CONFIRMED",
            diagnostics={
                "symbol": trade.symbol,
                "trade_plan_id": outcome.trade_plan_id,
                "outcome_id": outcome.outcome_id,
                "net_pnl": outcome.net_pnl,
            },
        )

    def _get_engine(self, symbol_config: SymbolTradingConfig) -> FuturesTradingEngine:
        engine = self._engines.get(symbol_config.symbol)
        if engine is not None:
            return engine

        app_config = self._symbol_app_config(symbol_config)
        risk_manager = FuturesRiskManager(
            position_notional_usdt=symbol_config.position_notional_usdt,
            stop_loss_pct=symbol_config.stop_loss_pct,
            take_profit_pct=symbol_config.take_profit_pct,
        )
        paper_journal = self.journals.journal(self._paper_journal_path(symbol_config))
        risk_guard = self.risk_guard_for_symbol(symbol_config)
        engine = FuturesTradingEngine(
            config=app_config,
            client=self.client,
            risk_manager=risk_manager,
            journals=self.journals,
            paper_journal=paper_journal,
            trade_audit_recorder=self.trade_audit_recorder,
            risk_guard=risk_guard,
            state_reconciliation_engine=self.state_reconciliation_engine,
        )
        self._engines[symbol_config.symbol] = engine
        return engine

    def _symbol_app_config(self, symbol_config: SymbolTradingConfig) -> AppConfig:
        return self.build_symbol_app_config(symbol_config)

    def build_symbol_app_config(self, symbol_config: SymbolTradingConfig) -> AppConfig:
        return replace(
            self.base_config,
            symbol=symbol_config.symbol,
            symbols=(symbol_config.symbol,),
            symbol_contexts=(
                TradingSymbolConfig(
                    symbol=symbol_config.symbol,
                    state_path=symbol_config.state_path,
                    lock_path=symbol_config.lock_path,
                    journal_path=symbol_config.journal_path,
                ),
            ),
            margin_type=symbol_config.margin_type,
            leverage=symbol_config.leverage,
            position_notional_usdt=symbol_config.position_notional_usdt,
            stop_loss_pct=symbol_config.stop_loss_pct,
            take_profit_pct=symbol_config.take_profit_pct,
            strategy_module=self.base_config.strategy_module,
            strategy_class=self.base_config.strategy_class,
            strategy_params=dict(self.base_config.strategy_params),
            bot_order_prefix=symbol_config.bot_order_prefix,
            trade_journal_path=symbol_config.journal_path,
            trade_audit_path=self.base_config.trade_audit_path,
            state_path=symbol_config.state_path,
            runtime_dir=symbol_config.runtime_dir,
            recovery_action=symbol_config.recovery_action,
            protection_policy=symbol_config.protection_policy,
            protection_working_type=symbol_config.protection_working_type,
            max_daily_loss_usdt=symbol_config.max_daily_loss_usdt,
            max_daily_trades=symbol_config.max_daily_trades,
            max_consecutive_losses_guard=symbol_config.max_consecutive_losses_guard,
            cooldown_after_loss_seconds=symbol_config.cooldown_after_loss_seconds,
            max_position_hold_seconds=symbol_config.max_position_hold_seconds,
            max_exposure_usdt=symbol_config.max_exposure_usdt,
            exit_model=symbol_config.exit_model,
            atr_period=symbol_config.atr_period,
            atr_stop_multiplier=symbol_config.atr_stop_multiplier,
            atr_take_multiplier=symbol_config.atr_take_multiplier,
        )


def _paper_cost_model(config: AppConfig) -> CostModel:
    """Paper fills must pay the same fees/spread/slippage the operator configured."""

    try:
        return CostModel.from_numbers(
            maker_fee_pct=config.maker_fee_pct,
            taker_fee_pct=config.taker_fee_pct,
            slippage_bps=config.estimated_slippage_bps,
            spread_bps=config.estimated_spread_bps,
            funding_enabled=config.funding_enabled,
            default_funding_rate=config.estimated_funding_rate,
        )
    except (AttributeError, TypeError, ValueError, ArithmeticError):
        return CostModel()


def _seconds_since_open(position: FuturesPosition) -> float | None:
    if not position.is_open or position.opened_at is None:
        return None
    try:
        return max(0.0, time() - float(position.opened_at))
    except (TypeError, ValueError):
        return None


def _operational_mode_from_config(config: AppConfig) -> str:
    symbols = tuple(getattr(config, "symbols", ()) or ())
    if getattr(config, "multi_symbol_enabled", False) or len(symbols) > 1:
        return "GOVERNOR_MULTI_SYMBOL"
    return "GOVERNOR_SINGLE_SYMBOL"


def empty_position(symbol: str) -> FuturesPosition:
    return FuturesPosition(
        symbol=symbol,
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )
