import hashlib
import hmac
import logging
import threading
import time
from decimal import Decimal, InvalidOperation
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
        spot_mode: bool | None = None,
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
        if spot_mode is None:
            lowered_host = self._host.lower()
            self.spot_mode = not ("fapi" in lowered_host or "binancefuture" in lowered_host)
        else:
            self.spot_mode = bool(spot_mode)
        self._spot_state_lock = threading.RLock()
        self._spot_balances: dict[str, tuple[Decimal, Decimal]] = {}
        self._spot_entry_prices: dict[str, Decimal] = {}
        self._spot_position_quantities: dict[str, Decimal] = {}
        self._spot_last_prices: dict[str, Decimal] = {}
        self._spot_algo_orders: dict[str, dict[str, Any]] = {}
        self._last_listen_key = ""

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
        path = "/api/v3/ping" if self.spot_mode else "/fapi/v1/ping"
        return self._public_request("GET", path, timeout=timeout)

    def server_time(self, *, timeout: RequestTimeout | None = None) -> dict[str, Any]:
        path = "/api/v3/time" if self.spot_mode else "/fapi/v1/time"
        return self._public_request("GET", path, timeout=timeout)

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
        path = "/api/v3/exchangeInfo" if self.spot_mode else "/fapi/v1/exchangeInfo"
        return self._public_request("GET", path, timeout=timeout)

    def ticker_price(
        self,
        symbol: str,
        *,
        timeout: RequestTimeout | None = None,
    ) -> dict[str, Any]:
        path = "/api/v3/ticker/price" if self.spot_mode else "/fapi/v2/ticker/price"
        data = self._public_request(
            "GET",
            path,
            params={"symbol": symbol},
            timeout=timeout,
        )
        if self.spot_mode and isinstance(data, dict) and data.get("price") is not None:
            try:
                price_dec = Decimal(str(data["price"]))
                if price_dec > 0:
                    with self._spot_state_lock:
                        self._spot_last_prices[str(symbol).upper().strip()] = price_dec
            except (InvalidOperation, ValueError, TypeError):
                pass
        return data

    def mark_price(
        self,
        symbol: str,
        *,
        timeout: RequestTimeout | None = None,
    ) -> dict[str, Any]:
        if self.spot_mode:
            data = self.ticker_price(symbol, timeout=timeout)
            price = str(data.get("price", "0")) if isinstance(data, dict) else "0"
            return {
                "symbol": symbol,
                "markPrice": price,
                "indexPrice": price,
                "lastFundingRate": "0",
                "nextFundingTime": 0,
            }
        return self._public_request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=timeout,
        )

    def account_info(self, *, timeout: RequestTimeout | None = None) -> dict[str, Any]:
        if self.spot_mode:
            data = self._signed_request("GET", "/api/v3/account", timeout=timeout)
            return self._normalize_spot_account_info(data)
        return self._signed_request("GET", "/fapi/v2/account", timeout=timeout)

    def income_history(
        self,
        symbol: str | None = None,
        income_type: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if self.spot_mode:
            return []
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
        if self.spot_mode:
            return self._spot_position_risk(symbol=symbol, timeout=timeout)
        params = {"symbol": symbol} if symbol else None
        return self._signed_request(
            "GET",
            "/fapi/v2/positionRisk",
            params=params,
            timeout=timeout,
        )

    def get_position_mode(self) -> dict[str, Any]:
        if self.spot_mode:
            return {"dualSidePosition": False}
        return self._signed_request("GET", "/fapi/v1/positionSide/dual")

    def open_orders(
        self,
        symbol: str | None,
        *,
        timeout: RequestTimeout | None = None,
    ) -> list[dict[str, Any]]:
        # symbol=None returns every symbol's open orders (request weight 40, not 1).
        path = "/api/v3/openOrders" if self.spot_mode else "/fapi/v1/openOrders"
        orders = self._signed_request(
            "GET",
            path,
            params={"symbol": symbol} if symbol else None,
            timeout=timeout,
        )
        if self.spot_mode:
            return [order for order in orders if int(order.get("orderListId", -1)) < 0]
        return orders

    def open_algo_orders(
        self,
        symbol: str | None,
        *,
        timeout: RequestTimeout | None = None,
    ) -> list[dict[str, Any]]:
        if self.spot_mode:
            return [
                self._normalize_spot_protection_order(order)
                for order in self._signed_request(
                    "GET", "/api/v3/openOrders",
                    params={"symbol": symbol} if symbol else None,
                    timeout=timeout,
                )
                if int(order.get("orderListId", -1)) >= 0
            ]
        # symbol=None returns every symbol's open algo orders (request weight 40, not 1).
        return self._signed_request(
            "GET",
            "/fapi/v1/openAlgoOrders",
            params={"symbol": symbol} if symbol else None,
            timeout=timeout,
        )

    def start_user_data_stream(self) -> dict[str, Any]:
        if self.spot_mode:
            response = self._api_key_request("POST", "/api/v3/userDataStream")
            if isinstance(response, dict):
                self._last_listen_key = str(response.get("listenKey", "") or "").strip()
            return response
        return self._api_key_request("POST", "/fapi/v1/listenKey")

    def keepalive_user_data_stream(self) -> dict[str, Any]:
        if self.spot_mode:
            params = {"listenKey": self._last_listen_key} if self._last_listen_key else None
            return self._api_key_request("PUT", "/api/v3/userDataStream", params=params)
        return self._api_key_request("PUT", "/fapi/v1/listenKey")

    def close_user_data_stream(self) -> dict[str, Any]:
        if self.spot_mode:
            params = {"listenKey": self._last_listen_key} if self._last_listen_key else None
            self._last_listen_key = ""
            return self._api_key_request("DELETE", "/api/v3/userDataStream", params=params)
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

        path = "/api/v3/order" if self.spot_mode else "/fapi/v1/order"
        response = self._signed_request("GET", path, params=params)
        if self.spot_mode and isinstance(response, dict):
            return self._normalize_spot_order_response(response)
        return response

    def query_algo_order(
        self,
        algo_id: int | str | None = None,
        client_algo_id: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}

        if algo_id is not None:
            params["algoId"] = algo_id

        if client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id

        if "algoId" not in params and "clientAlgoId" not in params:
            raise ValueError("Informe algo_id ou client_algo_id para consultar algo order")

        if self.spot_mode:
            resolved_symbol = symbol or self._spot_algo_orders.get(str(client_algo_id), {}).get("symbol")
            if not resolved_symbol:
                raise ValueError("symbol obrigatorio para consultar protecao Spot")
            order = self.query_order(
                symbol=resolved_symbol,
                order_id=algo_id if client_algo_id is None else None,
                orig_client_order_id=client_algo_id,
            )
            return self._normalize_spot_protection_order(order)

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

        path = "/api/v3/order" if self.spot_mode else "/fapi/v1/order"
        return self._signed_request(
            "DELETE",
            path,
            params=params,
        )

    def cancel_algo_order(
        self,
        algo_id: int | str | None = None,
        client_algo_id: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed("cancel_algo_order")
        params: dict[str, Any] = {}

        if algo_id is not None:
            params["algoId"] = algo_id

        if client_algo_id is not None:
            params["clientAlgoId"] = client_algo_id

        if "algoId" not in params and "clientAlgoId" not in params:
            raise ValueError("Informe algo_id ou client_algo_id para cancelar algo order")

        if self.spot_mode:
            resolved_symbol = symbol or self._spot_algo_orders.get(str(client_algo_id), {}).get("symbol")
            if not resolved_symbol:
                raise ValueError("symbol obrigatorio para cancelar protecao Spot")
            order = self.query_algo_order(algo_id, client_algo_id, resolved_symbol)
            order_list_id = int(order.get("orderListId", -1))
            if order_list_id < 0:
                raise ValueError("ordem Spot nao pertence a uma lista OCO")
            return self._signed_request(
                "DELETE", "/api/v3/orderList",
                params={"symbol": resolved_symbol, "orderListId": order_list_id},
            )

        return self._signed_request(
            "DELETE",
            "/fapi/v1/algoOrder",
            params=params,
        )

    def cancel_all_open_orders(self, symbol: str) -> dict[str, Any]:
        self._ensure_mutation_allowed("cancel_all_open_orders")
        if self.spot_mode:
            try:
                return self._signed_request(
                    "DELETE",
                    "/api/v3/openOrders",
                    params={"symbol": symbol},
                )
            except BinanceFuturesAPIError as error:
                if error.code == -2011:
                    return {"code": 200, "msg": "No open orders to cancel"}
                raise
        return self._signed_request(
            "DELETE",
            "/fapi/v1/allOpenOrders",
            params={"symbol": symbol},
        )

    def cancel_all_open_algo_orders(self, symbol: str) -> dict[str, Any]:
        self._ensure_mutation_allowed("cancel_all_open_algo_orders")
        if self.spot_mode:
            responses = []
            seen_lists: set[int] = set()
            for order in self.open_algo_orders(symbol):
                if not str(order.get("clientAlgoId", "")).startswith("ALPHORA_"):
                    continue
                order_list_id = int(order["orderListId"])
                if order_list_id in seen_lists:
                    continue
                seen_lists.add(order_list_id)
                responses.append(self.cancel_algo_order(algo_id=order["orderId"], symbol=symbol))
            return {"canceled": responses}
        return self._signed_request(
            "DELETE",
            "/fapi/v1/algoOpenOrders",
            params={"symbol": symbol},
        )

    def change_margin_type(self, symbol: str, margin_type: str) -> dict[str, Any]:
        self._ensure_mutation_allowed("change_margin_type")
        if self.spot_mode:
            return {"code": 200, "msg": "SPOT_NO_MARGIN_TYPE"}
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
        if self.spot_mode:
            return {"symbol": symbol, "leverage": 1}
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
        if self.spot_mode:
            return self._spot_new_order(**params)
        return self._signed_request(
            "POST",
            "/fapi/v1/order",
            params=params,
        )

    def test_new_order(self, **params) -> dict[str, Any]:
        if self.spot_mode:
            clean_params = self._strip_futures_only_order_params(params)
            return self._signed_request(
                "POST",
                "/api/v3/order/test",
                params=clean_params,
            )
        return self._signed_request(
            "POST",
            "/fapi/v1/order/test",
            params=params,
        )

    def new_algo_order(self, **params) -> dict[str, Any]:
        self._ensure_mutation_allowed("new_algo_order")
        if self.spot_mode:
            raise ValueError("Spot requer OCO real via new_spot_oco_order")
        return self._signed_request(
            "POST",
            "/fapi/v1/algoOrder",
            params=params,
        )

    def new_spot_oco_order(
        self, *, symbol: str, quantity: str, stop_price: str, take_price: str,
        list_client_order_id: str, stop_client_order_id: str, take_client_order_id: str,
    ) -> dict[str, Any]:
        self._ensure_mutation_allowed("new_spot_oco_order")
        if not self.spot_mode:
            raise ValueError("OCO Spot indisponivel em Futures")
        params = {
            "symbol": symbol, "side": "SELL", "quantity": quantity,
            "aboveType": "LIMIT_MAKER", "abovePrice": take_price,
            "belowType": "STOP_LOSS", "belowStopPrice": stop_price,
            "listClientOrderId": list_client_order_id,
            "aboveClientOrderId": take_client_order_id,
            "belowClientOrderId": stop_client_order_id,
            "newOrderRespType": "RESULT",
        }
        try:
            response = self._signed_request("POST", "/api/v3/orderList/oco", params=params)
        except BinanceFuturesNetworkError:
            response = self._signed_request(
                "GET", "/api/v3/orderList",
                params={"symbol": symbol, "origClientOrderId": list_client_order_id},
            )
        for client_id in (stop_client_order_id, take_client_order_id):
            self._spot_algo_orders[client_id] = {"symbol": symbol}
        return response

    @staticmethod
    def _normalize_spot_protection_order(order: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(order)
        order_type = str(order.get("type") or order.get("origType") or "")
        if order_type in {"STOP_LOSS", "STOP_LOSS_LIMIT"}:
            normalized["orderType"] = "STOP_MARKET"
            normalized["triggerPrice"] = order.get("stopPrice", "0")
        elif order_type in {"LIMIT_MAKER", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"}:
            normalized["orderType"] = "TAKE_PROFIT_MARKET"
            normalized["triggerPrice"] = order.get("price", "0")
        normalized["clientAlgoId"] = order.get("clientOrderId", "")
        normalized["algoId"] = order.get("orderId")
        normalized["spotOco"] = True
        return normalized

    def _normalize_spot_account_info(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {"assets": [], "balances": []}
        balances = data.get("balances")
        if not isinstance(balances, list):
            return data
        assets: list[dict[str, Any]] = []
        parsed_balances: dict[str, tuple[Decimal, Decimal]] = {}
        for item in balances:
            if not isinstance(item, dict):
                continue
            asset_name = str(item.get("asset", "") or "").upper().strip()
            if not asset_name:
                continue
            try:
                free_dec = Decimal(str(item.get("free", "0") or "0"))
                locked_dec = Decimal(str(item.get("locked", "0") or "0"))
            except (InvalidOperation, ValueError, TypeError):
                free_dec = Decimal("0")
                locked_dec = Decimal("0")
            total_dec = free_dec + locked_dec
            parsed_balances[asset_name] = (free_dec, locked_dec)
            assets.append(
                {
                    "asset": asset_name,
                    "walletBalance": str(total_dec),
                    "availableBalance": str(free_dec),
                    "marginBalance": str(total_dec),
                    "unrealizedProfit": "0",
                    "free": str(free_dec),
                    "locked": str(locked_dec),
                }
            )
        with self._spot_state_lock:
            self._spot_balances = parsed_balances
        enriched = dict(data)
        if "assets" not in enriched:
            enriched["assets"] = assets
        usdt_free, usdt_locked = parsed_balances.get("USDT", (Decimal("0"), Decimal("0")))
        enriched.setdefault("availableBalance", str(usdt_free))
        enriched.setdefault("totalWalletBalance", str(usdt_free + usdt_locked))
        return enriched

    def _spot_position_risk(
        self,
        *,
        symbol: str | None = None,
        timeout: RequestTimeout | None = None,
    ) -> list[dict[str, Any]]:
        account_data = self.account_info(timeout=timeout)
        if isinstance(account_data, dict) and isinstance(account_data.get("positions"), list):
            # Support test doubles that return futures-shaped positions on account_info.
            raw_positions = account_data["positions"]
            if symbol:
                norm = str(symbol).upper().strip()
                return [p for p in raw_positions if str(p.get("symbol", "")).upper() == norm]
            return list(raw_positions)

        with self._spot_state_lock:
            balances_snapshot = dict(self._spot_balances)
            tracked_symbols = set(self._spot_entry_prices.keys()) | set(
                self._spot_position_quantities.keys()
            )

        if symbol is not None:
            target_symbols = [str(symbol).upper().strip()]
        else:
            quote_assets = {"USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD"}
            held_symbols = {
                f"{asset}USDT"
                for asset, (free_qty, locked_qty) in balances_snapshot.items()
                if asset not in quote_assets and (free_qty + locked_qty) > 0
            }
            target_symbols = sorted(held_symbols | tracked_symbols)

        results: list[dict[str, Any]] = []
        for sym in target_symbols:
            base_asset = _spot_base_asset(sym)
            free_qty, locked_qty = balances_snapshot.get(
                base_asset, (Decimal("0"), Decimal("0"))
            )
            total_qty = free_qty + locked_qty
            if total_qty <= 0:
                with self._spot_state_lock:
                    self._spot_entry_prices.pop(sym, None)
                    self._spot_position_quantities.pop(sym, None)
                results.append(
                    {
                        "symbol": sym,
                        "positionAmt": "0",
                        "entryPrice": "0",
                        "unRealizedProfit": "0",
                        "positionSide": "BOTH",
                    }
                )
                continue

            with self._spot_state_lock:
                current_price = self._spot_last_prices.get(sym)
            if current_price is None or current_price <= 0:
                try:
                    ticker = self.ticker_price(sym, timeout=timeout)
                    current_price = Decimal(str(ticker.get("price", "0") or "0"))
                except Exception:
                    current_price = Decimal("0")

            with self._spot_state_lock:
                entry_price = self._spot_entry_prices.get(sym) or current_price

            ref_price = current_price if current_price > 0 else entry_price
            notional = total_qty * ref_price if ref_price > 0 else total_qty
            if ref_price > 0 and notional < Decimal("5"):
                # Ignore tiny Spot fee dust (< $5 USDT) as flat.
                with self._spot_state_lock:
                    self._spot_entry_prices.pop(sym, None)
                    self._spot_position_quantities.pop(sym, None)
                results.append(
                    {
                        "symbol": sym,
                        "positionAmt": "0",
                        "entryPrice": "0",
                        "unRealizedProfit": "0",
                        "positionSide": "BOTH",
                    }
                )
                continue

            effective_qty = total_qty
            unrealized_pnl = (
                (current_price - entry_price) * effective_qty
                if current_price > 0 and entry_price > 0
                else Decimal("0")
            )
            results.append(
                {
                    "symbol": sym,
                    "positionAmt": str(effective_qty),
                    "entryPrice": str(entry_price),
                    "unRealizedProfit": str(unrealized_pnl),
                    "positionSide": "BOTH",
                }
            )
        return results

    @staticmethod
    def _strip_futures_only_order_params(params: dict[str, Any]) -> dict[str, Any]:
        clean = dict(params)
        if "order_type" in clean and "type" not in clean:
            clean["type"] = clean.pop("order_type")
        for key in ("reduceOnly", "closePosition", "positionSide", "workingType", "priceProtect"):
            clean.pop(key, None)
        return clean

    def _spot_new_order(self, **params) -> dict[str, Any]:
        clean_params = self._strip_futures_only_order_params(params)
        symbol = str(clean_params.get("symbol", "") or "").upper().strip()
        side = str(clean_params.get("side", "") or "").upper().strip()
        requested_qty_raw = clean_params.get("quantity")
        requested_qty: Decimal | None = None
        if requested_qty_raw is not None:
            try:
                requested_qty = Decimal(str(requested_qty_raw))
            except (InvalidOperation, ValueError, TypeError):
                requested_qty = None

        # On Spot SELL, if base-asset commission slightly reduced free balance below
        # the nominal position quantity, clamp to available free balance so the close succeeds.
        if side == "SELL" and symbol and requested_qty is not None and requested_qty > 0:
            base_asset = _spot_base_asset(symbol)
            with self._spot_state_lock:
                free_qty, _ = self._spot_balances.get(base_asset, (Decimal("0"), Decimal("0")))
            if Decimal("0") < free_qty < requested_qty:
                clean_params["quantity"] = str(free_qty.normalize())

        response = self._signed_request(
            "POST",
            "/api/v3/order",
            params=clean_params,
        )
        if not isinstance(response, dict):
            return response

        normalized = self._normalize_spot_order_response(response)
        status = str(normalized.get("status", "") or "").upper()
        if symbol and status in {"FILLED", "PARTIALLY_FILLED"}:
            try:
                exec_qty = Decimal(str(normalized.get("executedQty", "0") or "0"))
                avg_price = Decimal(str(normalized.get("avgPrice", "0") or "0"))
            except (InvalidOperation, ValueError, TypeError):
                exec_qty = Decimal("0")
                avg_price = Decimal("0")

            with self._spot_state_lock:
                if side == "BUY" and exec_qty > 0:
                    if avg_price <= 0:
                        avg_price = self._spot_last_prices.get(symbol, Decimal("0"))
                    if avg_price > 0:
                        self._spot_entry_prices[symbol] = avg_price
                    self._spot_position_quantities[symbol] = exec_qty

        return normalized

    @staticmethod
    def _normalize_spot_order_response(
        response: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = dict(response)
        cum_quote_raw = normalized.get(
            "cumQuote",
            normalized.get("cummulativeQuoteQty", normalized.get("cumQuoteQty", "0")),
        )
        exec_qty_raw = normalized.get("executedQty", "0")
        try:
            cum_quote = Decimal(str(cum_quote_raw or "0"))
            exec_qty = Decimal(str(exec_qty_raw or "0"))
        except (InvalidOperation, ValueError, TypeError):
            cum_quote = Decimal("0")
            exec_qty = Decimal("0")

        if cum_quote > 0 and "cumQuote" not in normalized:
            normalized["cumQuote"] = str(cum_quote)

        avg_raw = normalized.get("avgPrice")
        try:
            avg_dec = Decimal(str(avg_raw or "0"))
        except (InvalidOperation, ValueError, TypeError):
            avg_dec = Decimal("0")

        if avg_dec <= 0 and exec_qty > 0 and cum_quote > 0:
            normalized["avgPrice"] = str(cum_quote / exec_qty)

        return normalized

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


def _spot_base_asset(symbol: str) -> str:
    normalized = str(symbol or "").upper().strip()
    for quote in ("USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD"):
        if normalized.endswith(quote) and len(normalized) > len(quote):
            return normalized[: -len(quote)]
    return normalized


BinanceSpotClient = BinanceFuturesClient
