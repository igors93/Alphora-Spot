import hashlib
import time
from dataclasses import replace
from decimal import Decimal

from binance.futures_client import (
    BinanceFuturesAPIError,
    BinanceFuturesNetworkError,
    RequestTimeout,
)
from config import ProtectionPolicy
from domain.models import FuturesPosition, PositionDirection, TradeAction
from domain.orders import OrderState
from domain.symbol_rules import format_decimal
from execution.engine_mixin_host import EngineMixinHost
from execution.errors import (
    EnginePaused,
    ProtectionTriggerAlreadyBreached,
    UnknownOrderStatus,
)
from futures.operational import (
    OperationalState,
)
from futures.planned_exit import STRATEGY_METADATA_EXIT_SOURCE
from futures.position_execution import PositionExecutionResult
from futures.protection import (
    PROTECTION_SOURCE_TRADE_PLAN,
    ExpectedProtection,
    ProtectionAuditResult,
    audit_protections,
    breached_protection_triggers,
    build_expected_protection,
    revalidate_expected_protection_after_fill,
)
from futures.risk_manager import TradeNotExecutableError
from resilience.budgets import POSITION_CONFIRMATION_BUDGET
from resilience.catalog import (
    FLAT_CONFIRMATION_ATTEMPTS,
    FLAT_CONFIRMATION_INTERVAL_SECONDS,
    FLAT_CONFIRMATION_MAX_TIME_SECONDS,
    FLAT_CONFIRMATION_REQUEST_TIMEOUT,
    POSITION_CONFIRMATION_ATTEMPTS,
    POSITION_CONFIRMATION_INTERVAL_SECONDS,
    POSITION_CONFIRMATION_MAX_TIME_SECONDS,
    POSITION_CONFIRMATION_REQUEST_TIMEOUT,
)
from resilience.polling import run_polling
from resilience.predicates import is_expected_position, is_open_position
from trading.outcome import build_trade_plan_outcome_from_live_payload
from utils.events import EventChannel, observe_event


def _estimate_gross_close_pnl(position: FuturesPosition, close_price: Decimal) -> Decimal:
    if position.direction is PositionDirection.LONG:
        return (close_price - position.entry_price) * position.quantity
    if position.direction is PositionDirection.SHORT:
        return (position.entry_price - close_price) * position.quantity
    return Decimal("0")


