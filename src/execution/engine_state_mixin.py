import time
from decimal import Decimal
from pathlib import Path

from domain.models import FuturesPosition
from domain.orders import OrderState
from execution.engine_mixin_host import EngineMixinHost
from futures.operational import (
    OperationalState,
    write_manual_reconcile_required,
)
from futures.protection import (
    PROTECTION_SOURCE_STATIC,
    PROTECTION_SOURCE_TRADE_PLAN,
    ExpectedProtection,
    ProtectionAuditResult,
)
from futures.state import state_from_position


class EngineStateMixin(EngineMixinHost):
    active_trade_id: str | None
    position_opened_at: float | None

    def _save_state_for_position(
        self,
        *,
        position: FuturesPosition,
        trade_id: str,
        entry_client_order_id: str | None,
        stop_client_algo_id: str | None = None,
        take_client_algo_id: str | None = None,
        operational_state: OperationalState = OperationalState.POSITION_OPEN_PROTECTED,
        protection_warning: str = "",
        protection_source: str = PROTECTION_SOURCE_STATIC,
        trade_plan_id: str | None = None,
        planned_stop_price: Decimal | None = None,
        planned_take_profit_price: Decimal | None = None,
    ) -> None:
        opened_at = position.opened_at or self.position_opened_at or time.time()
        state = state_from_position(
            active_trade_id=trade_id,
            symbol=position.symbol,
            direction=position.direction,
            opened_at=opened_at,
            entry_price=position.entry_price,
            quantity=position.quantity,
            entry_client_order_id=entry_client_order_id,
            stop_client_algo_id=stop_client_algo_id,
            take_client_algo_id=take_client_algo_id,
            operational_state=operational_state,
            protection_warning=protection_warning,
            protection_source=protection_source,
            trade_plan_id=trade_plan_id,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
        )
        self.state_store.save(state)
        self.active_trade_id = state.active_trade_id
        self.position_opened_at = state.opened_at
        self._record_operational_state(
            operational_state,
            reason="runtime_state_saved",
            trade_id=trade_id,
            protection_warning=protection_warning,
            protection_source=protection_source,
            trade_plan_id=trade_plan_id,
            planned_stop_price=planned_stop_price,
            planned_take_profit_price=planned_take_profit_price,
        )
        if (
            operational_state is OperationalState.POSITION_OPEN_PROTECTED
            and self.config.mutations_enabled
        ):
            try:
                pending = self.entry_intent_store.load()
            except (OSError, ValueError) as error:
                self._pause_engine("entry_intent_read_failed", error=str(error))
                return
            if pending is not None:
                if pending.lifecycle.state is not OrderState.PROTECTED:
                    if not self._advance_entry_intent(OrderState.PROTECTED):
                        return
                self._clear_entry_intent()

    def _clear_runtime_state(self) -> None:
        self.position_opened_at = None
        self.was_position_open = False
        self.active_trade_id = None
        self.state_store.clear()

    def _record_external_position_close(self, *, reason: str) -> None:
        """Journal a position that vanished from the exchange outside the robot's request path.

        Covers manual closes on the exchange UI and stop/take fills the robot did not
        observe (e.g. while offline). Without this record the trade journal keeps a
        dangling open trade. Best-effort: queries the recorded protection orders to
        distinguish a late-confirmed stop/take fill from a truly external close.
        Must be called BEFORE _clear_runtime_state().
        """
        state = self.runtime_state
        if state is None:
            return

        closed_at = time.time()
        exit_source, close_price, close_diagnostics = self._classify_untracked_close(state)
        self.logger.warning(
            "Position closed outside the robot | trade_id=%s | direction=%s | qty=%s | "
            "entry=%s | exit_source=%s | close_price=%s | reason=%s",
            state.active_trade_id,
            state.direction.value,
            state.quantity,
            state.entry_price,
            exit_source,
            close_price if close_price is not None else "unknown",
            reason,
        )
        self._record_journal(
            "position_closed_externally",
            trade_id=state.active_trade_id,
            symbol=state.symbol,
            direction=state.direction.value,
            quantity=str(state.quantity),
            entry_price=str(state.entry_price),
            opened_at=state.opened_at,
            closed_at=closed_at,
            exit_reason=reason,
            exit_source=exit_source,
            close_price=close_price,
            planned_stop_price=state.planned_stop_price,
            planned_take_profit_price=state.planned_take_profit_price,
            pnl_status="UNKNOWN",
            pnl_source="EXTERNAL_CLOSE",
            diagnostics=close_diagnostics,
        )
        recorder = getattr(self, "trade_audit_recorder", None)
        if recorder is not None:
            try:
                recorder.record_completed(
                    symbol=state.symbol,
                    trade_id=state.active_trade_id,
                    trade_plan_id=state.trade_plan_id,
                    entry_time=str(state.opened_at),
                    exit_time=str(closed_at),
                    exit_reason=reason,
                    exit_source=exit_source,
                    entry_price=state.entry_price,
                    close_price=close_price,
                    quantity=state.quantity,
                    direction=state.direction.value,
                    planned_stop_price=state.planned_stop_price,
                    planned_take_profit_price=state.planned_take_profit_price,
                    gross_pnl=None,
                    net_pnl=None,
                    pnl_status="UNKNOWN",
                    pnl_source="EXTERNAL_CLOSE",
                    diagnostics={"source": "EXTERNAL_CLOSE_DETECTION", **close_diagnostics},
                )
            except (OSError, ValueError, TypeError) as error:
                self.logger.warning(
                    "Failed to record external close in trade audit | trade_id=%s | error=%s",
                    state.active_trade_id,
                    error,
                )

    def _classify_untracked_close(self, state) -> tuple[str, str | None, dict]:
        """Best-effort: query recorded protection orders to tell a late stop/take fill
        from a manual/external close. Never raises — falls back to EXTERNAL/unknown."""
        from binance.futures_client import (
            BinanceFuturesAPIError,
            BinanceFuturesNetworkError,
        )

        candidates = (
            ("STOP_LOSS", state.stop_client_algo_id),
            ("TAKE_PROFIT", state.take_client_algo_id),
        )
        diagnostics: dict = {}
        for label, client_algo_id in candidates:
            if not client_algo_id:
                continue
            try:
                if getattr(self.client, "spot_mode", False):
                    response = self.client.query_algo_order(
                        client_algo_id=client_algo_id, symbol=self.config.symbol,
                    )
                else:
                    response = self.client.query_algo_order(client_algo_id=client_algo_id)
            except (BinanceFuturesAPIError, BinanceFuturesNetworkError, AttributeError) as error:
                diagnostics[f"{label.lower()}_query_error"] = str(error)
                continue
            status = str(response.get("algoStatus") or response.get("status") or "").upper()
            diagnostics[f"{label.lower()}_status"] = status
            if status in {"FILLED", "TRIGGERED", "EXECUTED"}:
                close_price = response.get("avgPrice") or response.get("triggerPrice")
                return (
                    label,
                    str(close_price) if close_price not in (None, "", "0") else None,
                    diagnostics,
                )
        return "EXTERNAL", None, diagnostics

    def _runtime_state_data(self) -> dict | None:
        if self.runtime_state is None:
            return None

        return {
            "active_trade_id": self.runtime_state.active_trade_id,
            "symbol": self.runtime_state.symbol,
            "direction": self.runtime_state.direction.value,
            "opened_at": self.runtime_state.opened_at,
            "entry_price": self.runtime_state.entry_price,
            "quantity": self.runtime_state.quantity,
            "entry_client_order_id": self.runtime_state.entry_client_order_id,
            "stop_client_algo_id": self.runtime_state.stop_client_algo_id,
            "take_client_algo_id": self.runtime_state.take_client_algo_id,
            "operational_state": self.runtime_state.operational_state.value,
            "protection_warning": self.runtime_state.protection_warning,
            "protection_source": self.runtime_state.protection_source,
            "trade_plan_id": self.runtime_state.trade_plan_id,
            "planned_stop_price": self.runtime_state.planned_stop_price,
            "planned_take_profit_price": self.runtime_state.planned_take_profit_price,
        }

    def _recovery_requires_protection_priority(self) -> bool:
        return (
            self.runtime_state is not None
            and self.runtime_state.operational_state is OperationalState.PROTECTIONS_PENDING
        )

    def _mark_position_protected(
        self,
        position: FuturesPosition,
        *,
        audit_result: ProtectionAuditResult,
        reason: str,
    ) -> None:
        protection_state = self._protection_state_payload(
            audit_result.expected,
            fallback_trade_plan_id=(
                getattr(self.runtime_state, "trade_plan_id", None)
                if self.runtime_state is not None
                else None
            ),
        )

        self._save_state_for_position(
            position=position,
            trade_id=self.active_trade_id or self._new_trade_id("PROTECTED"),
            entry_client_order_id=(
                self.runtime_state.entry_client_order_id if self.runtime_state is not None else None
            ),
            stop_client_algo_id=(
                self._extract_algo_client_id(audit_result.stop_order)
                if audit_result.stop_order
                else None
            ),
            take_client_algo_id=(
                self._extract_algo_client_id(audit_result.take_order)
                if audit_result.take_order
                else None
            ),
            operational_state=OperationalState.POSITION_OPEN_PROTECTED,
            protection_warning=audit_result.expected.warning,
            **protection_state,
        )
        self._record_operational_state(
            OperationalState.POSITION_OPEN_PROTECTED,
            reason=reason,
            position=position,
            protection_warning=audit_result.expected.warning,
            protection_source=protection_state["protection_source"],
            trade_plan_id=protection_state["trade_plan_id"],
            planned_stop_price=protection_state["planned_stop_price"],
            planned_take_profit_price=protection_state["planned_take_profit_price"],
        )

    @staticmethod
    def _protection_state_payload(
        expected: ExpectedProtection,
        *,
        fallback_trade_plan_id: str | None = None,
    ) -> dict:
        """Preserve the real protection source and actual placed trigger prices in runtime_state.

        Storing the actual placed stop/take prices (not None) for all protection sources prevents
        the audit from re-deriving them from position.entry_price on the next cycle. The fill
        response and the position_risk endpoint can return slightly different entry prices (1-tick
        precision difference), which would cause the audit to flag valid protections as invalid.
        """
        is_trade_plan = expected.source == PROTECTION_SOURCE_TRADE_PLAN
        return {
            "protection_source": expected.source,
            "trade_plan_id": (expected.trade_plan_id if is_trade_plan else fallback_trade_plan_id),
            "planned_stop_price": expected.stop_price,
            "planned_take_profit_price": expected.take_profit_price,
        }

    def _record_operational_state(
        self,
        state: OperationalState,
        *,
        reason: str,
        **data,
    ) -> None:
        self._record_journal(
            "operational_state_changed",
            operational_state=state.value,
            reason=reason,
            symbol=self.config.symbol,
            **data,
        )

    def _write_manual_reconcile_required(self, reason: str, data: dict) -> Path:
        from binance.futures_client import (
            BinanceFuturesAPIError,
            BinanceFuturesNetworkError,
        )

        try:
            position = self._get_position()
            last_known_position = self._position_data(position)
        except (BinanceFuturesAPIError, BinanceFuturesNetworkError, TypeError) as error:
            last_known_position = {"error": str(error)}

        return write_manual_reconcile_required(
            runtime_dir=self.config.runtime_dir,
            symbol=self.config.symbol,
            reason=reason,
            context=data,
            last_known_position=last_known_position,
        )

    def _record_journal(self, event_type: str, **data) -> None:
        try:
            self.journal.append(event_type, **data)
        except (OSError, ValueError, TypeError) as error:
            self.logger.warning(
                "Failed to write trade journal | event=%s | error=%s", event_type, error
            )
