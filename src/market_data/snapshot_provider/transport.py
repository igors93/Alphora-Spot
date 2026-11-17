"""REST transport of the snapshot sources: one total time budget per call, retries included."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from binance.futures_client import (
    IMMEDIATE_RETRY_STATUS_CODES,
    BinanceFuturesAPIError,
    BinanceFuturesNetworkError,
)
from binance.rest_weight import rest_source
from market_data.snapshot_provider.settings import (
    SnapshotProviderSettings,
    SnapshotTransportBudgetExceeded,
)
from market_data.snapshot_provider.sources import SOURCE_REST_MARKET, SOURCE_REST_PRIVATE

Timeout = tuple[float, float]

# HTTP statuses a snapshot read may repeat inside its own budget. 418/429 are not among them:
# they ask for Retry-After seconds of silence, far beyond a snapshot budget, and the client
# refuses to send until then anyway (binance.rest_weight).
_RETRYABLE_STATUSES = IMMEDIATE_RETRY_STATUS_CODES

_PRIVATE_REQUESTS: dict[str, tuple[str, str]] = {
    "account_info": ("GET", "/fapi/v2/account"),
    "position_risk": ("GET", "/fapi/v2/positionRisk"),
    "open_orders": ("GET", "/fapi/v1/openOrders"),
    "open_algo_orders": ("GET", "/fapi/v1/openAlgoOrders"),
}


def is_timestamp_retry_error(error: BaseException) -> bool:
    return isinstance(error, BinanceFuturesAPIError) and str(getattr(error, "code", "")) == "-1021"


def is_transient_rest_error(error: BaseException) -> bool:
    if isinstance(error, BinanceFuturesNetworkError):
        return True
    if isinstance(error, BinanceFuturesAPIError):
        return int(getattr(error, "status_code", 0) or 0) in _RETRYABLE_STATUSES
    return False


def timeout_caps(
    timeout: float | Timeout | None,
    *,
    default_connect: float,
    default_read: float,
) -> Timeout:
    if timeout is None:
        return (float(default_connect), float(default_read))
    if isinstance(timeout, tuple):
        connect, read = timeout
        return (float(connect), float(read))
    half = max(0.001, float(timeout) / 2.0)
    return (half, half)


def attempt_timeout(
    *,
    remaining_seconds: float,
    connect_cap_seconds: float,
    read_cap_seconds: float,
) -> Timeout:
    usable = max(0.001, remaining_seconds)
    connect = min(connect_cap_seconds, max(0.001, usable * 0.4))
    read = min(read_cap_seconds, max(0.001, usable - connect))
    if connect + read > usable:
        read = max(0.001, usable - connect)
    return (connect, read)


def private_request_spec(method_name: str, *args: Any) -> dict[str, Any]:
    """The signed request a private client method performs."""

    if method_name not in _PRIVATE_REQUESTS:
        raise RuntimeError(f"Unsupported private REST method for snapshots: {method_name}")
    method, path = _PRIVATE_REQUESTS[method_name]
    params: dict[str, Any] | None
    if method_name == "account_info":
        params = None
    elif method_name == "position_risk":
        symbol = args[0] if args else None
        params = {"symbol": symbol} if symbol else None
    else:
        params = {"symbol": args[0]}
    return {"method": method, "path": path, "params": params}


def ensure_method_supports_timeout(method: Callable[..., Any]) -> None:
    try:
        parameters: tuple[inspect.Parameter, ...] = tuple(
            inspect.signature(method).parameters.values()
        )
    except (TypeError, ValueError):
        parameters = ()

    if any(
        parameter.name == "timeout" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        return

    qualified = getattr(method, "__qualname__", repr(method))
    raise RuntimeError(
        f"Snapshot source called {qualified!r} which does not accept a "
        "'timeout' parameter. Add 'timeout=None' to the method signature "
        "to satisfy the transport budget contract."
    )


class RestTransport:
    """Call the Binance client inside the transport budget a source declared to the engine.

    Each attempt gets a connect/read timeout cut from what is left of the budget; a
    timestamp error (-1021) resyncs the client clock before the next attempt.
    """

    def __init__(
        self,
        *,
        client: Any,
        settings: SnapshotProviderSettings,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.client = client
        self.settings = settings
        self._monotonic = monotonic
        self._sleep = sleep

    def call_client(
        self,
        method: Callable[..., Any],
        *args: Any,
        timeout: float | Timeout | None = None,
        source_name: str = SOURCE_REST_MARKET,
        budget_seconds: float | None = None,
        connect_timeout_seconds: float | None = None,
        read_timeout_seconds: float | None = None,
        max_attempts: int | None = None,
    ) -> Any:
        """Call a client method, enforcing that it accepts a timeout parameter.

        All client methods a source uses must accept a ``timeout`` keyword so the
        engine's transport budget is honoured. The timeout is a total budget for the
        whole blocking operation, including the retries owned by this transport.
        """
        settings = self.settings
        ensure_method_supports_timeout(method)
        connect_cap, read_cap = timeout_caps(
            timeout,
            default_connect=(
                settings.rest_connect_timeout_seconds
                if connect_timeout_seconds is None
                else connect_timeout_seconds
            ),
            default_read=(
                settings.rest_read_timeout_seconds
                if read_timeout_seconds is None
                else read_timeout_seconds
            ),
        )
        return self.call_with_budget(
            lambda timeout_value: method(*args, timeout=timeout_value),
            source_name=source_name,
            budget_seconds=(
                settings.rest_transport_timeout_seconds
                if budget_seconds is None
                else budget_seconds
            ),
            connect_timeout_seconds=connect_cap,
            read_timeout_seconds=read_cap,
            max_attempts=settings.rest_max_attempts if max_attempts is None else max_attempts,
        )

    def call_private_client(self, method_name: str, *args: Any) -> Any:
        """Call one signed read, resyncing the client clock on -1021 inside the budget."""

        signed_once = getattr(self.client, "_signed_request_once", None)
        if not callable(signed_once):
            method = getattr(self.client, method_name)
            return self.call_client(method, *args, source_name=SOURCE_REST_PRIVATE)

        request = private_request_spec(method_name, *args)
        generation_snapshot = getattr(self.client, "_time_sync_generation_snapshot", None)
        sync_if_current = getattr(self.client, "_sync_time_if_generation_current", None)
        expected_generation = generation_snapshot() if callable(generation_snapshot) else None

        def operation(timeout_value: Timeout) -> Any:
            return signed_once(
                request["method"],
                request["path"],
                request["params"],
                timeout=timeout_value,
            )

        def sync_time(timeout_value: Timeout) -> None:
            nonlocal expected_generation
            if expected_generation is not None and callable(sync_if_current):
                _synced, _offset_ms, expected_generation = sync_if_current(
                    expected_generation,
                    timeout=timeout_value,
                )
                return
            fallback_sync = getattr(self.client, "sync_time", None)
            if callable(fallback_sync):
                fallback_sync(timeout=timeout_value)

        return self.call_with_budget(
            operation,
            source_name=SOURCE_REST_PRIVATE,
            budget_seconds=self.settings.rest_transport_timeout_seconds,
            connect_timeout_seconds=self.settings.rest_connect_timeout_seconds,
            read_timeout_seconds=self.settings.rest_read_timeout_seconds,
            max_attempts=self.settings.rest_max_attempts,
            sync_time=sync_time,
        )

    def call_with_budget(
        self,
        operation: Callable[[Timeout], Any],
        *,
        source_name: str,
        budget_seconds: float,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_attempts: int,
        sync_time: Callable[[Timeout], None] | None = None,
    ) -> Any:
        with rest_source(f"snapshot:{source_name}"):
            return self._call_with_budget(
                operation,
                source_name=source_name,
                budget_seconds=budget_seconds,
                connect_timeout_seconds=connect_timeout_seconds,
                read_timeout_seconds=read_timeout_seconds,
                max_attempts=max_attempts,
                sync_time=sync_time,
            )

    def _call_with_budget(
        self,
        operation: Callable[[Timeout], Any],
        *,
        source_name: str,
        budget_seconds: float,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_attempts: int,
        sync_time: Callable[[Timeout], None] | None,
    ) -> Any:
        min_attempt = self.settings.rest_min_attempt_timeout_seconds
        deadline = self._monotonic() + float(budget_seconds)
        last_error: BaseException | None = None

        for attempt in range(1, max(1, int(max_attempts)) + 1):
            remaining = self._remaining(deadline)
            if remaining < min_attempt:
                raise SnapshotTransportBudgetExceeded(
                    f"{source_name} budget exhausted before attempt {attempt}"
                ) from last_error

            timeout_value = attempt_timeout(
                remaining_seconds=remaining,
                connect_cap_seconds=connect_timeout_seconds,
                read_cap_seconds=read_timeout_seconds,
            )
            started = self._monotonic()
            try:
                result = operation(timeout_value)
            except BaseException as error:
                last_error = error
                if self._monotonic() - started > budget_seconds:
                    raise SnapshotTransportBudgetExceeded(
                        f"{source_name} exceeded declared transport budget"
                    ) from error

                timestamp_error = is_timestamp_retry_error(error)
                if not (timestamp_error or is_transient_rest_error(error)):
                    raise
                if attempt >= max_attempts:
                    raise

                if timestamp_error and sync_time is not None:
                    self._sync_before_retry(
                        deadline=deadline,
                        source_name=source_name,
                        sync_time=sync_time,
                        connect_timeout_seconds=connect_timeout_seconds,
                        read_timeout_seconds=read_timeout_seconds,
                        error=error,
                    )

                self._sleep_before_retry(deadline=deadline, source_name=source_name)
                continue

            if self._monotonic() - started > budget_seconds:
                raise SnapshotTransportBudgetExceeded(
                    f"{source_name} exceeded declared transport budget"
                )
            return result

        raise SnapshotTransportBudgetExceeded(f"{source_name} retry loop ended without a result")

    def _remaining(self, deadline: float) -> float:
        return deadline - self._monotonic()

    def _sync_before_retry(
        self,
        *,
        deadline: float,
        source_name: str,
        sync_time: Callable[[Timeout], None],
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        error: BaseException,
    ) -> None:
        remaining_for_sync = self._remaining(deadline)
        if remaining_for_sync < self.settings.rest_min_attempt_timeout_seconds:
            raise SnapshotTransportBudgetExceeded(
                f"{source_name} budget exhausted before sync_time"
            ) from error
        sync_time(
            attempt_timeout(
                remaining_seconds=remaining_for_sync,
                connect_cap_seconds=connect_timeout_seconds,
                read_cap_seconds=read_timeout_seconds,
            )
        )

    def _sleep_before_retry(self, *, deadline: float, source_name: str) -> None:
        delay = self.settings.rest_retry_delay_seconds
        if delay <= 0:
            return
        min_attempt = self.settings.rest_min_attempt_timeout_seconds
        remaining = self._remaining(deadline)
        if remaining <= min_attempt + delay:
            raise SnapshotTransportBudgetExceeded(
                f"{source_name} retry delay would exceed the declared transport budget"
            )
        self._sleep(min(delay, max(0.0, remaining - min_attempt)))


__all__ = [
    "RestTransport",
    "attempt_timeout",
    "ensure_method_supports_timeout",
    "is_timestamp_retry_error",
    "is_transient_rest_error",
    "private_request_spec",
    "timeout_caps",
]
