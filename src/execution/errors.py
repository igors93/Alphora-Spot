from enum import Enum

from binance.futures_client import (
    TRANSIENT_STATUS_CODES,
    BinanceFuturesAPIError,
    BinanceFuturesNetworkError,
)


class EnginePaused(RuntimeError):
    pass


class ProtectionTriggerAlreadyBreached(RuntimeError):
    pass


# Binance order-not-found codes: -2011 "Unknown order sent", -2013 "Order does not exist".
# Both mean the order is already gone and cancel should be treated as a success.
ORDER_NOT_FOUND_CODES: frozenset[int] = frozenset({-2011, -2013})


class UnknownOrderStatus(RuntimeError):
    def __init__(
        self,
        *,
        client_order_id: str,
        purpose: str,
        original_error: Exception,
    ):
        self.client_order_id = client_order_id
        self.purpose = purpose
        self.original_error = original_error
        super().__init__(f"Status desconhecido para {purpose} {client_order_id}: {original_error}")


class ErrorKind(Enum):
    TRANSIENT = "TRANSIENT"
    FATAL = "FATAL"
    UNKNOWN_ORDER_STATUS = "UNKNOWN_ORDER_STATUS"


FATAL_BINANCE_CODES = {-2015, -1121, -4061}


def classify_error(error: Exception) -> ErrorKind:
    if isinstance(error, UnknownOrderStatus):
        return ErrorKind.UNKNOWN_ORDER_STATUS

    if isinstance(error, BinanceFuturesNetworkError):
        return ErrorKind.TRANSIENT

    if isinstance(error, BinanceFuturesAPIError):
        if is_unknown_execution_status(error):
            return ErrorKind.UNKNOWN_ORDER_STATUS

        if error.code in FATAL_BINANCE_CODES:
            return ErrorKind.FATAL

        if error.status_code in TRANSIENT_STATUS_CODES:
            return ErrorKind.TRANSIENT

    return ErrorKind.FATAL


def is_unknown_execution_status(error: BinanceFuturesAPIError) -> bool:
    if error.status_code != 503:
        return False

    message = str(error.message).lower()
    return "unknown error" in message or "execution status is unknown" in message
