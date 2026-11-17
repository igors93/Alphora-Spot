from collections.abc import Callable
from decimal import Decimal
from typing import Any

from binance.futures_client import (
    BinanceFuturesAPIError,
    BinanceFuturesClient,
    BinanceFuturesNetworkError,
)


def configure_margin_leverage_safely(
    *,
    client: BinanceFuturesClient,
    symbol: str,
    margin_type: str,
    leverage: int,
    logger,
    record_event: Callable[..., None] | None = None,
) -> bool:
    skip_reason = exchange_config_skip_reason(client=client, symbol=symbol)
    if skip_reason:
        _warn_skip(logger, symbol, skip_reason)
        _record(
            record_event,
            "exchange_config_skipped",
            symbol=symbol,
            reason=skip_reason,
            safety_note=(
                "Margin/leverage config skipped because an open position, "
                "open orders exist, or the symbol state could not be confirmed."
            ),
            safety_action=(
                "Do not change margin/leverage during recovery with open position "
                "or orders. Continue auditing position and protections."
            ),
        )
        return False

    try:
        client.change_margin_type(symbol=symbol, margin_type=margin_type)
        logger.info("Margin configured | symbol=%s | margin_type=%s", symbol, margin_type)
    except BinanceFuturesAPIError as error:
        if error.code == -4046:
            logger.info("Margin type already configured | symbol=%s", symbol)
        elif _is_open_state_exchange_config_error(error):
            _warn_binance_rejected(logger, symbol, "margin", error)
            _record(
                record_event,
                "exchange_config_skipped",
                symbol=symbol,
                reason="binance_rejected_margin_change_open_state",
                error=str(error),
            )
            return False
        else:
            raise

    try:
        client.change_leverage(symbol=symbol, leverage=leverage)
        logger.info("Leverage configured | symbol=%s | leverage=%sx", symbol, leverage)
    except BinanceFuturesAPIError as error:
        if _is_open_state_exchange_config_error(error):
            _warn_binance_rejected(logger, symbol, "leverage", error)
            _record(
                record_event,
                "exchange_config_skipped",
                symbol=symbol,
                reason="binance_rejected_leverage_change_open_state",
                error=str(error),
            )
            return False
        raise

    _record(
        record_event,
        "symbol_exchange_configured",
        symbol=symbol,
        margin_type=margin_type,
        leverage=leverage,
    )
    return True


def exchange_config_skip_reason(*, client: BinanceFuturesClient, symbol: str) -> str:
    if not hasattr(client, "position_risk"):
        return ""

    try:
        positions = client.position_risk(symbol)
    except (BinanceFuturesAPIError, BinanceFuturesNetworkError, TypeError):
        return "position_query_failed"

    for position in positions:
        amount = Decimal(str(position.get("positionAmt", "0")))
        if amount != 0:
            return "position_open"

    if not hasattr(client, "open_orders") or not hasattr(client, "open_algo_orders"):
        return ""

    try:
        normal_orders = client.open_orders(symbol)
        algo_orders = client.open_algo_orders(symbol)
    except (BinanceFuturesAPIError, BinanceFuturesNetworkError, TypeError):
        return "open_orders_query_failed"

    if normal_orders or algo_orders:
        return "orders_open"

    return ""


def _is_open_state_exchange_config_error(error: BinanceFuturesAPIError) -> bool:
    message = str(error.message).lower()
    return error.code in {-4067, -4047} or "open order" in message or "position" in message


def _warn_skip(logger, symbol: str, reason: str) -> None:
    logger.warning(
        "Margin/leverage config skipped for safety | symbol=%s | "
        "reason=%s | explanation=Open position, open orders, or state could not be "
        "confirmed. Alphora will continue with recovery, audit, and protections.",
        symbol,
        reason,
    )


def _warn_binance_rejected(
    logger,
    symbol: str,
    setting_name: str,
    error: BinanceFuturesAPIError,
) -> None:
    logger.warning(
        "Binance rejected %s change in open state | symbol=%s | error=%s. "
        "Continuing without stopping the engine.",
        setting_name,
        symbol,
        error,
    )


def _record(record_event: Callable[..., None] | None, event_type: str, **data: Any) -> None:
    if record_event is not None:
        record_event(event_type, **data)
