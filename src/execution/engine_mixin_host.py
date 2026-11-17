"""Typed contract shared by the execution engine mixins."""

from __future__ import annotations

import logging
from _thread import RLock
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from binance.futures_client import BinanceFuturesClient, RequestTimeout
from config import AppConfig
from domain.models import FuturesPosition, TradeAction
from domain.orders import OrderState
from domain.symbol_rules import SymbolRules
from execution.entry_intent_store import CloseIntentStore, EntryIntentStore
from execution.errors import UnknownOrderStatus
from execution.ledger import PositionLedger
from execution.simulated_broker import SimulatedBroker
from execution.state_reconciliation import StateReconciliationEngine
from execution.trade_audit import TradeAuditRecorder
from futures.exit_models import ATRExitModel, FixedPctExitModel
from futures.operational import OperationalState
from futures.order_queries import OpenOrdersResult
from futures.position_execution import PositionExecutionResult
from futures.protection import ExpectedProtection, ProtectionAuditResult
from futures.risk_guard import FuturesRiskGuard
from futures.risk_manager import FuturesRiskManager
from futures.state import RuntimeState
from utils.trade_journal import TradeJournal


class EngineMixinHost(Protocol):
    config: AppConfig
    client: BinanceFuturesClient
    risk_manager: FuturesRiskManager
    risk_guard: FuturesRiskGuard
    logger: logging.Logger
    journal: TradeJournal
    trade_audit_recorder: TradeAuditRecorder
    state_reconciliation_engine: StateReconciliationEngine
    state_store: PositionLedger[RuntimeState]
    entry_intent_store: EntryIntentStore
    close_intent_store: CloseIntentStore
    exit_model: FixedPctExitModel | ATRExitModel
    paper_broker: SimulatedBroker | None
    rules: SymbolRules | None
    prices: deque[Decimal]
    _protection_operation_lock: RLock
    active_trade_id: str | None
    position_opened_at: float | None
    was_position_open: bool
    paused: bool
    consecutive_errors: int
    current_backoff_seconds: float

    @property
    def runtime_state(self) -> RuntimeState | None: ...

    @runtime_state.setter
    def runtime_state(self, value: RuntimeState | None) -> None: ...
    def _advance_close_intent(self, state: OrderState) -> bool: ...

    def _advance_entry_intent(self, state: OrderState) -> bool: ...

    def _attach_or_create_state_for_open_position(
        self, position: FuturesPosition
    ) -> FuturesPosition: ...

    def _audit_protection_orders(
        self, *, position: FuturesPosition, robot_algo_orders: list[dict], reason: str
    ) -> ProtectionAuditResult: ...

    def _cancel_robot_open_orders(
        self, normal_orders: list[dict], algo_orders: list[dict], reason: str
    ) -> None: ...

    def _clear_close_intent(self) -> bool: ...

    def _clear_entry_intent(self) -> bool: ...

    def _clear_runtime_state(self) -> None: ...

    @staticmethod
    def _close_order_fully_filled(order_response: dict | None, quantity: Decimal) -> bool: ...

    def _close_position(
        self, position: FuturesPosition, reason: str
    ) -> PositionExecutionResult: ...

    def _complete_entry_after_confirmed_close(self) -> bool: ...

    def _empty_position(self) -> FuturesPosition: ...

    @staticmethod
    def _extract_algo_client_id(order: dict) -> str: ...

    def _filter_robot_algo_orders(self, orders: list[dict]) -> list[dict]: ...

    def _filter_robot_normal_orders(self, orders: list[dict]) -> list[dict]: ...

    def _get_position(self, *, timeout: RequestTimeout | None = ...) -> FuturesPosition: ...

    def _handle_unprotected_position(
        self,
        *,
        position: FuturesPosition,
        audit_result: ProtectionAuditResult,
        reason: str,
        robot_algo_orders: list[dict],
    ) -> None: ...

    def _has_available_margin_for_order(self, quantity: Decimal, price: Decimal) -> bool: ...

    def _log_account_balance(self, account_info: dict) -> None: ...

    def _mark_position_protected(
        self, position: FuturesPosition, *, audit_result: ProtectionAuditResult, reason: str
    ) -> None: ...

    def _new_client_order_id(self, purpose: str) -> str: ...

    def _new_trade_id(self, purpose: str) -> str: ...

    def _normalized_order_prefix(self) -> str: ...

    def _pause_engine(self, reason: str, **data) -> None: ...

    @staticmethod
    def _position_data(position: FuturesPosition) -> dict: ...

    @staticmethod
    def _protection_state_payload(
        expected: ExpectedProtection, *, fallback_trade_plan_id: str | None = ...
    ) -> dict: ...

    def _query_order_for_reconcile(
        self, client_order_id: str
    ) -> tuple[dict | None, str | None]: ...

    def _reconcile_close_order_unknown(
        self,
        *,
        error: UnknownOrderStatus,
        trade_id: str,
        close_client_order_id: str,
        quantity: Decimal,
    ) -> dict | None: ...

    def _reconcile_entry_order_unknown(
        self, *, error: UnknownOrderStatus, action: TradeAction, trade_id: str
    ) -> tuple[FuturesPosition, dict] | None: ...

    def _record_external_position_close(self, *, reason: str) -> None: ...

    def _record_journal(self, event_type: str, **data) -> None: ...

    def _record_operational_state(
        self, state: OperationalState, *, reason: str, **data
    ) -> None: ...

    def _recover_pending_close_intent(self) -> bool: ...

    def _recover_pending_entry_intent(self) -> bool: ...

    def _recovery_requires_protection_priority(self) -> bool: ...

    def _runtime_state_data(self) -> dict | None: ...

    def _safe_cancel_all_open_orders(self, reason: str) -> bool: ...

    def _safe_get_open_algo_orders(self) -> OpenOrdersResult: ...

    def _safe_get_open_orders(self) -> OpenOrdersResult: ...

    def _settle_entry_order_before_protection(
        self,
        client_order_id: str,
        order: dict,
        position: FuturesPosition,
    ) -> tuple[dict, FuturesPosition] | None: ...

    def _save_state_for_position(
        self,
        *,
        position: FuturesPosition,
        trade_id: str,
        entry_client_order_id: str | None,
        stop_client_algo_id: str | None = ...,
        take_client_algo_id: str | None = ...,
        operational_state: OperationalState = ...,
        protection_warning: str = ...,
        protection_source: str = ...,
        trade_plan_id: str | None = ...,
        planned_stop_price: Decimal | None = ...,
        planned_take_profit_price: Decimal | None = ...,
    ) -> None: ...

    def _submit_algo_order(self, purpose: str, **params) -> dict: ...

    def _submit_order(self, purpose: str, **params) -> dict: ...

    def _write_manual_reconcile_required(self, reason: str, data: dict) -> Path: ...

    def select_entry_quantity(
        self,
        *,
        price: Decimal,
        planned_quantity: Decimal | None = ...,
        trade_plan_id: str | None = ...,
    ) -> tuple[Decimal, str]: ...
