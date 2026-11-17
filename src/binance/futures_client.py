import hashlib
import hmac
import logging
import threading
import time
from typing import Any
from urllib.parse import urlencode

import requests

from binance.rest_weight import (
    TOO_MANY_REQUESTS_CODE,
    RestWeightLedger,
    host_of,
    parse_retry_after,
    shared_rest_weight_ledger,
)

RequestTimeout = float | tuple[float, float]

_LOG = logging.getLogger("binance.client")

# Binance rejects requests whose timestamp differs from server time by more than
# recv_window ms.  A skew above this threshold is logged as a WARNING so that
# persistent clock-drift on the host is visible in the audit trail.
_CLOCK_SKEW_WARN_MS = 2_000  # 2 s
_CLOCK_SKEW_CRITICAL_MS = 10_000  # 10 s — almost certainly misconfigured NTP

# A signed request (orders included) is accepted by Binance while it is younger
# than recvWindow. Binance recommends <= 5000 ms; a 60 s window would let an
# order delayed in transit execute long after the decision that produced it.
# Clock skew is handled by sync_time()/-1021 resync, not by a wider window.
DEFAULT_RECV_WINDOW_MS = 10_000


# HTTP statuses where a Binance request may succeed if repeated (at the cycle level). 418 and
# 429 succeed only after Retry-After; the client refuses to send before that (see rest_weight).
TRANSIENT_STATUS_CODES = {408, 418, 429, 500, 502, 503, 504}
# Statuses a caller may repeat immediately, inside one operation's retry loop.
IMMEDIATE_RETRY_STATUS_CODES = frozenset({500, 502, 503, 504})


class BinanceFuturesAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: Any,
        message: Any,
        *,
        retry_after_seconds: float | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        # From the Retry-After header of a 429/418: no request before this many seconds.
        self.retry_after_seconds = retry_after_seconds

        super().__init__(
            f"Binance Futures API Error | HTTP {status_code} | code={code} | msg={message}"
        )


class BinanceFuturesRateLimited(BinanceFuturesAPIError):
    """Refused locally: the host is backing off after a 429/418, so nothing was sent."""


class BinanceFuturesNetworkError(Exception):
    pass


class BinanceFuturesMutationBlocked(Exception):
    pass


class BinanceFuturesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        recv_window: int = DEFAULT_RECV_WINDOW_MS,
        timeout: RequestTimeout = 20,
        mutation_enabled: bool = True,
        rest_weight: RestWeightLedger | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window = recv_window
        self.timeout = timeout
        self.time_offset_ms = 0
        self.mutation_enabled = mutation_enabled
        self.session = requests.Session()
        self._time_sync_lock = threading.Lock()
        self._time_sync_generation = 0
        self.rest_weight = rest_weight or shared_rest_weight_ledger()
        self._host = host_of(self.base_url)

        if not self.base_url.startswith("https://"):
            raise ValueError("base_url precisa começar com https://")

        if not self.api_key:
            raise ValueError("api_key não pode ficar vazia")

        if not self.api_secret:
            raise ValueError("api_secret não pode ficar vazia")

        if self.recv_window < 5000:
            raise ValueError("recv_window precisa ser no mínimo 5000")

        if self.recv_window > 60000:
            raise ValueError("recv_window não pode passar de 60000")

    def ping(self, *, timeout: RequestTimeout | None = None) -> dict[str, Any]:
        return self._public_request("GET", "/fapi/v1/ping", timeout=timeout)

    def server_time(self, *, timeout: RequestTimeout | None = None) -> dict[str, Any]:
        return self._public_request("GET", "/fapi/v1/time", timeout=timeout)

    def sync_time(self, *, timeout: RequestTimeout | None = None) -> int:
        """Synchronize local clock with Binance server time.

        Returns the computed offset in milliseconds (positive = server ahead of
        local clock).  Also emits a WARNING when the measured skew exceeds the
        advisory thresholds so that persistent NTP drift is visible in logs.
        """
        local_before_ms = self._local_time_ms()
        data = self.server_time(timeout=timeout)
        local_after_ms = self._local_time_ms()

        rtt_ms = local_after_ms - local_before_ms
        server_time_ms = int(data["serverTime"])
        local_midpoint_ms = (local_before_ms + local_after_ms) // 2

        previous_offset_ms = self.time_offset_ms
        new_offset_ms = server_time_ms - local_midpoint_ms
        self.time_offset_ms = new_offset_ms

        skew_ms = abs(new_offset_ms)
        if skew_ms >= _CLOCK_SKEW_CRITICAL_MS:
            _LOG.warning(
                "Clock skew crítico detectado após sync_time | "
                "offset_ms=%d | skew_ms=%d | rtt_ms=%d | previous_offset_ms=%d | "
                "acao=verifique NTP/chrony no host",
                new_offset_ms,
                skew_ms,
                rtt_ms,
                previous_offset_ms,
            )
        elif skew_ms >= _CLOCK_SKEW_WARN_MS:
            _LOG.warning(
                "Clock skew elevado após sync_time | "
                "offset_ms=%d | skew_ms=%d | rtt_ms=%d | previous_offset_ms=%d",
                new_offset_ms,
                skew_ms,
                rtt_ms,
                previous_offset_ms,
            )

        return new_offset_ms

    def _sync_time_serialized(self, *, timeout: RequestTimeout | None = None) -> int:
        """Thread-safe sync_time: at most one actual request runs at a time."""
        with self._time_sync_lock:
            new_offset_ms = self.sync_time(timeout=timeout)
            self._time_sync_generation += 1
            return new_offset_ms

    def _time_sync_generation_snapshot(self) -> int:
        with self._time_sync_lock:
            return self._time_sync_generation

    def _sync_time_if_generation_current(
        self,
        expected_generation: int,
        *,
        timeout: RequestTimeout | None = None,
    ) -> tuple[bool, int | None, int]:
        """Sync time only if no newer serialized sync already happened.

        Returns ``(synced, offset_ms, generation)``. A skipped sync reuses the
        latest generation from another thread.
        """
        with self._time_sync_lock:
            current_generation = self._time_sync_generation
            if current_generation != expected_generation:
                return False, None, current_generation

            new_offset_ms = self.sync_time(timeout=timeout)
            self._time_sync_generation += 1
            return True, new_offset_ms, self._time_sync_generation

    def exchange_info(self, *, timeout: RequestTimeout | None = None) -> dict[str, Any]:
        return self._public_request("GET", "/fapi/v1/exchangeInfo", timeout=timeout)

    def ticker_price(
        self,
        symbol: str,
        *,
        timeout: RequestTimeout | None = None,
    ) -> dict[str, Any]:
        return self._public_request(
            "GET",
            "/fapi/v2/ticker/price",
            params={"symbol": symbol},
            timeout=timeout,
        )

    def mark_price(
        self,
        symbol: str,
        *,
        timeout: RequestTimeout | None = None,
    ) -> dict[str, Any]:
        return self._public_request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=timeout,
        )

    def account_info(self, *, timeout: RequestTimeout | None = None) -> dict[str, Any]:
        return self._signed_request("GET", "/fapi/v2/account", timeout=timeout)

    def income_history(
        self,
        symbol: str | None = None,
        income_type: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol
        if income_type is not None:
            params["incomeType"] = income_type
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if limit is not None:
            params["limit"] = limit
        return self._signed_request("GET", "/fapi/v1/income", params=params)

    def position_risk(
        self,
        symbol: str | None = None,
        *,
        timeout: RequestTimeout | None = None,
    ) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        return self._signed_request(
            "GET",
            "/fapi/v2/positionRisk",
            params=params,
            timeout=timeout,
        )

    def get_position_mode(self) -> dict[str, Any]:
        return self._signed_request("GET", "/fapi/v1/positionSide/dual")

    def open_orders(
        self,
        symbol: str | None,
        *,
        timeout: RequestTimeout | None = None,
    ) -> list[dict[str, Any]]:
        # symbol=None returns every symbol's open orders (request weight 40, not 1).
        return self._signed_request(
            "GET",
            "/fapi/v1/openOrders",
            params={"symbol": symbol} if symbol else None,
            timeout=timeout,
        )

    def open_algo_orders(
        self,
        symbol: str | None,
        *,
        timeout: RequestTimeout | None = None,
    ) -> list[dict[str, Any]]:
        # symbol=None returns every symbol's open algo orders (request weight 40, not 1).
        return self._signed_request(
            "GET",
            "/fapi/v1/openAlgoOrders",
            params={"symbol": symbol} if symbol else None,
            timeout=timeout,
        )

    def start_user_data_stream(self) -> dict[str, Any]:
        return self._api_key_request("POST", "/fapi/v1/listenKey")

    def keepalive_user_data_stream(self) -> dict[str, Any]:
        return self._api_key_request("PUT", "/fapi/v1/listenKey")

    def close_user_data_stream(self) -> dict[str, Any]:
        return self._api_key_request("DELETE", "/fapi/v1/listenKey")

    def query_order(
        self,
        symbol: str,
        order_id: int | str | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol}

        if order_id is not None:
            params["orderId"] = order_id

        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id

        if "orderId" not in params and "origClientOrderId" not in params:
            raise ValueError("Informe order_id ou orig_client_order_id para consultar ordem")

        return self._signed_request("GET", "/fapi/v1/order", params=params)

    def query_algo_order(
        self,
        algo_id: int | str | None = None,
        client_algo_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}

        if algo_id is not None:
            params["algoId"] = algo_id

        if client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id

        if "algoId" not in params and "clientAlgoId" not in params:
            raise ValueError("Informe algo_id ou client_algo_id para consultar algo order")

        return self._signed_request("GET", "/fapi/v1/algoOrder", params=params)

    def cancel_order(
        self,
        symbol: str,
        order_id: int | str | None = None,
        orig_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed("cancel_order")
        params: dict[str, Any] = {"symbol": symbol}

        if order_id is not None:
            params["orderId"] = order_id

        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id

        if "orderId" not in params and "origClientOrderId" not in params:
            raise ValueError("Informe order_id ou orig_client_order_id para cancelar ordem")

        return self._signed_request(
            "DELETE",
            "/fapi/v1/order",
            params=params,
        )

    def cancel_algo_order(
        self,
        algo_id: int | str | None = None,
        client_algo_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed("cancel_algo_order")
        params: dict[str, Any] = {}

        if algo_id is not None:
            params["algoId"] = algo_id

        if client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id

        if "algoId" not in params and "clientAlgoId" not in params:
            raise ValueError("Informe algo_id ou client_algo_id para cancelar algo order")

        return self._signed_request(
            "DELETE",
            "/fapi/v1/algoOrder",
            params=params,
        )

    def cancel_all_open_orders(self, symbol: str) -> dict[str, Any]:
        self._ensure_mutation_allowed("cancel_all_open_orders")
        return self._signed_request(
            "DELETE",
            "/fapi/v1/allOpenOrders",
            params={"symbol": symbol},
        )

    def cancel_all_open_algo_orders(self, symbol: str) -> dict[str, Any]:
        self._ensure_mutation_allowed("cancel_all_open_algo_orders")
        return self._signed_request(
            "DELETE",
            "/fapi/v1/algoOpenOrders",
            params={"symbol": symbol},
        )

    def change_margin_type(self, symbol: str, margin_type: str) -> dict[str, Any]:
        self._ensure_mutation_allowed("change_margin_type")
        return self._signed_request(
            "POST",
            "/fapi/v1/marginType",
            params={
                "symbol": symbol,
                "marginType": margin_type,
            },
        )

    def change_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        self._ensure_mutation_allowed("change_leverage")
        return self._signed_request(
            "POST",
            "/fapi/v1/leverage",
            params={
                "symbol": symbol,
                "leverage": leverage,
            },
        )

    def new_order(self, **params) -> dict[str, Any]:
        self._ensure_mutation_allowed("new_order")
        return self._signed_request(
            "POST",
            "/fapi/v1/order",
            params=params,
        )

    def test_new_order(self, **params) -> dict[str, Any]:
        return self._signed_request(
            "POST",
            "/fapi/v1/order/test",
            params=params,
        )

    def new_algo_order(self, **params) -> dict[str, Any]:
        self._ensure_mutation_allowed("new_algo_order")
        return self._signed_request(
            "POST",
            "/fapi/v1/algoOrder",
            params=params,
        )

    def close(self) -> None:
        self.session.close()

    def _ensure_mutation_allowed(self, method_name: str) -> None:
        if self.mutation_enabled:
            return

        raise BinanceFuturesMutationBlocked(
            f"{method_name} bloqueado: cliente configurado sem permissoes de mutacao"
        )

    def _public_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: RequestTimeout | None = None,
    ):
        return self._request(
            method=method,
            path=path,
            params=params,
            signed=False,
            timeout=timeout,
        )

    def _api_key_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ):
        headers = {"X-MBX-APIKEY": self.api_key}
        self._refuse_during_backoff(path)

        try:
            response = self.session.request(
                method=method,
                url=f"{self.base_url}{path}",
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise BinanceFuturesNetworkError(
                f"Erro de rede ao chamar Binance Futures: {error}"
            ) from error

        self._record_response(path, response)
        return self._handle_response(response)

    def _signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: RequestTimeout | None = None,
    ):
        from binance.retry_policies import build_binance_timestamp_resync_policy

        policy = build_binance_timestamp_resync_policy(self, timeout=timeout)
        return policy.run(self._signed_request_once, method, path, params, timeout=timeout)

    def _signed_request_once(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: RequestTimeout | None = None,
    ):
        signed_params = dict(params or {})
        signed_params["timestamp"] = self._signed_timestamp_ms()
        signed_params["recvWindow"] = self.recv_window

        query_string = urlencode(signed_params, doseq=True)

        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        signed_params["signature"] = signature

        return self._request(
            method=method,
            path=path,
            params=signed_params,
            signed=True,
            timeout=timeout,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        signed: bool,
        timeout: RequestTimeout | None = None,
    ):
        headers = {}

        if signed:
            headers["X-MBX-APIKEY"] = self.api_key
        self._refuse_during_backoff(path)

        try:
            response = self.session.request(
                method=method,
                url=f"{self.base_url}{path}",
                params=params,
                headers=headers,
                timeout=self.timeout if timeout is None else timeout,
            )
        except requests.RequestException as error:
            raise BinanceFuturesNetworkError(
                f"Erro de rede ao chamar Binance Futures: {error}"
            ) from error

        self._record_response(path, response)
        return self._handle_response(response)

    def _refuse_during_backoff(self, path: str) -> None:
        """Fail fast while the host backs off: a request sent during a ban extends the ban."""

        backoff = self.rest_weight.active_backoff(self._host)
        if backoff is None:
            return
        raise BinanceFuturesRateLimited(
            status_code=backoff.status_code,
            code=TOO_MANY_REQUESTS_CODE,
            message=(
                f"requisicao {path} nao enviada: {self._host} em back-off apos HTTP "
                f"{backoff.status_code} por mais {backoff.remaining_seconds:.1f}s"
            ),
            retry_after_seconds=backoff.remaining_seconds,
        )

    def _record_response(self, path: str, response: Any) -> None:
        try:
            self.rest_weight.record(
                host=self._host,
                path=path,
                status_code=int(getattr(response, "status_code", 0) or 0),
                headers=getattr(response, "headers", None),
            )
        except Exception:
            _LOG.debug("REST weight ledger failed to record a response", exc_info=True)

    def _signed_timestamp_ms(self) -> int:
        return self._local_time_ms() + self.time_offset_ms

    @staticmethod
    def _local_time_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _handle_response(response: requests.Response):
        if not response.text:
            data = {}
        else:
            try:
                data = response.json()
            except ValueError:
                data = {"raw": response.text}

        if response.status_code >= 400:
            if not isinstance(data, dict):
                data = {"raw": data}
            raise BinanceFuturesAPIError(
                status_code=response.status_code,
                code=data.get("code"),
                message=data.get("msg", data),
                retry_after_seconds=parse_retry_after(getattr(response, "headers", None)),
            )

        return data
