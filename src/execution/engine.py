import threading
from collections import deque
from decimal import Decimal
from pathlib import Path

from binance.futures_client import (
    BinanceFuturesAPIError,
    BinanceFuturesClient,
    BinanceFuturesNetworkError,
)
from config import AppConfig
from domain.models import FuturesPosition, TradeAction
from domain.symbol_rules import SymbolRules
from execution.engine_error_mixin import EngineErrorMixin
from execution.engine_exchange_mixin import EngineExchangeMixin
from execution.engine_order_mixin import EngineOrderMixin
from execution.engine_position_mixin import EnginePositionMixin
from execution.engine_state_mixin import EngineStateMixin
from execution.ledger import PositionLedger
from execution.simulated_broker import SimulatedBroker
from execution.state_reconciliation import StateReconciliationEngine
from execution.trade_audit import TradeAuditRecorder
from futures.exit_models import build_exit_model
from futures.operational import OperationalState
from futures.order_queries import OpenOrdersResult
from futures.position_execution import (
    PositionExecutionResult,
    PositionExecutionStatus,
)
from futures.protection import ExpectedProtection
from futures.risk_guard import FuturesRiskGuard
from futures.risk_manager import FuturesRiskManager
from futures.state import RuntimeState, RuntimeStateStore
from operational_module_modes import SmartSizingMode
from utils.journals import JournalRegistry
from utils.logger import get_logger
from utils.trade_journal import TradeJournal


