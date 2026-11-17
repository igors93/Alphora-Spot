import re
import time
import uuid
from decimal import Decimal

from binance.futures_client import (
    BinanceFuturesAPIError,
    BinanceFuturesNetworkError,
)
from domain.models import FuturesPosition, PositionDirection, TradeAction
from domain.orders import OrderState
from execution.engine_mixin_host import EngineMixinHost
from execution.errors import (
    ORDER_NOT_FOUND_CODES,
    EnginePaused,
    UnknownOrderStatus,
    is_unknown_execution_status,
)
from futures.order_queries import OpenOrdersResult
from utils.events import EventChannel, observe_error, observe_event


def _order_is_terminal_without_fill(order_response: dict) -> bool:
    status = str(order_response.get("status") or "").upper()
    executed_qty = Decimal(str(order_response.get("executedQty") or "0"))
    return status in {"CANCELED", "EXPIRED", "REJECTED"} and executed_qty == 0


class EngineOrderMixin(EngineMixinHost):
    active_trade_id: str | None

    def _complete_entry_after_confirmed_close(self) -> bool:
        try:
            pending = self.entry_intent_store.load()
        except (OSError, ValueError) as error:
            self._pause_engine("entry_intent_read_failed", error=str(error))
            return False
        if pending is None:
            return True
        state = pending.lifecycle.state
        if state not in {
            OrderState.FILLED,
            OrderState.PARTIALLY_FILLED,
            OrderState.PROTECTION_FAILED,
            OrderState.PROTECTED,
        }:
            self._pause_engine(
                "entry_intent_unresolved_after_close",
                lifecycle_state=state.value,
                client_order_id=pending.lifecycle.client_order_id,
            )
            return False
        entry_order, query_error = self._query_order_for_reconcile(
            pending.lifecycle.client_order_id
        )
        if entry_order is None or str(entry_order.get("status") or "").upper() not in {
            "FILLED",
            "CANCELED",
            "EXPIRED",
        }:
            self._pause_engine(
                "entry_order_may_fill_after_close",
                client_order_id=pending.lifecycle.client_order_id,
                query_error=query_error,
                order_response=entry_order,
            )
            return False
        if state in {
            OrderState.FILLED,
            OrderState.PARTIALLY_FILLED,
        } and not self._advance_entry_intent(OrderState.PROTECTION_FAILED):
            return False
        if state is OrderState.PROTECTED:
            steps = (OrderState.CLOSING, OrderState.CLOSED)
        else:
            steps = (OrderState.EMERGENCY_CLOSE, OrderState.CLOSED)
        return (
            all(self._advance_entry_intent(step) for step in steps) and self._clear_entry_intent()
        )

    def _advance_close_intent(self, state: OrderState) -> bool:
        try:
            self.close_intent_store.advance(state)
        except (OSError, ValueError) as error:
            self._pause_engine(
                "close_intent_transition_failed",
                target_state=state.value,
                error=str(error),
            )
            return False
        return True

    def _clear_close_intent(self) -> bool:
        try:
            self.close_intent_store.clear()
        except OSError as error:
            self._pause_engine("close_intent_clear_failed", error=str(error))
            return False
        return True

    def _recover_pending_close_intent(self) -> bool:
        try:
            pending = self.close_intent_store.load()
        except (OSError, ValueError) as error:
            self._pause_engine("close_intent_read_failed", error=str(error))
            return False
        if pending is None:
            return True
        if pending.symbol != self.config.symbol:
            self._pause_engine(
                "close_intent_symbol_mismatch",
                pending_symbol=pending.symbol,
                configured_symbol=self.config.symbol,
            )
            return False
        if pending.lifecycle.state is OrderState.PERSISTED:
            return self._clear_close_intent()

        try:
            position = self._get_position()
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as error:
            self._pause_engine("close_intent_position_query_failed", error=str(error))
            return False
        order, query_error = self._query_order_for_reconcile(pending.lifecycle.client_order_id)
        if order is None:
            self._pause_engine(
                "close_intent_order_query_failed",
                client_order_id=pending.lifecycle.client_order_id,
                error=query_error,
            )
            return False
        if _order_is_terminal_without_fill(order) and position.is_open:
            return self._clear_close_intent()
        if not position.is_open and self._close_order_fully_filled(order, pending.quantity):
            state = pending.lifecycle.state
            if state is OrderState.CLOSED:
                return self._clear_close_intent() and self._complete_entry_after_confirmed_close()
            if state is OrderState.SUBMITTING:
                if not self._advance_close_intent(OrderState.ACKNOWLEDGED):
                    return False
            elif state is OrderState.UNKNOWN:
                if not self._advance_close_intent(OrderState.RECONCILING):
                    return False
            if state is not OrderState.FILLED and not self._advance_close_intent(OrderState.FILLED):
                return False
            return (
                self._advance_close_intent(OrderState.CLOSED)
                and self._clear_close_intent()
                and self._complete_entry_after_confirmed_close()
            )

        self._pause_engine(
            "close_intent_exchange_divergence",
            client_order_id=pending.lifecycle.client_order_id,
            lifecycle_state=pending.lifecycle.state.value,
            order_response=order,
            position=position,
        )
        return False

    def _advance_entry_intent(self, state: OrderState) -> bool:
        try:
            self.entry_intent_store.advance(state)
        except (OSError, ValueError) as error:
            self._pause_engine(
                "entry_intent_transition_failed",
                target_state=state.value,
                error=str(error),
            )
            return False
        return True

    def _clear_entry_intent(self) -> bool:
        try:
            self.entry_intent_store.clear()
        except OSError as error:
            self._pause_engine("entry_intent_clear_failed", error=str(error))
            return False
        return True

    def _settle_entry_order_before_protection(
        self,
        client_order_id: str,
        order: dict,
        position: FuturesPosition,
    ) -> tuple[dict, FuturesPosition] | None:
        """Cancel a resting entry and re-read both exchange facts before protection."""
        status = str(order.get("status") or "").upper()
        if status in {"NEW", "PARTIALLY_FILLED"}:
            try:
                self.client.cancel_order(
                    symbol=self.config.symbol,
                    orig_client_order_id=client_order_id,
                )
            except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as error:
                self._record_journal(
                    "entry_remainder_cancel_uncertain",
                    client_order_id=client_order_id,
                    error=str(error),
                )
            queried_order, query_error = self._query_order_for_reconcile(client_order_id)
            if queried_order is None:
                self._pause_engine(
                    "entry_order_query_after_cancel_failed",
                    client_order_id=client_order_id,
                    error=query_error,
                )
                return None
            order = queried_order
            try:
                position = self._get_position()
            except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as error:
                self._pause_engine(
                    "entry_position_query_after_cancel_failed",
                    client_order_id=client_order_id,
                    error=str(error),
                )
                return None
            status = str(order.get("status") or "").upper()
        if status not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
            self._pause_engine(
                "entry_order_not_terminal",
                client_order_id=client_order_id,
                order_response=order,
            )
            return None
        return order, position

    def _recover_pending_entry_intent(self) -> bool:
        try:
            pending = self.entry_intent_store.load()
        except (OSError, ValueError) as error:
            self._pause_engine("entry_intent_read_failed", error=str(error))
            return False
        if pending is None:
            return True
        if pending.symbol != self.config.symbol:
            self._pause_engine(
                "entry_intent_symbol_mismatch",
                pending_symbol=pending.symbol,
                configured_symbol=self.config.symbol,
            )
            return False

        try:
            position = self._get_position()
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as error:
            self._pause_engine("entry_intent_position_query_failed", error=str(error))
            return False

        state = pending.lifecycle.state
        if state is OrderState.PERSISTED and not position.is_open:
            return self._clear_entry_intent()
        if state is OrderState.PROTECTED and not position.is_open:
            return self._clear_entry_intent()

        if state in {
            OrderState.SUBMITTING,
            OrderState.UNKNOWN,
            OrderState.RECONCILING,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
        }:
            order, query_error = self._query_order_for_reconcile(pending.lifecycle.client_order_id)
            if order is None:
                self._pause_engine(
                    "entry_intent_order_query_failed",
                    client_order_id=pending.lifecycle.client_order_id,
                    error=query_error,
                )
                return False
            settled = self._settle_entry_order_before_protection(
                pending.lifecycle.client_order_id, order, position
            )
            if settled is None:
                return False
            order, position = settled
            if not position.is_open and _order_is_terminal_without_fill(order):
                return self._clear_entry_intent()
            if position.is_open:
                try:
                    executed_quantity = Decimal(str(order.get("executedQty") or "0"))
                except (ValueError, ArithmeticError):
                    executed_quantity = Decimal("0")
                order_status = str(order.get("status") or "").upper()
                expected_direction = (
                    PositionDirection.LONG if pending.side == "BUY" else PositionDirection.SHORT
                )
                net_quantity = executed_quantity
                if getattr(self.client, "spot_mode", False):
                    net_quantity = self.client.spot_net_buy_quantity(
                        self.config.symbol, str(order.get("orderId") or ""), executed_quantity,
                    )
                if (
                    not executed_quantity.is_finite()
                    or executed_quantity <= 0
                    or executed_quantity > pending.quantity
                    or position.quantity != net_quantity
                    or position.direction is not expected_direction
                    or (order_status == "FILLED" and executed_quantity != pending.quantity)
                ):
                    self._pause_engine(
                        "entry_intent_exchange_divergence",
                        client_order_id=pending.lifecycle.client_order_id,
                        order_response=order,
                        position=position,
                    )
                    return False
                if state is OrderState.SUBMITTING and not self._advance_entry_intent(
                    OrderState.ACKNOWLEDGED
                ):
                    return False
                elif state is OrderState.UNKNOWN and not self._advance_entry_intent(
                    OrderState.RECONCILING
                ):
                    return False
                fill_state = (
                    OrderState.FILLED if order_status == "FILLED" else OrderState.PARTIALLY_FILLED
                )
                if state is not fill_state and not self._advance_entry_intent(fill_state):
                    return False
                state = fill_state

        expected_direction = (
            PositionDirection.LONG if pending.side == "BUY" else PositionDirection.SHORT
        )
        if (
            position.is_open
            and position.direction == expected_direction
            and state
            in {
                OrderState.FILLED,
                OrderState.PARTIALLY_FILLED,
                OrderState.PROTECTION_FAILED,
                OrderState.PROTECTED,
            }
        ):
            self._record_journal(
                "entry_intent_recovered",
                trade_id=pending.lifecycle.intent_id,
                client_order_id=pending.lifecycle.client_order_id,
                lifecycle_state=state.value,
            )
            return True

        self._pause_engine(
            "entry_intent_exchange_divergence",
            client_order_id=pending.lifecycle.client_order_id,
            lifecycle_state=state.value,
            position=position,
        )
        return False

    @staticmethod
    def _close_order_fully_filled(order_response: dict | None, quantity: Decimal) -> bool:
        if not isinstance(order_response, dict):
            return False
        if str(order_response.get("status") or "").upper() != "FILLED":
            return False
        try:
            executed_quantity = Decimal(str(order_response["executedQty"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return False
        return executed_quantity.is_finite() and executed_quantity == quantity

    def _submit_order(self, purpose: str, **params) -> dict:
        params.setdefault("newClientOrderId", self._new_client_order_id("ORDER"))
        client_order_id = str(params["newClientOrderId"])

        event_type = {
            "entry": "entry_order_submit_attempt",
            "close": "close_order_submit_attempt",
        }.get(purpose, "order_submit_attempt")

        self._record_journal(
            event_type,
            client_order_id=client_order_id,
            symbol=params.get("symbol"),
            side=params.get("side"),
            order_type=params.get("type"),
            params=params,
        )
        observe_event(
            "order_submit_started",
            component=__name__,
            category="execution",
            channel=EventChannel.AUDIT,
            symbol=params.get("symbol"),
            order_client_id=client_order_id,
            purpose=purpose,
            side=params.get("side"),
            order_type=params.get("type"),
        )

        if not self.config.mutations_enabled:
            self.logger.info("DRY_RUN: order blocked, not sent: %s", params)
            response = {
                "dry_run": True,
                "clientOrderId": client_order_id,
                "params": params,
            }
            observe_event(
                "order_submit_succeeded",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_order_id,
                purpose=purpose,
                dry_run=True,
                response=response,
            )
            return response

        try:
            self.logger.info("Sending LIVE order to configured environment: %s", params)
            response = self.client.new_order(**params)
            observe_event(
                "order_submit_succeeded",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_order_id,
                purpose=purpose,
                dry_run=False,
                response=response,
            )
            return response
        except BinanceFuturesAPIError as error:
            observe_error(
                "api_error",
                error=error,
                component=__name__,
                category="exchange",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_order_id,
                purpose=purpose,
                code=getattr(error, "code", None),
                status_code=getattr(error, "status_code", None),
            )
            observe_error(
                "order_submit_failed",
                error=error,
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_order_id,
                purpose=purpose,
            )
            if is_unknown_execution_status(error):
                raise UnknownOrderStatus(
                    client_order_id=client_order_id,
                    purpose=purpose,
                    original_error=error,
                ) from error
            raise
        except BinanceFuturesNetworkError as error:
            observe_error(
                "order_submit_failed",
                error=error,
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_order_id,
                purpose=purpose,
            )
            raise UnknownOrderStatus(
                client_order_id=client_order_id,
                purpose=purpose,
                original_error=error,
            ) from error

    def _submit_algo_order(self, purpose: str, **params) -> dict:
        params.setdefault("clientAlgoId", self._new_client_order_id("ALGO"))
        client_algo_id = str(params["clientAlgoId"])

        event_type = {
            "stop": "stop_algo_submit_attempt",
            "take": "take_algo_submit_attempt",
        }.get(purpose, "algo_order_submit_attempt")

        self._record_journal(
            event_type,
            client_algo_id=client_algo_id,
            symbol=params.get("symbol"),
            side=params.get("side"),
            order_type=params.get("type"),
            params=params,
        )
        observe_event(
            "order_submit_started",
            component=__name__,
            category="execution",
            channel=EventChannel.AUDIT,
            symbol=params.get("symbol"),
            order_client_id=client_algo_id,
            purpose=purpose,
            side=params.get("side"),
            order_type=params.get("type"),
            algo_order=True,
        )

        if not self.config.mutations_enabled:
            self.logger.info("DRY_RUN: algo order blocked, not sent: %s", params)
            response = {
                "dry_run": True,
                "clientAlgoId": client_algo_id,
                "params": params,
            }
            observe_event(
                "order_submit_succeeded",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_algo_id,
                purpose=purpose,
                dry_run=True,
                algo_order=True,
                response=response,
            )
            return response

        try:
            self.logger.info("Sending LIVE algo order to configured environment: %s", params)
            response = self.client.new_algo_order(**params)
            observe_event(
                "order_submit_succeeded",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_algo_id,
                purpose=purpose,
                dry_run=False,
                algo_order=True,
                response=response,
            )
            return response
        except BinanceFuturesAPIError as error:
            observe_error(
                "api_error",
                error=error,
                component=__name__,
                category="exchange",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_algo_id,
                purpose=purpose,
                code=getattr(error, "code", None),
                status_code=getattr(error, "status_code", None),
            )
            observe_error(
                "order_submit_failed",
                error=error,
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_algo_id,
                purpose=purpose,
                algo_order=True,
            )
            if is_unknown_execution_status(error):
                return self._reconcile_algo_order_unknown(client_algo_id, purpose, error)
            raise
        except BinanceFuturesNetworkError as error:
            observe_error(
                "order_submit_failed",
                error=error,
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=params.get("symbol"),
                order_client_id=client_algo_id,
                purpose=purpose,
                algo_order=True,
            )
            return self._reconcile_algo_order_unknown(client_algo_id, purpose, error)

    def _reconcile_entry_order_unknown(
        self,
        *,
        error: UnknownOrderStatus,
        action: TradeAction,
        trade_id: str,
    ) -> tuple[FuturesPosition, dict] | None:
        order_response, order_error = self._query_order_for_reconcile(error.client_order_id)
        position_error = None

        try:
            position = self._get_position()
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as exc:
            position = self._empty_position()
            position_error = str(exc)

        expected_direction = (
            PositionDirection.LONG if action == TradeAction.OPEN_LONG else PositionDirection.SHORT
        )

        status = str((order_response or {}).get("status") or "").upper()
        executed_quantity = Decimal(str((order_response or {}).get("executedQty") or "0"))
        if (
            order_response is not None
            and status in {"FILLED", "PARTIALLY_FILLED", "NEW", "CANCELED", "EXPIRED"}
            and executed_quantity > 0
            and position.is_open
            and position.direction == expected_direction
        ):
            self._record_journal(
                "entry_order_reconciled",
                trade_id=trade_id,
                client_order_id=error.client_order_id,
                order_response=order_response,
                original_error=str(error.original_error),
            )
            return position, order_response

        if order_response is not None and _order_is_terminal_without_fill(order_response):
            self._record_journal(
                "entry_order_reconcile_no_position",
                trade_id=trade_id,
                client_order_id=error.client_order_id,
                order_response=order_response,
            )
            self.active_trade_id = None
            return None

        self._pause_engine(
            "entry_order_status_unknown",
            trade_id=trade_id,
            client_order_id=error.client_order_id,
            order_error=order_error,
            position_error=position_error,
            original_error=str(error.original_error),
        )
        return None

    def _reconcile_close_order_unknown(
        self,
        *,
        error: UnknownOrderStatus,
        trade_id: str,
        close_client_order_id: str,
        quantity: Decimal,
    ) -> dict | None:
        order_response, order_error = self._query_order_for_reconcile(error.client_order_id)
        position_error = None

        try:
            position = self._get_position()
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as exc:
            position = None
            position_error = str(exc)

        if (
            position is not None
            and not position.is_open
            and self._close_order_fully_filled(order_response, quantity)
        ):
            self._record_journal(
                "close_order_reconciled_flat",
                trade_id=trade_id,
                close_client_order_id=close_client_order_id,
                order_response=order_response,
                original_error=str(error.original_error),
            )
            return order_response

        self._record_journal(
            "position_close_not_confirmed",
            trade_id=trade_id,
            close_client_order_id=close_client_order_id,
            order_response=order_response,
            current_position=position,
            order_error=order_error,
        )
        self._pause_engine(
            "close_order_status_unknown",
            trade_id=trade_id,
            close_client_order_id=close_client_order_id,
            order_error=order_error,
            position_error=position_error,
            original_error=str(error.original_error),
        )
        return None

    def _query_order_for_reconcile(self, client_order_id: str) -> tuple[dict | None, str | None]:
        try:
            response = self.client.query_order(
                symbol=self.config.symbol,
                orig_client_order_id=client_order_id,
            )
            return response, None
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as error:
            self._record_journal(
                "query_order_failed",
                client_order_id=client_order_id,
                error=str(error),
            )
            return None, str(error)

    def _reconcile_algo_order_unknown(
        self,
        client_algo_id: str,
        purpose: str,
        error: Exception,
    ) -> dict:
        try:
            if getattr(self.client, "spot_mode", False):
                response = self.client.query_algo_order(
                    client_algo_id=client_algo_id, symbol=self.config.symbol,
                )
            else:
                response = self.client.query_algo_order(client_algo_id=client_algo_id)
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError) as query_error:
            self._pause_engine(
                "algo_order_status_unknown",
                purpose=purpose,
                client_algo_id=client_algo_id,
                original_error=str(error),
                query_error=str(query_error),
            )
            raise EnginePaused("algo order status unknown") from query_error

        self._record_journal(
            "algo_order_reconciled",
            purpose=purpose,
            client_algo_id=client_algo_id,
            order_response=response,
            original_error=str(error),
        )
        return response

    def _new_trade_id(self, purpose: str) -> str:
        prefix = self._normalized_order_prefix()
        purpose = self._normalize_id_part(purpose)
        now_ms = int(time.time() * 1000)
        suffix = uuid.uuid4().hex[:4].upper()

        return f"{prefix}_T_{purpose}_{now_ms}_{suffix}"[:36]

    def _new_client_order_id(self, purpose: str) -> str:
        prefix = self._normalized_order_prefix()
        purpose = self._normalize_id_part(purpose)
        now_ms = int(time.time() * 1000)
        suffix = uuid.uuid4().hex[:4].upper()

        return f"{prefix}_{purpose}_{now_ms}_{suffix}"[:36]

    def _normalized_order_prefix(self) -> str:
        return self._normalize_id_part(self.config.bot_order_prefix)[:12]

    @staticmethod
    def _normalize_id_part(value: str) -> str:
        value = value.upper().strip()
        value = re.sub(r"[^A-Z0-9_]", "_", value)
        value = re.sub(r"_+", "_", value)
        return value.strip("_") or "ALPHORA"

    @staticmethod
    def _extract_normal_client_order_id(order: dict) -> str:
        return str(order.get("clientOrderId") or order.get("origClientOrderId") or "")

    @staticmethod
    def _extract_algo_client_id(order: dict) -> str:
        return str(
            order.get("clientAlgoId")
            or order.get("clientAlgoID")
            or order.get("clientOrderId")
            or ""
        )

    def _is_robot_client_id(self, client_id: str) -> bool:
        if not client_id:
            return False

        return client_id.startswith(f"{self._normalized_order_prefix()}_")

    def _filter_robot_normal_orders(self, orders: list[dict]) -> list[dict]:
        return [
            order
            for order in orders
            if self._is_robot_client_id(self._extract_normal_client_order_id(order))
        ]

    def _filter_robot_algo_orders(self, orders: list[dict]) -> list[dict]:
        return [
            order
            for order in orders
            if self._is_robot_client_id(self._extract_algo_client_id(order))
        ]

    def _safe_get_open_orders(self) -> OpenOrdersResult:
        try:
            orders = self.client.open_orders(self.config.symbol)
            if not isinstance(orders, list):
                raise TypeError("open_orders response is not a list")
            return OpenOrdersResult.success(orders)

        except (BinanceFuturesAPIError, BinanceFuturesNetworkError, TypeError) as error:
            self.logger.warning("Could not query open normal orders: %s", error)
            return OpenOrdersResult.failure(error)

    def _safe_get_open_algo_orders(self) -> OpenOrdersResult:
        try:
            orders = self.client.open_algo_orders(self.config.symbol)
            if not isinstance(orders, list):
                raise TypeError("open_algo_orders response is not a list")
            return OpenOrdersResult.success(orders)

        except (BinanceFuturesAPIError, BinanceFuturesNetworkError, TypeError) as error:
            self.logger.warning("Could not query open algo orders: %s", error)
            return OpenOrdersResult.failure(error)

    def _safe_cancel_all_open_orders(self, reason: str) -> bool:
        normal_result = self._safe_get_open_orders()
        algo_result = self._safe_get_open_algo_orders()

        if not normal_result.ok or not algo_result.ok:
            self._record_journal(
                "cancel_skipped_order_query_failed",
                reason=reason,
                normal_error=normal_result.error,
                algo_error=algo_result.error,
            )
            return False

        normal_orders = self._filter_robot_normal_orders(normal_result.orders)
        algo_orders = self._filter_robot_algo_orders(algo_result.orders)

        if not normal_orders and not algo_orders:
            self.logger.info("No open robot orders to cancel: %s", reason)
            return True

        self._cancel_robot_open_orders(
            normal_orders=normal_orders,
            algo_orders=algo_orders,
            reason=reason,
        )
        return True

    def _cancel_robot_open_orders(
        self,
        normal_orders: list[dict],
        algo_orders: list[dict],
        reason: str,
    ) -> None:
        cancelled_normal = []
        cancelled_algo = []

        if not self.config.mutations_enabled:
            self._record_journal(
                "cancel_order_skipped_dry_run",
                reason=reason,
                normal_orders=normal_orders,
                algo_orders=algo_orders,
            )
            return

        for order in normal_orders:
            client_order_id = self._extract_normal_client_order_id(order)
            order_id = order.get("orderId")

            self._record_journal(
                "cancel_order_attempt",
                order_kind="normal",
                reason=reason,
                client_order_id=client_order_id,
                order_id=order_id,
            )
            observe_event(
                "order_cancel_started",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=self.config.symbol,
                order_client_id=client_order_id,
                order_id=order_id,
                order_kind="normal",
                reason=reason,
            )

            try:
                response = self.client.cancel_order(
                    symbol=self.config.symbol,
                    order_id=order_id,
                    orig_client_order_id=client_order_id or None,
                )
                cancelled_normal.append(response)
                observe_event(
                    "order_cancel_succeeded",
                    component=__name__,
                    category="execution",
                    channel=EventChannel.AUDIT,
                    symbol=self.config.symbol,
                    order_client_id=client_order_id,
                    order_id=order_id,
                    order_kind="normal",
                    reason=reason,
                    response=response,
                )
                self.logger.info(
                    "Robot normal order cancelled | reason=%s | clientOrderId=%s | orderId=%s",
                    reason,
                    client_order_id,
                    order_id,
                )

            except BinanceFuturesAPIError as error:
                if error.code in ORDER_NOT_FOUND_CODES:
                    observe_event(
                        "order_cancel_succeeded",
                        component=__name__,
                        category="execution",
                        channel=EventChannel.AUDIT,
                        symbol=self.config.symbol,
                        order_client_id=client_order_id,
                        order_id=order_id,
                        order_kind="normal",
                        reason="ORDER_ALREADY_GONE",
                    )
                    self.logger.info(
                        "Normal order no longer exists | clientOrderId=%s | orderId=%s",
                        client_order_id,
                        order_id,
                    )
                else:
                    observe_error(
                        "order_cancel_failed",
                        error=error,
                        component=__name__,
                        category="execution",
                        channel=EventChannel.AUDIT,
                        symbol=self.config.symbol,
                        order_client_id=client_order_id,
                        order_id=order_id,
                        order_kind="normal",
                        reason=reason,
                    )
                    raise

        for order in algo_orders:
            client_algo_id = self._extract_algo_client_id(order)
            algo_id = order.get("algoId")

            self._record_journal(
                "cancel_order_attempt",
                order_kind="algo",
                reason=reason,
                client_algo_id=client_algo_id,
                algo_id=algo_id,
            )
            observe_event(
                "order_cancel_started",
                component=__name__,
                category="execution",
                channel=EventChannel.AUDIT,
                symbol=self.config.symbol,
                order_client_id=client_algo_id,
                algo_id=algo_id,
                order_kind="algo",
                reason=reason,
            )

            try:
                cancel_params = {"algo_id": algo_id, "client_algo_id": client_algo_id or None}
                if getattr(self.client, "spot_mode", False):
                    cancel_params["symbol"] = self.config.symbol
                response = self.client.cancel_algo_order(**cancel_params)
                cancelled_algo.append(response)
                observe_event(
                    "order_cancel_succeeded",
                    component=__name__,
                    category="execution",
                    channel=EventChannel.AUDIT,
                    symbol=self.config.symbol,
                    order_client_id=client_algo_id,
                    algo_id=algo_id,
                    order_kind="algo",
                    reason=reason,
                    response=response,
                )
                self.logger.info(
                    "Algo order do robo cancelada | reason=%s | clientAlgoId=%s | algoId=%s",
                    reason,
                    client_algo_id,
                    algo_id,
                )

            except BinanceFuturesAPIError as error:
                if error.code in ORDER_NOT_FOUND_CODES:
                    observe_event(
                        "order_cancel_succeeded",
                        component=__name__,
                        category="execution",
                        channel=EventChannel.AUDIT,
                        symbol=self.config.symbol,
                        order_client_id=client_algo_id,
                        algo_id=algo_id,
                        order_kind="algo",
                        reason="ORDER_ALREADY_GONE",
                    )
                    self.logger.info(
                        "Algo order no longer exists | clientAlgoId=%s | algoId=%s",
                        client_algo_id,
                        algo_id,
                    )
                else:
                    observe_error(
                        "order_cancel_failed",
                        error=error,
                        component=__name__,
                        category="execution",
                        channel=EventChannel.AUDIT,
                        symbol=self.config.symbol,
                        order_client_id=client_algo_id,
                        algo_id=algo_id,
                        order_kind="algo",
                        reason=reason,
                    )
                    raise

        self._record_journal(
            "robot_orders_cancelled",
            reason=reason,
            cancelled_normal=cancelled_normal,
            cancelled_algo=cancelled_algo,
        )