class EnginePositionMixin(EngineMixinHost):
    active_trade_id: str | None
    position_opened_at: float | None

    def _prepare_entry(
        self,
        action: TradeAction,
        price: Decimal,
        *,
        planned_quantity: Decimal | None,
        trade_plan_id: str | None,
    ) -> tuple[Decimal, str, str] | PositionExecutionResult:
        if getattr(self.client, "spot_mode", False) and action is TradeAction.OPEN_SHORT:
            return PositionExecutionResult.blocked("SPOT_OPEN_SHORT_FORBIDDEN")
        if self.config.mutations_enabled:
            try:
                pending_entry = self.entry_intent_store.load()
            except (OSError, ValueError) as error:
                self._pause_engine("entry_intent_read_failed", error=str(error))
                return PositionExecutionResult.blocked(
                    "ENTRY_INTENT_READ_FAILED", diagnostics={"error": str(error)}
                )
            if pending_entry is not None:
                self._pause_engine(
                    "entry_intent_pending",
                    client_order_id=pending_entry.lifecycle.client_order_id,
                    lifecycle_state=pending_entry.lifecycle.state.value,
                )
                return PositionExecutionResult.blocked(
                    "ENTRY_INTENT_PENDING",
                    order_ids=(pending_entry.lifecycle.client_order_id,),
                    diagnostics={"lifecycle_state": pending_entry.lifecycle.state.value},
                )

        if self.rules is None:
            return PositionExecutionResult.failed(
                "SYMBOL_RULES_NOT_LOADED",
                diagnostics={"symbol": self.config.symbol},
            )

        try:
            quantity, quantity_source = self.select_entry_quantity(
                price=price,
                planned_quantity=planned_quantity,
                trade_plan_id=trade_plan_id,
            )
        except TradeNotExecutableError as error:
            if self.config.dynamic_sizing_enabled and planned_quantity is not None:
                self._record_journal(
                    "position_open_blocked",
                    reason="planned_quantity_invalid",
                    action=action.value,
                    symbol=self.config.symbol,
                    trade_plan_id=trade_plan_id,
                    planned_quantity=planned_quantity,
                    error=str(error),
                    calculation=error.calculation,
                )
                self.logger.warning(
                    "Open blocked: invalid planned_quantity | trade_plan_id=%s | error=%s",
                    trade_plan_id,
                    error,
                )
                return PositionExecutionResult.blocked(
                    "PLANNED_QUANTITY_INVALID",
                    diagnostics={
                        "trade_plan_id": trade_plan_id,
                        "planned_quantity": planned_quantity,
                        "error": str(error),
                        "calculation": error.calculation,
                    },
                )
            return PositionExecutionResult.blocked(
                "ENTRY_QUANTITY_NOT_EXECUTABLE",
                diagnostics={
                    "trade_plan_id": trade_plan_id,
                    "planned_quantity": planned_quantity,
                    "error": str(error),
                    "calculation": error.calculation,
                },
            )

        notional = quantity * price
        guard_decision = self.risk_guard.can_open_position(notional=notional)
        if not guard_decision.allowed:
            self._record_journal(
                "trade_blocked_by_risk_guard",
                reason=guard_decision.reason,
                metrics=guard_decision.metrics,
                action=action.value,
                symbol=self.config.symbol,
                strategy=f"{self.config.strategy_module}.{self.config.strategy_class}",
                execution_mode=self.config.execution_mode.value,
                quantity=quantity,
                notional=notional,
            )
            self.logger.warning(
                "Trade blocked by risk guard | reason=%s | metrics=%s",
                guard_decision.reason,
                guard_decision.metrics,
            )
            return PositionExecutionResult.blocked(
                str(guard_decision.reason or "RISK_GUARD_BLOCKED"),
                diagnostics={
                    "metrics": guard_decision.metrics,
                    "quantity": quantity,
                    "notional": notional,
                    "execution_mode": self.config.execution_mode.value,
                },
            )

        if not self._safe_cancel_all_open_orders(reason="before_opening_new_position"):
            self._record_journal(
                "position_open_blocked",
                reason="open_orders_query_failed",
                action=action.value,
            )
            return PositionExecutionResult.blocked(
                "OPEN_ORDERS_QUERY_FAILED",
                diagnostics={"action": action.value, "symbol": self.config.symbol},
            )

        if not self._has_available_margin_for_order(quantity=quantity, price=price):
            return PositionExecutionResult.blocked(
                "INSUFFICIENT_MARGIN",
                diagnostics={
                    "quantity": quantity,
                    "price": price,
                    "notional": notional,
                    "leverage": self.config.leverage,
                },
            )

        if action == TradeAction.OPEN_LONG:
            entry_side = "BUY"
        elif action == TradeAction.OPEN_SHORT:
            entry_side = "SELL"
        else:
            return PositionExecutionResult.failed(
                "ACTION_NOT_EXECUTABLE",
                diagnostics={"action": getattr(action, "value", str(action))},
            )

        return quantity, quantity_source, entry_side

    def _persist_entry_intent(
        self,
        *,
        entry_side: str,
        quantity: Decimal,
        trade_id: str,
        entry_client_order_id: str,
    ) -> PositionExecutionResult | None:
        if not self.config.mutations_enabled:
            return None
        try:
            self.entry_intent_store.begin(
                symbol=self.config.symbol,
                side=entry_side,
                quantity=quantity,
                trade_id=trade_id,
                client_order_id=entry_client_order_id,
            )
            self.entry_intent_store.advance(OrderState.SUBMITTING)
        except (OSError, ValueError) as error:
            self._pause_engine(
                "entry_intent_persist_failed",
                trade_id=trade_id,
                client_order_id=entry_client_order_id,
                error=str(error),
            )
            return PositionExecutionResult.failed(
                "ENTRY_INTENT_PERSIST_FAILED",
                order_ids=(entry_client_order_id,),
                diagnostics={"trade_id": trade_id, "error": str(error)},
            )
        return None

    def _confirm_partial_entry_settled(
        self,
        *,
        action: TradeAction,
        quantity: Decimal,
        client_order_id: str,
        position: FuturesPosition,
    ) -> FuturesPosition | PositionExecutionResult:
        entry_order, query_error = self._query_order_for_reconcile(client_order_id)
        settled = (
            self._settle_entry_order_before_protection(client_order_id, entry_order, position)
            if entry_order is not None
            else None
        )
        if settled is None:
            self._advance_entry_intent(OrderState.UNKNOWN)
            self._pause_engine(
                "entry_partial_fill_unsettled",
                client_order_id=client_order_id,
                query_error=query_error,
            )
            return PositionExecutionResult.uncertain(
                "ENTRY_PARTIAL_FILL_UNSETTLED", order_ids=(client_order_id,)
            )
        entry_order, position = settled
        try:
            executed_quantity = Decimal(str(entry_order.get("executedQty") or "0"))
        except (ValueError, ArithmeticError):
            executed_quantity = Decimal("0")
        expected_direction = (
            PositionDirection.LONG if action is TradeAction.OPEN_LONG else PositionDirection.SHORT
        )
        net_quantity = executed_quantity
        if getattr(self.client, "spot_mode", False):
            net_quantity = self.client.spot_net_buy_quantity(
                self.config.symbol, str(entry_order.get("orderId") or ""), executed_quantity,
            )
        if (
            not position.is_open
            or position.direction is not expected_direction
            or not executed_quantity.is_finite()
            or net_quantity != position.quantity
            or executed_quantity > quantity
            or (
                str(entry_order.get("status") or "").upper() == "FILLED"
                and executed_quantity != quantity
            )
        ):
            self._advance_entry_intent(OrderState.UNKNOWN)
            self._pause_engine(
                "entry_partial_fill_exchange_divergence",
                client_order_id=client_order_id,
                order_response=entry_order,
                position=position,
            )
            return PositionExecutionResult.uncertain(
                "ENTRY_PARTIAL_FILL_DIVERGENCE", order_ids=(client_order_id,)
            )
        return position

    def _dry_run_entry_result(
        self,
        *,
        action: TradeAction,
        entry_side: str,
        quantity: Decimal,
        quantity_source: str,
        price: Decimal,
        planned_quantity: Decimal | None,
        trade_plan_id: str | None,
        trade_id: str,
        entry_client_order_id: str,
        order_response: dict,
    ) -> PositionExecutionResult:
        self._record_journal(
            "position_open_dry_run",
            trade_id=trade_id,
            entry_client_order_id=entry_client_order_id,
            action=action.value,
            side=entry_side,
            quantity=quantity,
            quantity_source=quantity_source,
            planned_quantity=planned_quantity,
            planned_quantity_applied=quantity_source == "TRADE_PLAN",
            dynamic_sizing_enabled=self.config.dynamic_sizing_enabled,
            trade_plan_id=trade_plan_id,
            reference_price=price,
            order_response=order_response,
        )
        self.active_trade_id = None
        return PositionExecutionResult.dry_run(
            "DRY_RUN_OPEN_NOT_SENT",
            order_ids=(entry_client_order_id,),
            diagnostics={
                "trade_id": trade_id,
                "order_response": order_response,
                "quantity": quantity,
                "quantity_source": quantity_source,
            },
        )

    def _submit_entry(
        self,
        *,
        action: TradeAction,
        entry_side: str,
        quantity: Decimal,
        quantity_source: str,
        price: Decimal,
        planned_quantity: Decimal | None,
        trade_plan_id: str | None,
        trade_id: str,
    ) -> tuple[FuturesPosition, dict, str] | PositionExecutionResult:
        entry_client_order_id = self._new_client_order_id("ENTRY")

        persist_failure = self._persist_entry_intent(
            entry_side=entry_side,
            quantity=quantity,
            trade_id=trade_id,
            entry_client_order_id=entry_client_order_id,
        )
        if persist_failure is not None:
            return persist_failure

        try:
            order_response = self._submit_order(
                purpose="entry",
                symbol=self.config.symbol,
                side=entry_side,
                type="MARKET",
                quantity=format_decimal(quantity),
                newOrderRespType="RESULT",
                newClientOrderId=entry_client_order_id,
            )
        except UnknownOrderStatus as error:
            if self.config.mutations_enabled and (
                not self._advance_entry_intent(OrderState.UNKNOWN)
                or not self._advance_entry_intent(OrderState.RECONCILING)
            ):
                return PositionExecutionResult.uncertain(
                    "ENTRY_INTENT_TRANSITION_FAILED",
                    order_ids=(entry_client_order_id,),
                    diagnostics={"trade_id": trade_id},
                )
            reconciled = self._reconcile_entry_order_unknown(
                error=error,
                action=action,
                trade_id=trade_id,
            )
            if reconciled is None:
                if self.paused:
                    self._advance_entry_intent(OrderState.UNKNOWN)
                    return PositionExecutionResult.uncertain(
                        "ENTRY_ORDER_STATUS_UNKNOWN",
                        order_ids=(entry_client_order_id,),
                        diagnostics={"trade_id": trade_id, "error": str(error.original_error)},
                    )
                if self.config.mutations_enabled and (
                    not self._advance_entry_intent(OrderState.REJECTED)
                    or not self._clear_entry_intent()
                ):
                    return PositionExecutionResult.uncertain(
                        "ENTRY_INTENT_RESOLUTION_FAILED",
                        order_ids=(entry_client_order_id,),
                        diagnostics={"trade_id": trade_id},
                    )
                return PositionExecutionResult.skipped(
                    "ENTRY_ORDER_NOT_FILLED",
                    order_ids=(entry_client_order_id,),
                    diagnostics={"trade_id": trade_id, "error": str(error.original_error)},
                )

            position, order_response = reconciled
        else:
            if self.config.mutations_enabled and not self._advance_entry_intent(
                OrderState.ACKNOWLEDGED
            ):
                return PositionExecutionResult.uncertain(
                    "ENTRY_INTENT_TRANSITION_FAILED",
                    order_ids=(entry_client_order_id,),
                    diagnostics={"trade_id": trade_id},
                )
            if not self.config.mutations_enabled:
                return self._dry_run_entry_result(
                    action=action,
                    entry_side=entry_side,
                    quantity=quantity,
                    quantity_source=quantity_source,
                    price=price,
                    planned_quantity=planned_quantity,
                    trade_plan_id=trade_plan_id,
                    trade_id=trade_id,
                    entry_client_order_id=entry_client_order_id,
                    order_response=order_response,
                )

            confirmed_position = self._wait_for_position_after_entry(action)
            if confirmed_position is None:
                if self.config.mutations_enabled:
                    self._advance_entry_intent(OrderState.UNKNOWN)
                self._record_journal(
                    "position_open_not_confirmed",
                    trade_id=trade_id,
                    entry_client_order_id=entry_client_order_id,
                    order_response=order_response,
                )
                self._pause_engine(
                    "position_open_not_confirmed",
                    trade_id=trade_id,
                    entry_client_order_id=entry_client_order_id,
                )
                return PositionExecutionResult.uncertain(
                    "POSITION_OPEN_NOT_CONFIRMED",
                    order_ids=(entry_client_order_id,),
                    diagnostics={"trade_id": trade_id, "order_response": order_response},
                )
            position = confirmed_position

        if self.config.mutations_enabled and position.quantity < quantity:
            settled_position = self._confirm_partial_entry_settled(
                action=action,
                quantity=quantity,
                client_order_id=entry_client_order_id,
                position=position,
            )
            if isinstance(settled_position, PositionExecutionResult):
                return settled_position
            position = settled_position

        if self.config.mutations_enabled:
            fill_state = (
                OrderState.FILLED if str(order_response.get("status") or "").upper() == "FILLED"
                else OrderState.PARTIALLY_FILLED
            )
            if not self._advance_entry_intent(fill_state):
                return PositionExecutionResult.uncertain(
                    "ENTRY_INTENT_TRANSITION_FAILED",
                    order_ids=(entry_client_order_id,),
                    diagnostics={"trade_id": trade_id, "position": position},
                )

        return position, order_response, entry_client_order_id

    def _protect_entry(
        self,
        *,
        action: TradeAction,
        position: FuturesPosition,
        trade_id: str,
        entry_client_order_id: str,
        planned_stop_price: Decimal | None,
        planned_take_profit_price: Decimal | None,
        trade_plan_id: str | None,
        planned_exit_source: str | None,
        effective_protection: ExpectedProtection | None,
        entry_price: Decimal,
        order_response: dict,
    ) -> PositionExecutionResult:
        try:
            self._create_protection_orders(
                position=position,
                reason="after_entry",
                trade_id=trade_id,
                planned_stop_price=planned_stop_price,
                planned_take_profit_price=planned_take_profit_price,
                trade_plan_id=trade_plan_id,
                planned_exit_source=planned_exit_source,
                expected_protection=effective_protection,
            )
        except (
            BinanceFuturesAPIError,
            BinanceFuturesNetworkError,
            EnginePaused,
            ProtectionTriggerAlreadyBreached,
        ) as prot_error:
            # Protection trigger price was already breached by the time the order was
            # submitted (price moved past stop or TP between entry fill and order placement).
            # Close the position immediately regardless of protection_policy — leaving it
            # open without protection is riskier than an unplanned market exit.
            trigger_already_hit = isinstance(prot_error, ProtectionTriggerAlreadyBreached) or (
                isinstance(prot_error, BinanceFuturesAPIError)
                and prot_error.code == -2021
                and self.config.mutations_enabled
            )
            self._record_journal(
                "protection_creation_failed",
                trade_id=trade_id,
                action=action.value,
                position=position,
            )
            observe_event(
                "protection_order_missing",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                severity="WARNING" if trigger_already_hit else "ERROR",
                symbol=self.config.symbol,
                trade_id=trade_id,
                trade_plan_id=trade_plan_id,
                action=action.value,
                reason="protection_trigger_already_hit"
                if trigger_already_hit
                else "protection_creation_failed",
                position=position,
            )
            if trigger_already_hit:
                self.logger.warning(
                    "Protection trigger already breached after entry fill; closing position | %s",
                    prot_error,
                )
            else:
                self.logger.exception("Failed to create stop/take-profit after entry.")

            current_position = self._get_position()
            should_close = current_position.is_open and (
                trigger_already_hit
                or self.config.protection_policy is ProtectionPolicy.CLOSE_ON_UNPROTECTED
            )
            if should_close:
                close_reason = (
                    "protection_trigger_already_hit"
                    if trigger_already_hit
                    else "protection_creation_failed"
                )
                close_result = self._close_position(current_position, close_reason)
                if close_result.position_changed:
                    return PositionExecutionResult.rolled_back(
                        "PROTECTION_FAILED_POSITION_ROLLED_BACK",
                        order_ids=(entry_client_order_id,) + close_result.order_ids,
                        diagnostics={
                            "trade_id": trade_id,
                            "close_order_ids": close_result.order_ids,
                            "close_reason": close_result.reason,
                        },
                    )
                return PositionExecutionResult.partial(
                    "PROTECTION_FAILED_CLOSE_UNCERTAIN",
                    order_ids=(entry_client_order_id,) + close_result.order_ids,
                    diagnostics={
                        "trade_id": trade_id,
                        "close_reason": close_result.reason,
                        "close_status": close_result.status.value,
                    },
                )
            self._pause_engine(
                "protection_creation_failed",
                trade_id=trade_id,
                action=action.value,
            )
            return PositionExecutionResult.partial(
                "PROTECTION_FAILED_ENGINE_PAUSED",
                order_ids=(entry_client_order_id,),
                diagnostics={"trade_id": trade_id},
            )

        if self.config.mutations_enabled and self.paused:
            return PositionExecutionResult.partial(
                "ENTRY_INTENT_FINALIZATION_FAILED",
                order_ids=(entry_client_order_id,),
                diagnostics={"trade_id": trade_id},
            )

        return PositionExecutionResult.confirmed(
            "POSITION_OPEN_CONFIRMED",
            order_ids=(entry_client_order_id,),
            diagnostics={
                "trade_id": trade_id,
                "quantity": position.quantity,
                "entry_price": entry_price,
                "order_response": order_response,
            },
        )

    def _revalidate_entry_protection(
        self,
        *,
        action: TradeAction,
        price: Decimal,
        entry_price: Decimal,
        position: FuturesPosition,
        trade_id: str,
        entry_client_order_id: str,
        prevalidated_protection: ExpectedProtection | None,
    ) -> ExpectedProtection | PositionExecutionResult | None:
        effective_protection = prevalidated_protection
        post_fill_validation = None
        if self.config.post_fill_protection_revalidation_enabled:
            rules = self.rules
            if rules is None:
                self._pause_engine("symbol_rules_missing_after_entry", trade_id=trade_id)
                return PositionExecutionResult.failed("SYMBOL_RULES_NOT_LOADED")
            try:
                trigger_price = self._get_trigger_reference_price()
                post_fill_validation = revalidate_expected_protection_after_fill(
                    action=action,
                    planned_entry_price=price,
                    actual_entry_price=entry_price,
                    trigger_price=trigger_price,
                    risk_manager=self.risk_manager,
                    rules=rules,
                    working_type=self.config.protection_working_type.value,
                    exit_model=self.exit_model,
                    prevalidated=prevalidated_protection,
                    max_risk_increase_pct=Decimal(str(self.config.post_fill_max_risk_increase_pct)),
                    trigger_buffer_bps=Decimal(str(self.config.protection_trigger_buffer_bps)),
                )
            except (ValueError, ArithmeticError) as error:
                self._record_journal(
                    "post_fill_protection_revalidation_failed",
                    trade_id=trade_id,
                    action=action.value,
                    planned_entry_price=price,
                    actual_entry_price=entry_price,
                    reason=str(error),
                    error_type=type(error).__name__,
                )
                post_fill_validation = None
            if post_fill_validation is None or not post_fill_validation.allowed:
                diagnostics = (
                    {} if post_fill_validation is None else dict(post_fill_validation.diagnostics)
                )
                self._record_journal(
                    "post_fill_protection_invalid",
                    trade_id=trade_id,
                    action=action.value,
                    planned_entry_price=price,
                    actual_entry_price=entry_price,
                    reason=(
                        "POST_FILL_PROTECTION_REVALIDATION_FAILED"
                        if post_fill_validation is None
                        else post_fill_validation.reason
                    ),
                    diagnostics=diagnostics,
                )
                current_position = self._get_position()
                if current_position.is_open:
                    close_result = self._close_position(
                        current_position,
                        "post_fill_protection_invalid",
                    )
                    if close_result.position_changed:
                        return PositionExecutionResult.rolled_back(
                            "POST_FILL_PROTECTION_INVALID_POSITION_ROLLED_BACK",
                            order_ids=(entry_client_order_id,) + close_result.order_ids,
                            diagnostics={
                                "trade_id": trade_id,
                                "close_reason": close_result.reason,
                                "post_fill": diagnostics,
                            },
                        )
                    return PositionExecutionResult.partial(
                        "POST_FILL_PROTECTION_INVALID_CLOSE_UNCERTAIN",
                        order_ids=(entry_client_order_id,) + close_result.order_ids,
                        diagnostics={
                            "trade_id": trade_id,
                            "close_reason": close_result.reason,
                            "close_status": close_result.status.value,
                            "post_fill": diagnostics,
                        },
                    )
                return PositionExecutionResult.rolled_back(
                    "POST_FILL_PROTECTION_INVALID_POSITION_ALREADY_FLAT",
                    order_ids=(entry_client_order_id,),
                    diagnostics={
                        "trade_id": trade_id,
                        "post_fill": diagnostics,
                    },
                )
            effective_protection = post_fill_validation.expected
            self._record_journal(
                "post_fill_protection_revalidated",
                trade_id=trade_id,
                action=action.value,
                planned_entry_price=price,
                actual_entry_price=entry_price,
                adjusted=post_fill_validation.adjusted,
                reason=post_fill_validation.reason,
                risk_increase_pct=post_fill_validation.risk_increase_pct,
                take_extension_pct=post_fill_validation.take_extension_pct,
                diagnostics=post_fill_validation.diagnostics,
            )

        return effective_protection

    def _open_position(
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
        preparation = self._prepare_entry(
            action, price, planned_quantity=planned_quantity, trade_plan_id=trade_plan_id
        )
        if isinstance(preparation, PositionExecutionResult):
            return preparation
        quantity, quantity_source, entry_side = preparation

        trade_id = self._new_trade_id(action.value)
        self.active_trade_id = trade_id
        self._record_operational_state(
            OperationalState.ENTRY_PENDING,
            reason="position_open_requested",
            trade_id=trade_id,
            action=action.value,
        )

        self._record_journal(
            "position_open_requested",
            trade_id=trade_id,
            action=action.value,
            side=entry_side,
            symbol=self.config.symbol,
            quantity=quantity,
            quantity_source=quantity_source,
            planned_quantity=planned_quantity,
            planned_quantity_applied=quantity_source == "TRADE_PLAN",
            dynamic_sizing_enabled=self.config.dynamic_sizing_enabled,
            trade_plan_id=trade_plan_id,
            reference_price=price,
            reason=reason,
        )

        self.logger.info(
            "Opening position | trade_id=%s | side=%s | qty=%s | ref_price=%s",
            trade_id,
            entry_side,
            quantity,
            price,
        )

        submission = self._submit_entry(
            action=action,
            entry_side=entry_side,
            quantity=quantity,
            quantity_source=quantity_source,
            price=price,
            planned_quantity=planned_quantity,
            trade_plan_id=trade_plan_id,
            trade_id=trade_id,
        )
        if isinstance(submission, PositionExecutionResult):
            return submission
        position, order_response, entry_client_order_id = submission

        self.position_opened_at = time.time()
        self.was_position_open = True

        entry_price = self._resolve_entry_price(position, order_response, price)
        position = replace(
            position,
            entry_price=entry_price,
            opened_at=self.position_opened_at,
        )

        post_fill = self._revalidate_entry_protection(
            action=action,
            price=price,
            entry_price=entry_price,
            position=position,
            trade_id=trade_id,
            entry_client_order_id=entry_client_order_id,
            prevalidated_protection=prevalidated_protection,
        )
        if isinstance(post_fill, PositionExecutionResult):
            return post_fill
        effective_protection = post_fill

        self._save_state_for_position(
            position=position,
            trade_id=trade_id,
            entry_client_order_id=entry_client_order_id,
            operational_state=OperationalState.PROTECTIONS_PENDING,
        )

        self._record_journal(
            "position_opened",
            trade_id=trade_id,
            entry_client_order_id=entry_client_order_id,
            action=action.value,
            side=entry_side,
            symbol=self.config.symbol,
            quantity=position.quantity,
            entry_price=entry_price,
            order_response=order_response,
        )
        self.trade_audit_recorder.record_open(
            symbol=self.config.symbol,
            trade_id=trade_id,
            trade_plan_id=trade_plan_id,
            entry_time=str(self.position_opened_at),
            entry_reason=reason,
            entry_price=entry_price,
            quantity=position.quantity,
            direction=position.direction.value,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
            diagnostics={
                "source": "FUTURES_ENGINE",
                "entry_client_order_id": entry_client_order_id,
                "quantity_source": quantity_source,
                "planned_quantity": planned_quantity,
                "planned_exit_source": planned_exit_source,
            },
        )

        return self._protect_entry(
            action=action,
            position=position,
            trade_id=trade_id,
            entry_client_order_id=entry_client_order_id,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
            trade_plan_id=trade_plan_id,
            planned_exit_source=planned_exit_source,
            effective_protection=effective_protection,
            entry_price=entry_price,
            order_response=order_response,
        )

    def _create_protection_orders(
        self,
        position: FuturesPosition,
        reason: str,
        trade_id: str | None,
        *,
        planned_stop_price: Decimal | None = None,
        planned_take_profit_price: Decimal | None = None,
        trade_plan_id: str | None = None,
        planned_exit_source: str | None = None,
        expected_protection: ExpectedProtection | None = None,
        existing_stop_order: dict | None = None,
        existing_take_order: dict | None = None,
        repair_key: str | None = None,
    ) -> dict:
        with self._protection_operation_lock:
            return self._create_protection_orders_locked(
                position=position,
                reason=reason,
                trade_id=trade_id,
                planned_stop_price=planned_stop_price,
                planned_take_profit_price=planned_take_profit_price,
                trade_plan_id=trade_plan_id,
                planned_exit_source=planned_exit_source,
                expected_protection=expected_protection,
                existing_stop_order=existing_stop_order,
                existing_take_order=existing_take_order,
                repair_key=repair_key,
            )

    def _create_protection_orders_locked(
        self,
        position: FuturesPosition,
        reason: str,
        trade_id: str | None,
        *,
        planned_stop_price: Decimal | None,
        planned_take_profit_price: Decimal | None,
        trade_plan_id: str | None,
        planned_exit_source: str | None,
        expected_protection: ExpectedProtection | None,
        existing_stop_order: dict | None,
        existing_take_order: dict | None,
        repair_key: str | None,
    ) -> dict:
        if self.rules is None:
            raise RuntimeError("Symbol rules not loaded")

        if not position.is_open:
            raise ValueError("Cannot create protection for a flat position")

        if position.entry_price <= 0:
            raise ValueError("Cannot create protection with invalid entry_price")

        current_position = self._refresh_position_for_protection_operation(
            expected_position=position,
            stage="before_protection_creation",
            trade_id=trade_id,
        )
        if current_position is None:
            return self._protection_skipped_result(
                reason="POSITION_FLAT_BEFORE_PROTECTION_CREATION",
                trade_id=trade_id,
                repair_key=repair_key,
            )
        position = current_position

        expected = expected_protection or build_expected_protection(
            position=position,
            risk_manager=self.risk_manager,
            rules=self.rules,
            working_type=self.config.protection_working_type.value,
            exit_model=self.exit_model,
            planned_stop_price=(planned_stop_price if self.config.dynamic_exit_enabled else None),
            planned_take_profit_price=(
                planned_take_profit_price if self.config.dynamic_exit_enabled else None
            ),
            trade_plan_id=(trade_plan_id if self.config.dynamic_exit_enabled else None),
        )
        if expected.working_type != self.config.protection_working_type.value:
            raise ValueError("Expected protection working type does not match engine configuration")

        if expected.warning:
            self.logger.warning("Protection model warning: %s", expected.warning)
            self._record_journal(
                "protection_model_warning",
                trade_id=trade_id,
                reason=reason,
                model=expected.model,
                source=expected.source,
                trade_plan_id=expected.trade_plan_id,
                warning=expected.warning,
            )

        self._assert_protection_triggers_not_breached(position=position, expected=expected)

        stop_client_algo_id = (
            self._extract_algo_client_id(existing_stop_order)
            if existing_stop_order is not None
            else self._protection_client_algo_id("STOP", repair_key)
        )
        take_client_algo_id = (
            self._extract_algo_client_id(existing_take_order)
            if existing_take_order is not None
            else self._protection_client_algo_id("TAKE", repair_key)
        )
        created_stop = False
        created_take = False

        self.logger.info(
            "Protections calculated | trade_id=%s | entry=%s | stop=%s | take=%s | source=%s",
            trade_id,
            position.entry_price,
            expected.stop_price,
            expected.take_profit_price,
            expected.source,
        )

        stop_response = existing_stop_order
        take_response = existing_take_order
        if getattr(self.client, "spot_mode", False):
            if bool(existing_stop_order) != bool(existing_take_order):
                self._pause_engine("spot_oco_incomplete", trade_id=trade_id)
                raise EnginePaused("Spot OCO incompleta; reconciliacao necessaria")
            if existing_stop_order is None:
                list_client_id = self._protection_client_algo_id("OCO", repair_key)
                if self.config.mutations_enabled:
                    oco_response = self.client.new_spot_oco_order(
                        symbol=self.config.symbol,
                        quantity=format_decimal(position.quantity),
                        stop_price=format_decimal(expected.stop_price),
                        take_price=format_decimal(expected.take_profit_price),
                        list_client_order_id=list_client_id,
                        stop_client_order_id=stop_client_algo_id,
                        take_client_order_id=take_client_algo_id,
                    )
                else:
                    oco_response = {"dry_run": True, "listClientOrderId": list_client_id}
                self._record_journal(
                    "spot_oco_submitted", trade_id=trade_id, response=oco_response,
                    stop_client_algo_id=stop_client_algo_id,
                    take_client_algo_id=take_client_algo_id,
                )
                stop_response = oco_response
                take_response = oco_response
                created_stop = True
                created_take = True
        else:
            if existing_stop_order is None:
                stop_response = self._submit_protection_order(
                    purpose="stop",
                    client_algo_id=stop_client_algo_id,
                    expected=expected,
                    trade_id=trade_id,
                    repair_key=repair_key,
                )
                if stop_response is None:
                    return self._protection_skipped_result(
                        reason="POSITION_FLAT_DURING_STOP_SUBMISSION",
                        trade_id=trade_id,
                        repair_key=repair_key,
                    )
                created_stop = True

            current_position = self._refresh_position_for_protection_operation(
                expected_position=position,
                stage="between_stop_and_take",
                trade_id=trade_id,
            )
            if current_position is None:
                return self._protection_skipped_result(
                    reason="POSITION_FLAT_BETWEEN_STOP_AND_TAKE",
                    trade_id=trade_id,
                    repair_key=repair_key,
                )
            position = current_position
            self._assert_protection_triggers_not_breached(position=position, expected=expected)

            if existing_take_order is None:
                take_response = self._submit_protection_order(
                    purpose="take",
                    client_algo_id=take_client_algo_id,
                    expected=expected,
                    trade_id=trade_id,
                    repair_key=repair_key,
                )
                if take_response is None:
                    return self._protection_skipped_result(
                        reason="POSITION_FLAT_DURING_TAKE_SUBMISSION",
                        trade_id=trade_id,
                        repair_key=repair_key,
                    )
                created_take = True

        current_position = self._refresh_position_for_protection_operation(
            expected_position=position,
            stage="after_protection_submission",
            trade_id=trade_id,
        )
        if current_position is None:
            return self._protection_skipped_result(
                reason="POSITION_FLAT_AFTER_PROTECTION_SUBMISSION",
                trade_id=trade_id,
                repair_key=repair_key,
            )
        position = current_position

        protection_state = self._protection_state_payload(
            expected,
            fallback_trade_plan_id=trade_plan_id,
        )
        effective_trade_plan_id = protection_state["trade_plan_id"]
        self._save_state_for_position(
            position=position,
            trade_id=trade_id or self.active_trade_id or self._new_trade_id("STATE"),
            entry_client_order_id=(
                self.runtime_state.entry_client_order_id if self.runtime_state is not None else None
            ),
            stop_client_algo_id=stop_client_algo_id,
            take_client_algo_id=take_client_algo_id,
            operational_state=OperationalState.POSITION_OPEN_PROTECTED,
            protection_warning=expected.warning,
            **protection_state,
        )

        result = {
            "status": "PROTECTED",
            "repair_key": repair_key,
            "created_stop": created_stop,
            "created_take": created_take,
            "stop_client_algo_id": stop_client_algo_id,
            "take_client_algo_id": take_client_algo_id,
            "stop_price": expected.stop_price,
            "take_profit_price": expected.take_profit_price,
            "protection_model": expected.model,
            "protection_source": expected.source,
            "planned_exit_source": planned_exit_source,
            "trade_plan_id": effective_trade_plan_id,
            "protection_warning": expected.warning,
            "stop_response": stop_response,
            "take_response": take_response,
        }

        self._record_journal(
            "protections_created",
            trade_id=trade_id,
            reason=reason,
            symbol=self.config.symbol,
            direction=position.direction.value,
            quantity=position.quantity,
            entry_price=position.entry_price,
            **result,
        )
        observe_event(
            "protection_order_created",
            component=__name__,
            category="execution",
            channel=EventChannel.AUDIT,
            symbol=self.config.symbol,
            trade_id=trade_id,
            trade_plan_id=effective_trade_plan_id,
            stop_client_algo_id=stop_client_algo_id,
            take_client_algo_id=take_client_algo_id,
            stop_price=expected.stop_price,
            take_profit_price=expected.take_profit_price,
            protection_model=expected.model,
            protection_source=expected.source,
            planned_exit_source=planned_exit_source,
            protection_warning=expected.warning,
            repair_key=repair_key,
            created_stop=created_stop,
            created_take=created_take,
        )
        if (
            planned_exit_source == STRATEGY_METADATA_EXIT_SOURCE
            and expected.source == PROTECTION_SOURCE_TRADE_PLAN
        ):
            observe_event(
                "protection_order_created_from_strategy_exit",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=self.config.symbol,
                trade_id=trade_id,
                trade_plan_id=effective_trade_plan_id,
                stop_client_algo_id=stop_client_algo_id,
                take_client_algo_id=take_client_algo_id,
                stop_price=expected.stop_price,
                take_profit_price=expected.take_profit_price,
                protection_model=expected.model,
                protection_source=expected.source,
                planned_exit_source=planned_exit_source,
            )
        self.trade_audit_recorder.record_protected(
            symbol=self.config.symbol,
            trade_id=trade_id,
            trade_plan_id=effective_trade_plan_id,
            entry_time=str(position.opened_at or self.position_opened_at or ""),
            entry_price=position.entry_price,
            quantity=position.quantity,
            direction=position.direction.value,
            stop_price=expected.stop_price,
            take_profit_price=expected.take_profit_price,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
            protection_source=expected.source,
            protection_model=expected.model,
            stop_client_algo_id=stop_client_algo_id,
            take_client_algo_id=take_client_algo_id,
            diagnostics={
                "source": "FUTURES_ENGINE",
                "reason": reason,
                "repair_key": repair_key,
                "created_stop": created_stop,
                "created_take": created_take,
                "protection_warning": expected.warning,
                "planned_exit_source": planned_exit_source,
                "stop_response": stop_response,
                "take_response": take_response,
            },
        )
        return result

    def _submit_protection_order(
        self,
        *,
        purpose: str,
        client_algo_id: str,
        expected: ExpectedProtection,
        trade_id: str | None,
        repair_key: str | None,
    ) -> dict | None:
        order_type = "STOP_MARKET" if purpose == "stop" else "TAKE_PROFIT_MARKET"
        trigger_price = expected.stop_price if purpose == "stop" else expected.take_profit_price
        try:
            return self._submit_algo_order(
                purpose=purpose,
                symbol=self.config.symbol,
                side=expected.close_side,
                type=order_type,
                triggerPrice=format_decimal(trigger_price),
                closePosition="true",
                workingType=self.config.protection_working_type.value,
                algoType="CONDITIONAL",
                clientAlgoId=client_algo_id,
            )
        except BinanceFuturesAPIError as error:
            if error.code != -4509:
                raise
            current_position = self._get_position()
            if current_position.is_open:
                raise
            self._record_journal(
                "protection_submission_reconciled_position_flat",
                trade_id=trade_id,
                repair_key=repair_key,
                purpose=purpose,
                client_algo_id=client_algo_id,
                error_code=error.code,
                error_message=error.message,
            )
            self._handle_flat_position_during_protection_operation(
                stage=f"{purpose}_submission_error",
                trade_id=trade_id,
            )
            return None

    def _assert_protection_triggers_not_breached(
        self,
        *,
        position: FuturesPosition,
        expected: ExpectedProtection,
    ) -> None:
        try:
            current_trigger_price = self._get_trigger_reference_price()
            breached = breached_protection_triggers(
                position=position,
                expected=expected,
                current_price=current_trigger_price,
            )
            if breached:
                self.logger.warning(
                    "Protection trigger already breached before submission | "
                    "working_type=%s | current=%s | stop=%s | take=%s | breached=%s",
                    self.config.protection_working_type.value,
                    current_trigger_price,
                    expected.stop_price,
                    expected.take_profit_price,
                    breached,
                )
                raise ProtectionTriggerAlreadyBreached(
                    f"current={current_trigger_price} breached={breached}"
                )
        except ProtectionTriggerAlreadyBreached:
            raise
        except Exception as preflight_error:
            self.logger.warning(
                "Protection pre-flight price check failed, proceeding: %s", preflight_error
            )

    def _refresh_position_for_protection_operation(
        self,
        *,
        expected_position: FuturesPosition,
        stage: str,
        trade_id: str | None,
    ) -> FuturesPosition | None:
        if not self.config.mutations_enabled:
            return expected_position

        if trade_id and self.active_trade_id and trade_id != self.active_trade_id:
            self._record_journal(
                "protection_operation_aborted_stale_trade",
                stage=stage,
                expected_trade_id=trade_id,
                active_trade_id=self.active_trade_id,
            )
            self._pause_engine(
                "protection_operation_stale_trade",
                stage=stage,
                expected_trade_id=trade_id,
                active_trade_id=self.active_trade_id,
            )
            raise EnginePaused("Protection operation targets a stale trade")

        current_position = self._get_position()
        if not current_position.is_open:
            self._handle_flat_position_during_protection_operation(
                stage=stage,
                trade_id=trade_id,
            )
            return None

        if current_position.direction is not expected_position.direction:
            self._record_journal(
                "protection_operation_aborted_position_changed",
                stage=stage,
                trade_id=trade_id,
                expected_direction=expected_position.direction.value,
                current_direction=current_position.direction.value,
                expected_quantity=expected_position.quantity,
                current_quantity=current_position.quantity,
            )
            self._pause_engine(
                "protection_position_direction_changed",
                stage=stage,
                trade_id=trade_id,
                expected_direction=expected_position.direction.value,
                current_direction=current_position.direction.value,
            )
            raise EnginePaused("Position direction changed during protection operation")

        if current_position.quantity > expected_position.quantity:
            self._record_journal(
                "protection_operation_aborted_exposure_increased",
                stage=stage,
                trade_id=trade_id,
                expected_quantity=expected_position.quantity,
                current_quantity=current_position.quantity,
            )
            self._pause_engine(
                "protection_position_exposure_increased",
                stage=stage,
                trade_id=trade_id,
                expected_quantity=expected_position.quantity,
                current_quantity=current_position.quantity,
            )
            raise EnginePaused("Position exposure increased during protection operation")

        if current_position.quantity < expected_position.quantity:
            self._record_journal(
                "protection_position_reduced_during_operation",
                stage=stage,
                trade_id=trade_id,
                expected_quantity=expected_position.quantity,
                current_quantity=current_position.quantity,
            )

        return replace(
            current_position,
            opened_at=expected_position.opened_at or self.position_opened_at,
        )

    def _handle_flat_position_during_protection_operation(
        self,
        *,
        stage: str,
        trade_id: str | None,
    ) -> None:
        algo_result = self._safe_get_open_algo_orders()
        robot_algo_orders = (
            self._filter_robot_algo_orders(algo_result.orders) if algo_result.ok else []
        )
        if robot_algo_orders and self.config.mutations_enabled:
            self._cancel_robot_open_orders(
                normal_orders=[],
                algo_orders=robot_algo_orders,
                reason=f"position_flat_during_protection_{stage}",
            )
        self._record_journal(
            "protection_operation_skipped_position_flat",
            stage=stage,
            trade_id=trade_id,
            orphan_algo_orders=len(robot_algo_orders),
            algo_query_error=algo_result.error,
        )
        observe_event(
            "protection_repair_skipped_position_flat",
            component=__name__,
            category="execution",
            channel=EventChannel.AUDIT,
            symbol=self.config.symbol,
            trade_id=trade_id,
            stage=stage,
            orphan_algo_orders=len(robot_algo_orders),
        )
        self.logger.info(
            "Protection operation skipped because position is flat | stage=%s | trade_id=%s",
            stage,
            trade_id,
        )
        self._clear_runtime_state()

    def _protection_client_algo_id(self, purpose: str, repair_key: str | None) -> str:
        if repair_key is None:
            return self._new_client_order_id(purpose)
        digest = hashlib.sha256(f"{repair_key}:{purpose}".encode()).hexdigest()[:12]
        return f"{self._normalized_order_prefix()}_{purpose}_R_{digest.upper()}"[:36]

    def _build_protection_repair_key(
        self,
        *,
        trade_id: str | None,
        position: FuturesPosition,
        expected: ExpectedProtection,
    ) -> str:
        payload = "|".join(
            (
                trade_id or "NO_TRADE",
                position.symbol,
                position.direction.value,
                str(position.quantity),
                str(expected.stop_price),
                str(expected.take_profit_price),
                expected.working_type,
                expected.source,
                expected.trade_plan_id or "NO_PLAN",
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _protection_skipped_result(
        *,
        reason: str,
        trade_id: str | None,
        repair_key: str | None,
    ) -> dict:
        return {
            "status": "SKIPPED_POSITION_FLAT",
            "reason": reason,
            "trade_id": trade_id,
            "repair_key": repair_key,
        }

    def _prepare_close(
        self, position: FuturesPosition, reason: str
    ) -> tuple[FuturesPosition, str] | PositionExecutionResult:
        if self.config.mutations_enabled:
            try:
                pending_close = self.close_intent_store.load()
            except (OSError, ValueError) as error:
                self._pause_engine("close_intent_read_failed", error=str(error))
                return PositionExecutionResult.blocked(
                    "CLOSE_INTENT_READ_FAILED", diagnostics={"error": str(error)}
                )
            if pending_close is not None:
                self._pause_engine(
                    "close_intent_pending",
                    client_order_id=pending_close.lifecycle.client_order_id,
                    lifecycle_state=pending_close.lifecycle.state.value,
                )
                return PositionExecutionResult.blocked(
                    "CLOSE_INTENT_PENDING",
                    order_ids=(pending_close.lifecycle.client_order_id,),
                    diagnostics={"lifecycle_state": pending_close.lifecycle.state.value},
                )

        original_position = position
        pending_trade_id = self.active_trade_id or self._new_trade_id("CLOSE")
        if self.config.mutations_enabled:
            current_position = self._sync_local_position_state(self._get_position())
            if not current_position.is_open:
                self.logger.info(
                    "Close skipped: position already flat | trade_id=%s | reason=%s",
                    pending_trade_id,
                    reason,
                )
                self._record_journal(
                    "position_close_skipped_already_flat",
                    trade_id=pending_trade_id,
                    reason=reason,
                    requested_position=original_position,
                    current_position=current_position,
                )
                self._record_operational_state(
                    OperationalState.FLAT_CONFIRMED,
                    reason="already_flat_before_close",
                    trade_id=pending_trade_id,
                )
                if self.config.cancel_open_orders_when_flat:
                    self._safe_cancel_all_open_orders(reason="already_flat_before_close")
                return PositionExecutionResult.skipped(
                    "ALREADY_FLAT_BEFORE_CLOSE",
                    diagnostics={
                        "trade_id": pending_trade_id,
                        "requested_position": original_position,
                        "current_position": current_position,
                    },
                )

            if current_position.direction != original_position.direction:
                self._record_journal(
                    "position_close_direction_changed",
                    trade_id=pending_trade_id,
                    reason=reason,
                    requested_position=original_position,
                    current_position=current_position,
                )
                self._pause_engine(
                    "position_close_direction_changed",
                    trade_id=pending_trade_id,
                    requested_position=original_position,
                    current_position=current_position,
                )
                return PositionExecutionResult.blocked(
                    "POSITION_DIRECTION_CHANGED_BEFORE_CLOSE",
                    diagnostics={
                        "trade_id": pending_trade_id,
                        "requested_position": original_position,
                        "current_position": current_position,
                    },
                )

            if current_position.quantity != original_position.quantity:
                self._record_journal(
                    "position_close_revalidated",
                    reason=reason,
                    requested_position=original_position,
                    current_position=current_position,
                    action="using_current_position_quantity",
                )
                position = current_position
        elif not position.is_open:
            self._record_journal(
                "position_close_skipped_position_not_open",
                trade_id=pending_trade_id,
                reason=reason,
                requested_position=original_position,
            )
            return PositionExecutionResult.skipped(
                "POSITION_NOT_OPEN",
                diagnostics={
                    "trade_id": pending_trade_id,
                    "requested_position": original_position,
                },
            )

        return position, pending_trade_id

    def _persist_close_intent(
        self,
        *,
        close_side: str,
        position: FuturesPosition,
        trade_id: str,
        close_client_order_id: str,
    ) -> PositionExecutionResult | None:
        if not self.config.mutations_enabled:
            return None
        try:
            self.close_intent_store.begin(
                symbol=self.config.symbol,
                side=close_side,
                quantity=position.quantity,
                trade_id=trade_id,
                client_order_id=close_client_order_id,
            )
            self.close_intent_store.advance(OrderState.SUBMITTING)
        except (OSError, ValueError) as error:
            self._pause_engine(
                "close_intent_persist_failed",
                trade_id=trade_id,
                client_order_id=close_client_order_id,
                error=str(error),
            )
            return PositionExecutionResult.failed(
                "CLOSE_INTENT_PERSIST_FAILED",
                order_ids=(close_client_order_id,),
                diagnostics={"trade_id": trade_id, "error": str(error)},
            )
        return None

    def _request_close(
        self,
        *,
        position: FuturesPosition,
        reason: str,
        pending_trade_id: str,
    ) -> tuple[str, str, str]:
        close_side = "SELL" if position.direction == PositionDirection.LONG else "BUY"
        close_client_order_id = self._new_client_order_id("CLOSE")
        trade_id = pending_trade_id
        self._record_operational_state(
            OperationalState.CLOSE_PENDING,
            reason=reason,
            trade_id=trade_id,
            position=position,
        )

        self.logger.info(
            "Closing position | trade_id=%s | side=%s | qty=%s | direction=%s",
            trade_id,
            close_side,
            position.quantity,
            position.direction.value,
        )

        self._record_journal(
            "position_close_requested",
            trade_id=trade_id,
            reason=reason,
            symbol=self.config.symbol,
            direction=position.direction.value,
            quantity=position.quantity,
            entry_price=position.entry_price,
            close_side=close_side,
            close_client_order_id=close_client_order_id,
        )

        return close_side, close_client_order_id, trade_id

    def _submit_close(
        self,
        *,
        position: FuturesPosition,
        reason: str,
        pending_trade_id: str,
    ) -> tuple[str, str, dict] | PositionExecutionResult:
        close_side, close_client_order_id, trade_id = self._request_close(
            position=position, reason=reason, pending_trade_id=pending_trade_id
        )

        persist_failure = self._persist_close_intent(
            close_side=close_side,
            position=position,
            trade_id=trade_id,
            close_client_order_id=close_client_order_id,
        )
        if persist_failure is not None:
            return persist_failure

        try:
            order_response = self._submit_order(
                purpose="close",
                symbol=self.config.symbol,
                side=close_side,
                type="MARKET",
                quantity=format_decimal(position.quantity),
                reduceOnly="true",
                newOrderRespType="RESULT",
                newClientOrderId=close_client_order_id,
            )
        except UnknownOrderStatus as error:
            if self.config.mutations_enabled and (
                not self._advance_close_intent(OrderState.UNKNOWN)
                or not self._advance_close_intent(OrderState.RECONCILING)
            ):
                return PositionExecutionResult.uncertain(
                    "CLOSE_INTENT_TRANSITION_FAILED",
                    order_ids=(close_client_order_id,),
                    diagnostics={"trade_id": trade_id},
                )
            reconciled_order = self._reconcile_close_order_unknown(
                error=error,
                trade_id=trade_id,
                close_client_order_id=close_client_order_id,
                quantity=position.quantity,
            )
            if reconciled_order is None:
                self._advance_close_intent(OrderState.UNKNOWN)
                return PositionExecutionResult.uncertain(
                    "CLOSE_ORDER_STATUS_UNKNOWN",
                    order_ids=(close_client_order_id,),
                    diagnostics={"trade_id": trade_id, "error": str(error.original_error)},
                )
            order_response = reconciled_order
        if self.config.mutations_enabled:
            try:
                pending_close = self.close_intent_store.load()
            except (OSError, ValueError) as error:
                self._pause_engine("close_intent_read_failed", error=str(error))
                return PositionExecutionResult.uncertain(
                    "CLOSE_INTENT_READ_FAILED",
                    order_ids=(close_client_order_id,),
                    diagnostics={"trade_id": trade_id, "error": str(error)},
                )
            if pending_close is None:
                self._pause_engine("close_intent_missing_after_dispatch", trade_id=trade_id)
                return PositionExecutionResult.uncertain(
                    "CLOSE_INTENT_MISSING_AFTER_DISPATCH",
                    order_ids=(close_client_order_id,),
                    diagnostics={"trade_id": trade_id},
                )
            pending_state = pending_close.lifecycle.state
            if pending_state is OrderState.SUBMITTING and not self._advance_close_intent(
                OrderState.ACKNOWLEDGED
            ):
                return PositionExecutionResult.uncertain(
                    "CLOSE_INTENT_TRANSITION_FAILED",
                    order_ids=(close_client_order_id,),
                    diagnostics={"trade_id": trade_id},
                )

        if not self.config.mutations_enabled:
            self._record_journal(
                "position_close_dry_run",
                trade_id=trade_id,
                close_client_order_id=close_client_order_id,
                order_response=order_response,
            )
            return PositionExecutionResult.dry_run(
                "DRY_RUN_CLOSE_NOT_SENT",
                order_ids=(close_client_order_id,),
                diagnostics={"trade_id": trade_id, "order_response": order_response},
            )

        if not self._close_order_fully_filled(order_response, position.quantity):
            if self.config.mutations_enabled:
                self._advance_close_intent(OrderState.UNKNOWN)
            self._record_journal(
                "close_order_fill_not_confirmed",
                trade_id=trade_id,
                close_client_order_id=close_client_order_id,
                expected_quantity=position.quantity,
                order_response=order_response,
            )
            self._pause_engine(
                "position_close_not_confirmed",
                trade_id=trade_id,
                close_client_order_id=close_client_order_id,
                order_response=order_response,
            )
            return PositionExecutionResult.uncertain(
                "CLOSE_ORDER_FILL_NOT_CONFIRMED",
                order_ids=(close_client_order_id,),
                diagnostics={"trade_id": trade_id, "order_response": order_response},
            )

        if self.config.mutations_enabled and not self._advance_close_intent(OrderState.FILLED):
            return PositionExecutionResult.uncertain(
                "CLOSE_INTENT_TRANSITION_FAILED",
                order_ids=(close_client_order_id,),
                diagnostics={"trade_id": trade_id},
            )

        return close_client_order_id, trade_id, order_response

    def _confirm_flat_after_close(
        self,
        *,
        trade_id: str,
        close_client_order_id: str,
        order_response: dict,
    ) -> PositionExecutionResult | None:
        confirmation_error = None
        try:
            flat_confirmed = self._wait_until_flat()
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as error:
            flat_confirmed = False
            confirmation_error = str(error)

        if not flat_confirmed:
            try:
                current_position = self._get_position()
            except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as error:
                current_position = None
                confirmation_error = str(error)
            self._record_operational_state(
                OperationalState.WAITING_FLAT_CONFIRMATION,
                reason="position_close_not_confirmed",
                trade_id=trade_id,
                position=current_position,
            )
            self._record_journal(
                "position_close_not_confirmed",
                trade_id=trade_id,
                close_client_order_id=close_client_order_id,
                order_response=order_response,
                current_position=current_position,
                confirmation_error=confirmation_error,
            )
            self._pause_engine(
                "position_close_not_confirmed",
                trade_id=trade_id,
                close_client_order_id=close_client_order_id,
                confirmation_error=confirmation_error,
            )
            return PositionExecutionResult.uncertain(
                "POSITION_CLOSE_NOT_CONFIRMED",
                order_ids=(close_client_order_id,),
                diagnostics={
                    "trade_id": trade_id,
                    "order_response": order_response,
                    "current_position": current_position,
                    "confirmation_error": confirmation_error,
                },
            )

        return None

    def _close_position(self, position: FuturesPosition, reason: str) -> PositionExecutionResult:
        preparation = self._prepare_close(position, reason)
        if isinstance(preparation, PositionExecutionResult):
            return preparation
        position, pending_trade_id = preparation

        if getattr(self.client, "spot_mode", False) and self.config.mutations_enabled:
            if not self._safe_cancel_all_open_orders(reason="spot_unlock_before_close"):
                self._pause_engine("spot_protection_cancel_unconfirmed")
                return PositionExecutionResult.blocked("SPOT_PROTECTION_CANCEL_UNCONFIRMED")
            remaining_protections = self._safe_get_open_algo_orders()
            if not remaining_protections.ok or remaining_protections.orders:
                self._pause_engine("spot_protection_still_open")
                return PositionExecutionResult.blocked("SPOT_PROTECTION_STILL_OPEN")
            account = self.client.account_info()
            base_asset = self.config.symbol.removesuffix("USDT")
            free_quantity = next(
                (
                    Decimal(str(asset.get("availableBalance", "0")))
                    for asset in account.get("assets", []) if asset.get("asset") == base_asset
                ),
                Decimal("0"),
            )
            negotiable = min(position.quantity, free_quantity)
            if negotiable <= 0:
                self._pause_engine("spot_base_balance_locked", free_quantity=str(free_quantity))
                return PositionExecutionResult.blocked("SPOT_BASE_BALANCE_LOCKED")
            if negotiable < position.quantity:
                self._record_journal(
                    "spot_partial_close_required", owned_quantity=position.quantity,
                    negotiable_quantity=negotiable,
                )
                position = replace(position, quantity=negotiable)

        submission = self._submit_close(
            position=position, reason=reason, pending_trade_id=pending_trade_id
        )
        if isinstance(submission, PositionExecutionResult):
            return submission
        close_client_order_id, trade_id, order_response = submission

        confirmation_failure = self._confirm_flat_after_close(
            trade_id=trade_id,
            close_client_order_id=close_client_order_id,
            order_response=order_response,
        )
        if confirmation_failure is not None:
            return confirmation_failure

        closed_at = time.time()
        pnl_result = self._resolve_live_close_pnl(
            position=position,
            order_response=order_response,
            close_client_order_id=close_client_order_id,
            closed_at=closed_at,
        )

        runtime_state_before_clear = self.runtime_state
        if runtime_state_before_clear is not None:
            outcome = build_trade_plan_outcome_from_live_payload(
                symbol=self.config.symbol,
                direction=position.direction,
                entry_price=position.entry_price,
                quantity=position.quantity,
                trade_plan_id=runtime_state_before_clear.trade_plan_id,
                active_trade_id=runtime_state_before_clear.active_trade_id,
                planned_stop_price=runtime_state_before_clear.planned_stop_price,
                planned_take_profit_price=runtime_state_before_clear.planned_take_profit_price,
                exit_reason=reason,
                pnl_result=pnl_result,
            )
            self._record_journal(
                "trade_plan_outcome_recorded",
                trade_id=trade_id,
                trade_plan_id=outcome.trade_plan_id,
                outcome_id=outcome.outcome_id,
                outcome_status=outcome.outcome_status,
                net_pnl=outcome.net_pnl,
                pnl_status=outcome.pnl_status,
                pnl_source=outcome.pnl_source,
                outcome=outcome,
            )
        else:
            outcome = None

        self.trade_audit_recorder.record_completed(
            symbol=self.config.symbol,
            trade_id=trade_id,
            trade_plan_id=(
                runtime_state_before_clear.trade_plan_id
                if runtime_state_before_clear is not None
                else None
            ),
            entry_time=str(
                runtime_state_before_clear.opened_at
                if runtime_state_before_clear is not None
                else position.opened_at or self.position_opened_at or ""
            ),
            exit_time=str(closed_at),
            exit_reason=reason,
            entry_price=position.entry_price,
            close_price=pnl_result.get("close_price_estimate"),
            quantity=position.quantity,
            direction=position.direction.value,
            planned_stop_price=(
                runtime_state_before_clear.planned_stop_price
                if runtime_state_before_clear is not None
                else None
            ),
            planned_take_profit_price=(
                runtime_state_before_clear.planned_take_profit_price
                if runtime_state_before_clear is not None
                else None
            ),
            gross_pnl=pnl_result.get("gross_pnl_estimate"),
            net_pnl=pnl_result.get("net_pnl"),
            pnl_status=(
                outcome.pnl_status if outcome is not None else pnl_result.get("pnl_status")
            ),
            pnl_source=(
                outcome.pnl_source if outcome is not None else pnl_result.get("pnl_source")
            ),
            diagnostics={
                "source": "FUTURES_ENGINE",
                "close_client_order_id": close_client_order_id,
                "pnl_result": pnl_result,
                "outcome_id": outcome.outcome_id if outcome is not None else None,
                "order_response": order_response,
            },
        )

        self._clear_runtime_state()
        self._record_operational_state(
            OperationalState.FLAT_CONFIRMED,
            reason=reason,
            trade_id=trade_id,
        )

        self._record_journal(
            "position_closed",
            trade_id=trade_id,
            close_client_order_id=close_client_order_id,
            order_response=order_response,
            **pnl_result,
        )

        self._safe_cancel_all_open_orders(reason="after_close_confirmed")
        if self.config.mutations_enabled and (
            not self._advance_close_intent(OrderState.CLOSED)
            or not self._clear_close_intent()
            or not self._complete_entry_after_confirmed_close()
        ):
            return PositionExecutionResult.partial(
                "CLOSE_INTENT_FINALIZATION_FAILED",
                order_ids=(close_client_order_id,),
                diagnostics={"trade_id": trade_id},
            )
        return PositionExecutionResult.confirmed(
            "POSITION_CLOSE_CONFIRMED",
            order_ids=(close_client_order_id,),
            diagnostics={
                "trade_id": trade_id,
                "closed_position": position,
                "order_response": order_response,
                "pnl_result": pnl_result,
            },
        )

    def _resolve_live_close_pnl(
        self,
        *,
        position: FuturesPosition,
        order_response: dict,
        close_client_order_id: str,
        closed_at: float,
    ) -> dict:
        close_price = self._extract_average_order_price(order_response)
        if close_price is None and self.config.mutations_enabled:
            order_data, _error = self._query_order_for_reconcile(close_client_order_id)
            if order_data is not None:
                close_price = self._extract_average_order_price(order_data)

        gross_estimate = (
            _estimate_gross_close_pnl(position, close_price) if close_price is not None else None
        )

        income_pnl = self._income_pnl_components_from_history(
            opened_at=(
                position.opened_at
                or (
                    self.runtime_state.opened_at
                    if self.runtime_state is not None
                    else self.position_opened_at
                )
            ),
            closed_at=closed_at,
        )
        if income_pnl is not None:
            result = {
                **income_pnl,
                "pnl_status": "confirmed_income_history",
                "pnl_source": "binance_income_history",
            }
            if close_price is not None:
                result["close_price_estimate"] = close_price
            if gross_estimate is not None:
                result["gross_pnl_estimate"] = gross_estimate
            return result

        result = {
            "net_pnl": None,
            "pnl_status": "unknown_live_close",
            "pnl_source": "unavailable",
            "warning": (
                "Daily PnL may be incomplete: live close confirmed, "
                "but realized PnL was not reliably received from Binance."
            ),
        }

        if close_price is None:
            return result

        result.update(
            {
                "gross_pnl_estimate": gross_estimate,
                "close_price_estimate": close_price,
                "pnl_status": "unknown_live_close_gross_estimated",
                "pnl_source": "order_average_price_estimate",
            }
        )
        return result

    def _income_pnl_components_from_history(
        self,
        *,
        opened_at: float | None,
        closed_at: float,
    ) -> dict | None:
        window_start = opened_at if opened_at is not None else closed_at - 60.0
        try:
            rows = self.client.income_history(
                symbol=self.config.symbol,
                start_time=int((window_start - 5.0) * 1000),
                end_time=int((max(closed_at, time.time()) + 5.0) * 1000),
                limit=1000,
            )
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError, TypeError) as error:
            self._record_journal(
                "income_pnl_lookup_failed",
                symbol=self.config.symbol,
                reason=str(error),
            )
            return None

        components = {
            "REALIZED_PNL": Decimal("0"),
            "COMMISSION": Decimal("0"),
            "FUNDING_FEE": Decimal("0"),
        }
        component_rows = {name: 0 for name in components}
        for row in rows or []:
            if str(row.get("symbol", self.config.symbol)).upper() != self.config.symbol:
                continue
            income_type = str(row.get("incomeType", "REALIZED_PNL")).upper()
            if income_type not in components:
                continue
            try:
                components[income_type] += Decimal(str(row.get("income", "0")))
            except Exception:
                continue
            component_rows[income_type] += 1

        if component_rows["REALIZED_PNL"] == 0:
            return None

        net_pnl = sum(components.values(), Decimal("0"))
        return {
            "net_pnl": net_pnl,
            "realized_pnl": components["REALIZED_PNL"],
            "commission": components["COMMISSION"],
            "funding_fee": components["FUNDING_FEE"],
            "pnl_components": {
                "realized_pnl": components["REALIZED_PNL"],
                "commission": components["COMMISSION"],
                "funding_fee": components["FUNDING_FEE"],
                "rows": {
                    "realized_pnl": component_rows["REALIZED_PNL"],
                    "commission": component_rows["COMMISSION"],
                    "funding_fee": component_rows["FUNDING_FEE"],
                },
            },
        }

    def _realized_pnl_from_income_history(self, *, closed_at: float) -> Decimal | None:
        """Compatibility helper returning only realized price PnL."""
        components = self._income_pnl_components_from_history(
            opened_at=closed_at - 60.0,
            closed_at=closed_at,
        )
        if components is None:
            return None
        return components["realized_pnl"]

    @staticmethod
    def _extract_average_order_price(order: dict | None) -> Decimal | None:
        if not order:
            return None
        for key in ("avgPrice", "averagePrice", "avg_price"):
            raw = order.get(key)
            if raw in {None, ""}:
                continue
            try:
                value = Decimal(str(raw))
            except Exception:
                continue
            if value > 0:
                return value
        try:
            executed_qty = Decimal(str(order.get("executedQty", "0") or "0"))
            cum_quote = Decimal(
                str(
                    order.get(
                        "cumQuote",
                        order.get("cummulativeQuoteQty", order.get("cumQuoteQty", "0")),
                    )
                    or "0"
                )
            )
        except Exception:
            return None
        if executed_qty > 0 and cum_quote > 0:
            return cum_quote / executed_qty
        return None

    def _wait_for_position_after_entry(
        self,
        action: TradeAction,
        attempts: int = POSITION_CONFIRMATION_ATTEMPTS,
        delay_seconds: float = POSITION_CONFIRMATION_INTERVAL_SECONDS,
    ) -> FuturesPosition | None:
        expected_direction = (
            PositionDirection.LONG if action == TradeAction.OPEN_LONG else PositionDirection.SHORT
        )
        return run_polling(
            lambda: self._get_position(timeout=POSITION_CONFIRMATION_REQUEST_TIMEOUT),
            attempts=attempts,
            interval_seconds=delay_seconds,
            retry_if_result=lambda pos: not is_expected_position(pos, expected_direction),
            fallback=None,
            delay_before_first_attempt=True,
            retry_exceptions=(BinanceFuturesNetworkError,),
            policy_name="wait_for_position_after_entry",
            budget=POSITION_CONFIRMATION_BUDGET,
            budget_key=f"position-confirmation:{self.config.symbol}",
            max_time_seconds=POSITION_CONFIRMATION_MAX_TIME_SECONDS,
        )

    def _wait_until_flat(
        self,
        attempts: int = FLAT_CONFIRMATION_ATTEMPTS,
        delay_seconds: float = FLAT_CONFIRMATION_INTERVAL_SECONDS,
    ) -> bool:
        def _check_flat() -> FuturesPosition:
            pos = self._get_position(timeout=FLAT_CONFIRMATION_REQUEST_TIMEOUT)
            if pos.is_open:
                self.logger.info(
                    "Waiting for position to flatten | direction=%s | qty=%s | entry=%s",
                    pos.direction.value,
                    pos.quantity,
                    pos.entry_price,
                )
            return pos

        result = run_polling(
            _check_flat,
            attempts=attempts,
            interval_seconds=delay_seconds,
            retry_if_result=is_open_position,
            fallback=None,
            delay_before_first_attempt=True,
            retry_exceptions=(BinanceFuturesNetworkError,),
            policy_name="wait_until_flat",
            budget=POSITION_CONFIRMATION_BUDGET,
            budget_key=f"flat-confirmation:{self.config.symbol}",
            max_time_seconds=FLAT_CONFIRMATION_MAX_TIME_SECONDS,
        )
        if result is not None and not result.is_open:
            self.logger.info("Position flat confirmed")
            return True
        return False

    def _resolve_entry_price(
        self,
        position: FuturesPosition,
        order_response: dict,
        fallback_price: Decimal,
    ) -> Decimal:
        if position.entry_price > 0:
            return position.entry_price

        avg_price = order_response.get("avgPrice")
        if avg_price:
            return Decimal(str(avg_price))

        return fallback_price

    def _get_position(self, *, timeout: RequestTimeout | None = None) -> FuturesPosition:
        if timeout is None:
            positions = self.client.position_risk(self.config.symbol)
        else:
            positions = self.client.position_risk(self.config.symbol, timeout=timeout)

        for data in positions:
            amount = Decimal(str(data.get("positionAmt", "0")))

            if amount == 0:
                continue

            entry_price = Decimal(str(data.get("entryPrice", "0")))
            unrealized_pnl = Decimal(str(data.get("unRealizedProfit", "0")))

            if amount > 0:
                return FuturesPosition(
                    symbol=self.config.symbol,
                    direction=PositionDirection.LONG,
                    quantity=abs(amount),
                    entry_price=entry_price,
                    unrealized_pnl=unrealized_pnl,
                    opened_at=self.position_opened_at,
                )

            if amount < 0:
                return FuturesPosition(
                    symbol=self.config.symbol,
                    direction=PositionDirection.SHORT,
                    quantity=abs(amount),
                    entry_price=entry_price,
                    unrealized_pnl=unrealized_pnl,
                    opened_at=self.position_opened_at,
                )

        return self._empty_position()

    def _empty_position(self) -> FuturesPosition:
        return FuturesPosition(
            symbol=self.config.symbol,
            direction=PositionDirection.NONE,
            quantity=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            opened_at=None,
        )

    def _sync_local_position_state(self, position: FuturesPosition) -> FuturesPosition:
        if position.is_open:
            if self.runtime_state and self.runtime_state.symbol == position.symbol:
                self.position_opened_at = self.runtime_state.opened_at
                self.active_trade_id = self.runtime_state.active_trade_id

            if self.position_opened_at is None:
                self.position_opened_at = time.time()
                self.logger.info("Open position detected. Starting local timer.")

            self.was_position_open = True
            return replace(position, opened_at=self.position_opened_at)

        if self.was_position_open or self.runtime_state is not None:
            self.logger.info("Flat position detected.")
            self._record_external_position_close(reason="flat_position_detected")
            self._clear_runtime_state()

            if self.config.cancel_open_orders_when_flat:
                self._safe_cancel_all_open_orders(reason="flat_position_detected")

        return position

    def _attach_or_create_state_for_open_position(
        self,
        position: FuturesPosition,
    ) -> FuturesPosition:
        if (
            self.runtime_state is not None
            and self.runtime_state.symbol == position.symbol
            and self.runtime_state.direction == position.direction
        ):
            self.active_trade_id = self.runtime_state.active_trade_id
            self.position_opened_at = self.runtime_state.opened_at
        else:
            pending = self.entry_intent_store.load() if self.config.mutations_enabled else None
            self.active_trade_id = (
                pending.lifecycle.intent_id
                if pending is not None
                else self._new_trade_id("RECOVERY")
            )
            self.position_opened_at = time.time()
            self._save_state_for_position(
                position=replace(position, opened_at=self.position_opened_at),
                trade_id=self.active_trade_id,
                entry_client_order_id=(
                    pending.lifecycle.client_order_id if pending is not None else None
                ),
                operational_state=OperationalState.POSITION_OPEN_UNPROTECTED,
            )

        self.was_position_open = True
        return replace(position, opened_at=self.position_opened_at)

    def _get_current_price(self) -> Decimal:
        data = self.client.ticker_price(self.config.symbol)
        return Decimal(str(data["price"]))

    def _get_trigger_reference_price(self) -> Decimal:
        if self.config.protection_working_type.value == "MARK_PRICE":
            data = self.client.mark_price(self.config.symbol)
            return Decimal(str(data["markPrice"]))

        return self._get_current_price()

    @staticmethod
    def _position_data(position: FuturesPosition) -> dict:
        return {
            "symbol": position.symbol,
            "direction": position.direction.value,
            "quantity": str(position.quantity),
            "entry_price": str(position.entry_price),
            "unrealized_pnl": str(position.unrealized_pnl),
            "opened_at": position.opened_at,
        }

    def _audit_open_position_protections(
        self,
        position: FuturesPosition,
        reason: str,
    ) -> None:
        with self._protection_operation_lock:
            self._audit_open_position_protections_locked(position, reason)

    def _audit_open_position_protections_locked(
        self,
        position: FuturesPosition,
        reason: str,
    ) -> None:
        algo_result = self._safe_get_open_algo_orders()
        if not algo_result.ok:
            self._pause_engine(
                "protection_audit_query_failed",
                audit_reason=reason,
                algo_error=algo_result.error,
            )
            return

        robot_algo_orders = self._filter_robot_algo_orders(algo_result.orders)
        audit_result = self._audit_protection_orders(
            position=position,
            robot_algo_orders=robot_algo_orders,
            reason=reason,
        )

        if audit_result.ok:
            self._mark_position_protected(
                position,
                audit_result=audit_result,
                reason=reason,
            )
            return

        self._handle_unprotected_position(
            position=position,
            audit_result=audit_result,
            reason=reason,
            robot_algo_orders=robot_algo_orders,
        )

    def _audit_protection_orders(
        self,
        *,
        position: FuturesPosition,
        robot_algo_orders: list[dict],
        reason: str,
    ) -> ProtectionAuditResult:
        if self.rules is None:
            raise RuntimeError("Symbol rules not loaded")

        planned_stop_price = None
        planned_take_profit_price = None
        trade_plan_id = None
        if self.runtime_state is not None:
            # Always use the stored trigger prices regardless of protection source or
            # dynamic_exit_enabled. These are the actual prices placed on the exchange, so
            # the audit compares like-for-like instead of re-deriving from position.entry_price
            # (which can differ by 1 tick from the fill response used at placement time).
            planned_stop_price = self.runtime_state.planned_stop_price
            planned_take_profit_price = self.runtime_state.planned_take_profit_price
            if self.runtime_state.protection_source == "TRADE_PLAN":
                trade_plan_id = self.runtime_state.trade_plan_id

        audit_result = audit_protections(
            position=position,
            algo_orders=robot_algo_orders,
            risk_manager=self.risk_manager,
            rules=self.rules,
            working_type=self.config.protection_working_type.value,
            symbol=self.config.symbol,
            robot_id_prefix=f"{self._normalized_order_prefix()}_",
            exit_model=self.exit_model,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
            trade_plan_id=trade_plan_id,
        )

        self._record_journal(
            "protection_audit",
            reason=reason,
            ok=audit_result.ok,
            missing=audit_result.missing,
            invalid=audit_result.invalid,
            expected=audit_result.expected,
            stop_order=audit_result.stop_order,
            take_order=audit_result.take_order,
        )

        return audit_result

    def _handle_unprotected_position(
        self,
        *,
        position: FuturesPosition,
        audit_result: ProtectionAuditResult,
        reason: str,
        robot_algo_orders: list[dict],
    ) -> None:
        trade_id = self.active_trade_id or (
            self.runtime_state.active_trade_id if self.runtime_state is not None else None
        )
        current_position = self._refresh_position_for_protection_operation(
            expected_position=position,
            stage="before_protection_repair",
            trade_id=trade_id,
        )
        if current_position is None:
            return

        # Re-read protection orders after the position refresh. This closes the race in
        # which the audit sees a missing order while a previous repair is still becoming
        # visible on the exchange.
        fresh_algo_result = self._safe_get_open_algo_orders()
        if not fresh_algo_result.ok:
            self._pause_engine(
                "protection_repair_order_query_failed",
                audit_reason=reason,
                algo_error=fresh_algo_result.error,
            )
            return
        robot_algo_orders = self._filter_robot_algo_orders(fresh_algo_result.orders)
        audit_result = self._audit_protection_orders(
            position=current_position,
            robot_algo_orders=robot_algo_orders,
            reason=f"{reason}_repair_recheck",
        )
        if audit_result.ok:
            self._record_journal(
                "protection_auto_repair_skipped_already_healthy",
                reason=reason,
                trade_id=trade_id,
            )
            self._mark_position_protected(
                current_position,
                audit_result=audit_result,
                reason=f"{reason}_repair_recheck",
            )
            return

        policy = self.config.protection_policy
        self._record_operational_state(
            OperationalState.POSITION_OPEN_UNPROTECTED,
            reason=reason,
            position=current_position,
            missing=audit_result.missing,
            invalid=audit_result.invalid,
        )

        if policy is ProtectionPolicy.PAUSE:
            self._pause_engine(
                "protection_audit_failed",
                audit_reason=reason,
                missing=audit_result.missing,
                invalid=audit_result.invalid,
            )
            return

        if policy is ProtectionPolicy.CLOSE_ON_UNPROTECTED:
            self._record_journal(
                "protection_policy_close",
                reason=reason,
                missing=audit_result.missing,
                invalid=audit_result.invalid,
            )
            if not self.config.mutations_enabled:
                self._pause_engine(
                    "unprotected_position_close_blocked_by_dry_run",
                    audit_reason=reason,
                    missing=audit_result.missing,
                    invalid=audit_result.invalid,
                )
                return
            self._close_position(current_position, f"unprotected_{reason}")
            return

        if policy is not ProtectionPolicy.AUTO_REPAIR:
            raise ValueError(f"Invalid protection policy: {policy}")

        if not self.config.mutations_enabled:
            self._pause_engine(
                "protection_auto_repair_blocked_by_dry_run",
                audit_reason=reason,
                missing=audit_result.missing,
                invalid=audit_result.invalid,
            )
            return

        current_price = self._get_trigger_reference_price()
        breached = breached_protection_triggers(
            position=current_position,
            expected=audit_result.expected,
            current_price=current_price,
        )

        if breached:
            self._record_journal(
                "protection_trigger_already_breached",
                audit_reason=reason,
                current_price=current_price,
                breached=breached,
                missing=audit_result.missing,
                invalid=audit_result.invalid,
            )
            observe_event(
                "protection_trigger_breached_emergency_close",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                severity="ERROR",
                symbol=self.config.symbol,
                reason=reason,
                breached=breached,
                current_price=str(current_price),
            )
            close_result = self._close_position(
                current_position,
                f"protection_trigger_breached_{reason}",
            )
            if not close_result.position_changed:
                self._pause_engine(
                    "protection_trigger_already_breached",
                    audit_reason=reason,
                    current_price=current_price,
                    breached=breached,
                    missing=audit_result.missing,
                    invalid=audit_result.invalid,
                )
            return

        repair_key = self._build_protection_repair_key(
            trade_id=trade_id,
            position=current_position,
            expected=audit_result.expected,
        )

        # Invalid orders are removed as a set because the audit cannot safely associate
        # every validation error with a single exchange payload. Valid orders are kept
        # when only the opposite protection is missing.
        existing_stop_order = audit_result.stop_order
        existing_take_order = audit_result.take_order
        if audit_result.invalid and robot_algo_orders:
            self._cancel_robot_open_orders(
                normal_orders=[],
                algo_orders=robot_algo_orders,
                reason=f"repair_invalid_protections_{reason}",
            )
            existing_stop_order = None
            existing_take_order = None

        current_position = self._refresh_position_for_protection_operation(
            expected_position=current_position,
            stage="after_invalid_protection_cancellation",
            trade_id=trade_id,
        )
        if current_position is None:
            return

        self._record_journal(
            "protection_auto_repair",
            reason=reason,
            trade_id=trade_id,
            repair_key=repair_key,
            missing=audit_result.missing,
            invalid=audit_result.invalid,
            current_price=current_price,
            protection_source=audit_result.expected.source,
            trade_plan_id=audit_result.expected.trade_plan_id,
            planned_stop_price=audit_result.expected.stop_price,
            planned_take_profit_price=audit_result.expected.take_profit_price,
            preserve_existing_stop=existing_stop_order is not None,
            preserve_existing_take=existing_take_order is not None,
        )
        observe_event(
            "protection_repair_attempted",
            component=__name__,
            category="execution",
            channel=EventChannel.AUDIT,
            severity="WARNING",
            symbol=self.config.symbol,
            reason=reason,
            trade_id=trade_id,
            repair_key=repair_key,
            missing=audit_result.missing,
            invalid=audit_result.invalid,
            current_price=current_price,
            protection_source=audit_result.expected.source,
            trade_plan_id=audit_result.expected.trade_plan_id,
            preserve_existing_stop=existing_stop_order is not None,
            preserve_existing_take=existing_take_order is not None,
        )
        protection_state = self._protection_state_payload(
            audit_result.expected,
            fallback_trade_plan_id=(
                getattr(self.runtime_state, "trade_plan_id", None)
                if self.runtime_state is not None
                else None
            ),
        )
        self._save_state_for_position(
            position=current_position,
            trade_id=trade_id or self._new_trade_id("REPAIR"),
            entry_client_order_id=(
                self.runtime_state.entry_client_order_id if self.runtime_state is not None else None
            ),
            stop_client_algo_id=(
                self._extract_algo_client_id(existing_stop_order)
                if existing_stop_order is not None
                else None
            ),
            take_client_algo_id=(
                self._extract_algo_client_id(existing_take_order)
                if existing_take_order is not None
                else None
            ),
            operational_state=OperationalState.PROTECTIONS_PENDING,
            **protection_state,
        )
        try:
            self._create_protection_orders(
                position=current_position,
                reason=f"auto_repair_{reason}",
                trade_id=trade_id,
                planned_stop_price=audit_result.expected.stop_price,
                planned_take_profit_price=audit_result.expected.take_profit_price,
                trade_plan_id=audit_result.expected.trade_plan_id,
                expected_protection=audit_result.expected,
                existing_stop_order=existing_stop_order,
                existing_take_order=existing_take_order,
                repair_key=repair_key,
            )
        except ProtectionTriggerAlreadyBreached:
            observe_event(
                "protection_trigger_breached_emergency_close",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                severity="ERROR",
                symbol=self.config.symbol,
                reason=f"trigger_breached_during_repair_{reason}",
                trade_id=trade_id,
            )
            self._close_position(
                current_position, f"protection_trigger_breached_during_repair_{reason}"
            )