class FuturesTradingEngine(
    EngineExchangeMixin,
    EngineOrderMixin,
    EnginePositionMixin,
    EngineStateMixin,
    EngineErrorMixin,
):
    def __init__(
        self,
        config: AppConfig,
        client: BinanceFuturesClient,
        risk_manager: FuturesRiskManager,
        *,
        journals: JournalRegistry | None = None,
        paper_journal: TradeJournal | None = None,
        trade_audit_recorder: TradeAuditRecorder | None = None,
        risk_guard: FuturesRiskGuard | None = None,
        state_reconciliation_engine: StateReconciliationEngine | None = None,
    ):
        self.config = config
        self.client = client
        self.risk_manager = risk_manager
        self.logger = get_logger(__name__)
        # The caller's registry owns the journal files; a standalone engine (ops) owns its own.
        journals = journals or JournalRegistry()
        self.journal = journals.journal(config.trade_journal_path)
        self.paper_journal = paper_journal or journals.journal(
            Path(config.trade_journal_path).parent
            / f"paper_trades_{config.symbol.lower()}_{config.environment_key}.jsonl"
        )
        self.trade_audit_recorder = trade_audit_recorder or TradeAuditRecorder.from_config(
            config, journals=journals
        )
        self.position_ledger = PositionLedger[RuntimeState](
            state_path=config.state_path,
            position_store=RuntimeStateStore(config.state_path),
        )
        self.state_store = self.position_ledger
        self.entry_intent_store = self.position_ledger.entry_intents
        self.close_intent_store = self.position_ledger.close_intents
        self.risk_guard = risk_guard or FuturesRiskGuard(config)
        self.exit_model = build_exit_model(config)
        self.state_reconciliation_engine = (
            state_reconciliation_engine or StateReconciliationEngine()
        )
        self._protection_operation_lock = threading.RLock()
        self.paper_broker: SimulatedBroker | None = None

        self.prices: deque[Decimal] = deque(maxlen=300)
        self.rules: SymbolRules | None = None

        self.position_opened_at: float | None = None
        self.was_position_open = False
        self.active_trade_id: str | None = None
        if self.runtime_state is not None:
            self.active_trade_id = self.runtime_state.active_trade_id
            self.position_opened_at = self.runtime_state.opened_at

        self.paused = False
        self.consecutive_errors = 0
        self.current_backoff_seconds = self.config.initial_backoff_seconds

    def recover(self) -> None:
        self._recover_state_on_boot()

    def get_current_price(self) -> Decimal:
        return self._get_current_price()

    def get_trigger_reference_price(self) -> Decimal:
        """Price used by STOP/TAKE triggers (mark or contract, per config)."""
        return self._get_trigger_reference_price()

    def get_position(self) -> FuturesPosition:
        return self._get_position()

    def sync_position_state(self, position: FuturesPosition) -> FuturesPosition:
        return self._sync_local_position_state(position)

    def get_open_orders(self) -> OpenOrdersResult:
        return self._safe_get_open_orders()

    def get_open_algo_orders(self) -> OpenOrdersResult:
        return self._safe_get_open_algo_orders()

    def get_runtime_state(self) -> RuntimeState | None:
        return self.runtime_state

    @property
    def runtime_state(self) -> RuntimeState | None:
        return self.position_ledger.position

    @runtime_state.setter
    def runtime_state(self, value: RuntimeState | None) -> None:
        self.position_ledger.observe_local_state(value)

    def open_position(
        self,
        action: TradeAction,
        price: Decimal,
        reason: str,
        *,
        planned_quantity: Decimal | None = None,
        trade_plan_id: str | None = None,
        planned_stop_price: Decimal | None = None,
        planned_take_profit_price: Decimal | None = None,
        planned_exit_source: str | None = None,
        prevalidated_protection: ExpectedProtection | None = None,
    ) -> PositionExecutionResult:
        result = self._open_position(
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
        return self._reconcile_open_position_result(result)

    def _reconcile_open_position_result(
        self,
        result: PositionExecutionResult,
    ) -> PositionExecutionResult:
        if result.status is not PositionExecutionStatus.CONFIRMED:
            return result

        runtime_state = self.runtime_state
        operational_state = getattr(runtime_state, "operational_state", None)
        if operational_state is OperationalState.POSITION_OPEN_PROTECTED:
            return result

        diagnostics = {
            **dict(result.diagnostics),
            "runtime_operational_state": (
                operational_state.value
                if isinstance(operational_state, OperationalState)
                else operational_state
            ),
            "engine_paused": self.paused,
        }

        try:
            current_position = self._get_position()
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as error:
            return PositionExecutionResult.partial(
                "POSITION_STATE_UNKNOWN_AFTER_PROTECTION_FAILURE",
                order_ids=result.order_ids,
                diagnostics={
                    **diagnostics,
                    "reconciliation_error": type(error).__name__,
                },
            )

        if not current_position.is_open:
            return PositionExecutionResult.rolled_back(
                "POSITION_OPEN_ROLLED_BACK_AFTER_PROTECTION_FAILURE",
                order_ids=result.order_ids,
                diagnostics={
                    **diagnostics,
                    "final_position": current_position,
                },
            )

        if (
            self.paused
            or runtime_state is None
            or operational_state
            in {
                OperationalState.PROTECTIONS_PENDING,
                OperationalState.POSITION_OPEN_UNPROTECTED,
                OperationalState.MANUAL_RECONCILE_REQUIRED,
            }
        ):
            return PositionExecutionResult.partial(
                "POSITION_OPEN_PARTIAL_AFTER_PROTECTION_FAILURE",
                order_ids=result.order_ids,
                diagnostics={
                    **diagnostics,
                    "final_position": current_position,
                },
            )

        return result

    def select_entry_quantity(
        self,
        *,
        price: Decimal,
        planned_quantity: Decimal | None = None,
        trade_plan_id: str | None = None,
    ) -> tuple[Decimal, str]:
        if self.rules is None:
            raise RuntimeError("Symbol rules not loaded")

        reduce_only_rejected = False
        if self.config.dynamic_sizing_enabled and planned_quantity is not None:
            quantity = self.risk_manager.normalize_planned_quantity(
                planned_quantity=planned_quantity,
                price=price,
                rules=self.rules,
            )
            quantity_source = "TRADE_PLAN"
            if self.config.smart_sizing_mode is SmartSizingMode.REDUCE_ONLY:
                baseline_quantity = self.risk_manager.calculate_quantity(
                    price,
                    self.rules,
                )
                if quantity > baseline_quantity:
                    quantity = baseline_quantity
                    quantity_source = "RISK_MANAGER"
                    reduce_only_rejected = True
        else:
            quantity = self.risk_manager.calculate_quantity(price, self.rules)
            quantity_source = "RISK_MANAGER"

        self._record_journal(
            "entry_quantity_selected",
            symbol=self.config.symbol,
            quantity=quantity,
            quantity_source=quantity_source,
            reference_price=price,
            notional=quantity * price,
            dynamic_sizing_enabled=self.config.dynamic_sizing_enabled,
            planned_quantity=planned_quantity,
            planned_quantity_applied=quantity_source == "TRADE_PLAN",
            planned_quantity_ignored=(
                planned_quantity is not None and not self.config.dynamic_sizing_enabled
            ),
            planned_quantity_reduce_only_rejected=reduce_only_rejected,
            smart_sizing_mode=getattr(
                self.config.smart_sizing_mode, "value", self.config.smart_sizing_mode
            ),
            risk_manager_fallback=(self.config.dynamic_sizing_enabled and planned_quantity is None),
            trade_plan_id=trade_plan_id,
        )
        return quantity, quantity_source

    def close_position(self, position: FuturesPosition, reason: str) -> PositionExecutionResult:
        return self._close_position(position, reason)

    def audit_protections(self, position: FuturesPosition, reason: str) -> None:
        self._audit_open_position_protections(position, reason=reason)

    def cancel_robot_orders(self, reason: str) -> bool:
        return self._safe_cancel_all_open_orders(reason=reason)

    def record_event(self, event_type: str, **data) -> None:
        self._record_journal(event_type, **data)
