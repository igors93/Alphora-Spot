import json
import time
from decimal import Decimal
from pathlib import Path

from config import RecoveryAction
from domain.models import FuturesPosition
from domain.symbol_rules import SymbolRules
from execution.engine_mixin_host import EngineMixinHost
from execution.exchange_config import configure_margin_leverage_safely
from execution.simulated_broker import SimulatedBroker
from execution.state_reconciliation import build_exchange_state_snapshot
from futures.exchange_rules import (
    extract_symbol_rules,
)
from futures.operational import (
    OperationalState,
)
from futures.performance.cost_model import CostModel
from operational_module_modes import ReconciliationMode
from utils.audit_logger import get_run_dir
from utils.events import EventChannel, observe_event


class EngineExchangeMixin(EngineMixinHost):
    paper_broker: SimulatedBroker | None
    rules: SymbolRules | None

    def _validate_position_mode(self) -> None:
        data = self.client.get_position_mode()
        dual_side_position = data.get("dualSidePosition")

        if dual_side_position in {True, "true", "TRUE", "True"}:
            raise ValueError(
                "Futures account is in Hedge Mode. Alphora requires One-way Mode/BOTH."
            )

        self.logger.info("Position mode validated: One-way Mode/BOTH")

    def _configure_exchange(self) -> None:
        self.logger.info("Configuring margin/leverage after recovery")
        configure_margin_leverage_safely(
            client=self.client,
            symbol=self.config.symbol,
            margin_type=self.config.margin_type.value,
            leverage=self.config.leverage,
            logger=self.logger,
            record_event=self._record_journal,
        )

    def _recover_state_on_boot(self) -> None:
        self.logger.info("Starting state recovery on boot")

        if self.config.mutations_enabled:
            if not self._recover_pending_close_intent():
                return
            if not self._recover_pending_entry_intent():
                return

        position = self._get_position()
        normal_result = self._safe_get_open_orders()
        algo_result = self._safe_get_open_algo_orders()

        if not normal_result.ok or not algo_result.ok:
            self._pause_engine(
                "recovery_order_query_failed",
                normal_error=normal_result.error,
                algo_error=algo_result.error,
            )
            return

        normal_orders = normal_result.orders
        algo_orders = algo_result.orders
        robot_normal_orders = self._filter_robot_normal_orders(normal_orders)
        robot_algo_orders = self._filter_robot_algo_orders(algo_orders)

        self.logger.info(
            "Recovery snapshot | position=%s | qty=%s | normal_orders=%s | algo_orders=%s | "
            "robot_normal_orders=%s | robot_algo_orders=%s",
            position.direction.value,
            position.quantity,
            len(normal_orders),
            len(algo_orders),
            len(robot_normal_orders),
            len(robot_algo_orders),
        )

        self._record_journal(
            "recovery_snapshot",
            position=position,
            normal_orders=normal_orders,
            algo_orders=algo_orders,
            robot_normal_orders=robot_normal_orders,
            robot_algo_orders=robot_algo_orders,
            runtime_state=self._runtime_state_data(),
        )
        reconciliation_decision = self._record_reconciliation_snapshot(
            position=position,
            normal_orders=normal_orders,
            algo_orders=algo_orders,
            robot_normal_orders=robot_normal_orders,
            robot_algo_orders=robot_algo_orders,
            stage="RECOVERY_BOOT",
        )

        if position.is_open:
            recovered_position = self._attach_or_create_state_for_open_position(position)

            self.logger.warning(
                "Recovery found open position | direction=%s | qty=%s | entry=%s",
                recovered_position.direction.value,
                recovered_position.quantity,
                recovered_position.entry_price,
            )

            if self._recovery_requires_protection_priority():
                self._record_operational_state(
                    OperationalState.PROTECTIONS_PENDING,
                    reason="recovery_protections_pending",
                    position=recovered_position,
                )
                audit_result = self._audit_protection_orders(
                    position=recovered_position,
                    robot_algo_orders=robot_algo_orders,
                    reason="recovery_protections_pending",
                )
                if audit_result.ok:
                    self._mark_position_protected(
                        recovered_position,
                        audit_result=audit_result,
                        reason="recovery_protections_pending",
                    )
                else:
                    self._handle_unprotected_position(
                        position=recovered_position,
                        audit_result=audit_result,
                        reason="recovery_protections_pending",
                        robot_algo_orders=robot_algo_orders,
                    )
                return

            if self.config.recovery_action == RecoveryAction.CLOSE:
                self.logger.warning("Recovery action CLOSE activated. Closing position.")
                self._close_position(recovered_position, "recovery_close_on_boot")
                return

            if self.config.recovery_action == RecoveryAction.HOLD:
                self.logger.warning(
                    "Recovery action HOLD activated. Position will be held without automatic repair."
                )
                return

            if self.config.recovery_action != RecoveryAction.PROTECT:
                raise ValueError(f"Invalid recovery action: {self.config.recovery_action}")

            audit_result = self._audit_protection_orders(
                position=recovered_position,
                robot_algo_orders=robot_algo_orders,
                reason="recovery_on_boot",
            )

            if not audit_result.ok:
                self._handle_unprotected_position(
                    position=recovered_position,
                    audit_result=audit_result,
                    reason="recovery_on_boot",
                    robot_algo_orders=robot_algo_orders,
                )
            else:
                self._mark_position_protected(
                    recovered_position,
                    audit_result=audit_result,
                    reason="recovery_on_boot",
                )

            return

        if robot_normal_orders or robot_algo_orders:
            if self.config.cancel_orphan_orders_on_boot:
                self.logger.warning(
                    "Recovery found robot orders without an open position. Cancelling orphans."
                )
                if (
                    reconciliation_decision is not None
                    and reconciliation_decision.mode is ReconciliationMode.REPAIR_SAFE
                ):
                    self._record_journal(
                        "reconciliation_repair_attempted",
                        symbol=self.config.symbol,
                        mode=reconciliation_decision.mode.value,
                        repair="CANCEL_ORPHAN_ROBOT_ORDERS",
                        normal_orders_count=len(robot_normal_orders),
                        algo_orders_count=len(robot_algo_orders),
                    )
                self._cancel_robot_open_orders(
                    normal_orders=robot_normal_orders,
                    algo_orders=robot_algo_orders,
                    reason="recovery_flat_with_orphan_orders",
                )
                if (
                    reconciliation_decision is not None
                    and reconciliation_decision.mode is ReconciliationMode.REPAIR_SAFE
                ):
                    self._record_journal(
                        "reconciliation_repair_succeeded",
                        symbol=self.config.symbol,
                        mode=reconciliation_decision.mode.value,
                        repair="CANCEL_ORPHAN_ROBOT_ORDERS",
                    )
            else:
                self.logger.warning(
                    "Recovery found robot orders without a position, but cancellation is disabled."
                )

        self._record_external_position_close(reason="recovery_boot_exchange_flat")
        self._clear_runtime_state()
        self.logger.info("Recovery completed with no open position")

    def _record_reconciliation_snapshot(
        self,
        *,
        position: FuturesPosition,
        normal_orders: list[dict],
        algo_orders: list[dict],
        robot_normal_orders: list[dict],
        robot_algo_orders: list[dict],
        stage: str,
    ):
        mode = self.config.state_reconciliation_mode or ReconciliationMode.OFF
        if mode is ReconciliationMode.OFF:
            return None

        snapshot = build_exchange_state_snapshot(
            symbol=self.config.symbol,
            local_position=self._position_from_runtime_state(),
            exchange_position=position,
            normal_orders=normal_orders,
            algo_orders=algo_orders,
            robot_normal_orders=robot_normal_orders,
            robot_algo_orders=robot_algo_orders,
            data_source="FUTURES_ENGINE_RECOVERY",
            timestamp=time.time(),
            diagnostics={"runtime_state": self._runtime_state_data()},
        )
        decision = self.state_reconciliation_engine.evaluate(snapshot, mode=mode)
        payload = decision.to_dict()
        report_path = self._write_reconciliation_report(payload)
        self._record_journal(
            "reconciliation_snapshot_created",
            symbol=self.config.symbol,
            stage=stage,
            mode=decision.mode.value,
            status=decision.status,
            issues_count=len(decision.issues),
            should_block_new_entries=decision.should_block_new_entries,
            snapshot=snapshot.to_dict(),
            diagnostics=payload,
            reconciliation_report_path=str(report_path) if report_path else None,
        )
        for issue in decision.issues:
            log = self.logger.error if issue.severity == "CRITICAL" else self.logger.warning
            log(
                "Reconciliation issue | symbol=%s | stage=%s | code=%s | severity=%s | %s | "
                "suggested_action=%s",
                self.config.symbol,
                stage,
                issue.code,
                issue.severity,
                issue.description,
                issue.suggested_action,
            )
            issue_payload = issue.to_dict()
            issue_payload.pop("symbol", None)
            self._record_journal(
                "reconciliation_issue_detected",
                symbol=self.config.symbol,
                stage=stage,
                mode=decision.mode.value,
                **issue_payload,
            )
        if decision.should_block_new_entries:
            path = self._write_manual_reconcile_required(
                "state_reconciliation_block",
                payload,
            )
            self._record_operational_state(
                OperationalState.MANUAL_RECONCILE_REQUIRED,
                reason="state_reconciliation_block",
                path=str(path),
                **payload,
            )
            self._record_journal(
                "reconciliation_block_applied",
                symbol=self.config.symbol,
                mode=decision.mode.value,
                status=decision.status,
                path=str(path),
                reconciliation_report_path=str(report_path) if report_path else None,
                issues=[issue.to_dict() for issue in decision.issues],
            )
        if report_path is not None:
            self._record_journal(
                "reconciliation_report_written",
                symbol=self.config.symbol,
                stage=stage,
                path=str(report_path),
                mode=decision.mode.value,
                status=decision.status,
            )
        observe_event(
            "state_reconciliation_evaluated",
            component=__name__,
            category="execution",
            channel=EventChannel.AUDIT,
            symbol=self.config.symbol,
            stage=stage,
            mode=decision.mode.value,
            status=decision.status,
            issues_count=len(decision.issues),
            should_block_new_entries=decision.should_block_new_entries,
        )
        return decision

    def _position_from_runtime_state(self) -> FuturesPosition:
        state = self.runtime_state
        if state is None:
            return self._empty_position()
        return FuturesPosition(
            symbol=self.config.symbol,
            direction=state.direction,
            quantity=state.quantity,
            entry_price=state.entry_price,
            unrealized_pnl=Decimal("0"),
            opened_at=state.opened_at,
        )

    def _write_reconciliation_report(self, payload: dict):
        run_dir = get_run_dir()
        if run_dir is None:
            return None
        path = run_dir / "reports" / "reconciliation_report.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload["last_report_path"] = str(path)
            diagnostics = payload.get("diagnostics")
            if isinstance(diagnostics, dict):
                diagnostics["reconciliation_report_path"] = str(path)
            existing = []
            if path.exists():
                existing_payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing_payload, list):
                    existing = existing_payload
            existing.append(payload)
            path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            return path
        except (OSError, ValueError, TypeError) as error:
            self.logger.warning("Failed to write reconciliation_report.json: %s", error)
            return None

    def _bootstrap(self) -> None:
        self.logger.info("Synchronizing time with Binance")
        self.client.sync_time()

        self.logger.info("Testing public connection")
        self.client.ping()

        self.logger.info("Validating Futures account authentication")
        account_info = self.client.account_info()
        self._log_account_balance(account_info)

        self._validate_position_mode()

        exchange_info = self.client.exchange_info()
        self.rules = extract_symbol_rules(exchange_info, self.config.symbol)

        self.logger.info(
            "Symbol rules loaded | step=%s | min_qty=%s | min_notional=%s | tick=%s",
            self.rules.quantity_step,
            self.rules.min_quantity,
            self.rules.min_notional,
            self.rules.price_tick,
        )

        if self.config.recover_on_boot:
            self._recover_state_on_boot()

        if self.config.execution_mode.is_paper:
            self.paper_broker = SimulatedBroker(
                symbol=self.config.symbol,
                risk_manager=self.risk_manager,
                symbol_rules=self.rules,
                cost_model=CostModel(),
                exit_model=self.exit_model,
            )

        if self.config.mutations_enabled and not self.paused:
            self._configure_exchange()

    def _consume_stop_request(self) -> bool:
        stop_path = self._stop_request_path()
        if not stop_path.exists():
            return False

        self._record_journal("engine_stop_requested", path=str(stop_path))
        try:
            stop_path.unlink()
        except OSError as error:
            self.logger.warning("Could not delete stop.request file: %s", error)

        self._record_journal("engine_stopped_by_user")
        self.logger.info("Stop request received. Shutting down engine safely.")
        return True

    def _stop_request_path(self):

        return Path(self.config.runtime_dir) / "stop.request"
