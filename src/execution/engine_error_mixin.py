import time
from decimal import Decimal

from binance.futures_client import (
    BinanceFuturesAPIError,
    BinanceFuturesMutationBlocked,
)
from execution.engine_mixin_host import EngineMixinHost
from execution.errors import (
    ErrorKind,
    classify_error,
)
from futures.operational import (
    MANUAL_RECONCILE_REASONS,
    OperationalState,
)


class EngineErrorMixin(EngineMixinHost):
    paused: bool
    current_backoff_seconds: float

    def _pause_engine(self, reason: str, **data) -> None:
        if not self.paused:
            self.logger.error("Engine paused | reason=%s | data=%s", reason, data)
            self._record_journal("engine_paused", reason=reason, **data)
            self._record_journal("needs_manual_review", reason=reason, **data)
            if reason in MANUAL_RECONCILE_REASONS:
                path = self._write_manual_reconcile_required(reason, data)
                self._record_operational_state(
                    OperationalState.MANUAL_RECONCILE_REQUIRED,
                    reason=reason,
                    path=str(path),
                    **data,
                )

        self.paused = True

    def _handle_loop_error(self, error: Exception) -> None:
        if isinstance(error, BinanceFuturesMutationBlocked):
            self._pause_engine("mutation_blocked", message=str(error))
            return

        kind = classify_error(error)
        self.consecutive_errors += 1

        self._record_journal(
            "engine_error",
            kind=kind.value,
            consecutive_errors=self.consecutive_errors,
            message=str(error),
        )

        if isinstance(error, BinanceFuturesAPIError):
            self._handle_binance_error(error)
        else:
            self.logger.error("Main loop error: %s", error)

        if kind in {ErrorKind.FATAL, ErrorKind.UNKNOWN_ORDER_STATUS}:
            self._pause_engine(
                "fatal_or_unknown_error",
                kind=kind.value,
                message=str(error),
            )
            return

        if self.consecutive_errors >= self.config.max_consecutive_errors:
            self._pause_engine(
                "max_consecutive_errors_exceeded",
                consecutive_errors=self.consecutive_errors,
                message=str(error),
            )
            return

        sleep_seconds = self.current_backoff_seconds
        self.current_backoff_seconds = min(
            self.current_backoff_seconds * 2,
            self.config.max_backoff_seconds,
        )
        time.sleep(sleep_seconds)

    def _reset_error_backoff(self) -> None:
        self.consecutive_errors = 0
        self.current_backoff_seconds = self.config.initial_backoff_seconds

    def _handle_binance_error(self, error: BinanceFuturesAPIError) -> None:
        self.logger.error("Binance error: %s", error)

        if error.code == -2015:
            self.logger.error(
                "Invalid API key, incorrect permission, blocked IP, or wrong environment."
            )

        elif error.code == -1021:
            self.logger.error("Timestamp out of sync. Attempting time synchronization.")
            self.client.sync_time()

        elif error.code == -1121:
            self.logger.error("Invalid symbol. Check ALPHORA_SYMBOL in .env.")

        elif error.code == -2019:
            self.logger.error(
                "Insufficient margin. Increase balance, reduce size, or adjust leverage."
            )

        elif error.code == -4120:
            self.logger.error(
                "Order type not supported on the normal endpoint. Stop/Take uses Algo Order."
            )

        elif error.status_code in {418, 429}:
            self.logger.error("Rate limit or temporary ban. Increase the loop interval.")

        elif error.status_code == 503:
            self.logger.error(
                "Error 503. Execution status may be unknown; reconciliation required."
            )

    def _log_account_balance(self, account_info: dict) -> None:
        usdt = self._extract_usdt_asset(account_info)

        if usdt is None:
            self.logger.warning("USDT balance not found in account_info. API response may differ.")
            return

        wallet_balance = usdt.get("walletBalance", "0")
        available_balance = usdt.get("availableBalance", "0")
        margin_balance = usdt.get("marginBalance", "0")
        unrealized_pnl = usdt.get("unrealizedProfit", "0")

        self.logger.info(
            "USDT balance via API | wallet=%s | available=%s | margin=%s | unrealized_pnl=%s",
            wallet_balance,
            available_balance,
            margin_balance,
            unrealized_pnl,
        )

    def _has_available_margin_for_order(self, quantity: Decimal, price: Decimal) -> bool:
        account_info = self.client.account_info()
        usdt = self._extract_usdt_asset(account_info)

        if usdt is None:
            self.logger.error("Cannot validate margin: USDT balance not found in API response.")
            return False

        available_balance = Decimal(str(usdt.get("availableBalance", "0")))
        notional = quantity * price
        required_margin = notional / Decimal(str(self.config.leverage))

        safety_multiplier = Decimal("1.10")
        required_with_safety = required_margin * safety_multiplier

        self.logger.info(
            "Margin validation | notional=%s | leverage=%sx | estimated_margin=%s | "
            "margin_with_buffer=%s | available_api=%s",
            notional,
            self.config.leverage,
            required_margin,
            required_with_safety,
            available_balance,
        )

        if available_balance < required_with_safety:
            self.logger.error(
                "Insufficient margin according to API. Available=%s USDT | Required=%s USDT.",
                available_balance,
                required_with_safety,
            )
            return False

        return True

    @staticmethod
    def _extract_usdt_asset(account_info: dict) -> dict | None:
        for asset in account_info.get("assets", []):
            if asset.get("asset") == "USDT":
                return asset

        return None
