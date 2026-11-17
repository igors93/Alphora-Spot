from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from binance.public_rest import RestHostBackoff, tracked_public_get
from binance.rest_weight import RestWeightLedger, shared_rest_weight_ledger
from binance.retry_policies import build_binance_public_read_policy
from domain.candle import Candle
from domain.timeframe import expected_close_time_ms, normalize_timeframe
from market_data.candle_validation import validate_candle
from resilience.retry_engine import RetryExhaustedError

BINANCE_FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
_MAX_LIMIT = 1500
# A kline is only treated as closed this long after its close_time on the local
# clock. A host clock running ahead of Binance would otherwise hand strategies a
# candle that is still forming on the exchange.
CLOSED_CANDLE_SAFETY_MARGIN_MS = 2_000

_KLINES_POLICY = build_binance_public_read_policy(
    policy_name="candle_provider_fetch",
    budget_key="klines:candle-provider",
)


class CandleProviderError(RuntimeError):
    pass


class CandleDataProvider:
    """Fetch closed candles from Binance Futures REST API."""

    def __init__(
        self,
        *,
        base_url: str = BINANCE_FUTURES_KLINES_URL,
        rest_weight: RestWeightLedger | None = None,
    ):
        self._base_url = base_url
        self._rest_weight = rest_weight or shared_rest_weight_ledger()

    def fetch_closed_candles(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
        end_time_ms: int | None = None,
    ) -> list[Candle]:
        """Return up to `limit` closed candles ending at or before `end_time_ms`."""
        normalized_symbol = str(symbol or "").upper().strip()
        normalized_interval = normalize_timeframe(interval)
        if limit <= 0:
            return []

        # Request limit+1 to detect the in-progress candle; drop it if present.
        raw = self._fetch_batch(
            symbol=normalized_symbol,
            interval=normalized_interval,
            limit=min(limit + 1, _MAX_LIMIT),
            end_time_ms=end_time_ms,
        )

        candles: list[Candle] = []
        now_ms = int(time.time() * 1000)
        for kline in raw:
            candle = _kline_to_candle(
                kline,
                symbol=normalized_symbol,
                interval=normalized_interval,
                now_ms=now_ms,
            )
            if candle is None:
                continue
            if not candle.is_closed:
                continue
            candles.append(candle)

        return candles[-limit:] if len(candles) > limit else candles

    def fetch_since(
        self,
        *,
        symbol: str,
        interval: str,
        since_open_time_ms: int,
        limit: int = _MAX_LIMIT,
    ) -> list[Candle]:
        """Return closed candles with open_time_ms > since_open_time_ms."""
        normalized_symbol = str(symbol or "").upper().strip()
        normalized_interval = normalize_timeframe(interval)
        start_ms = int(since_open_time_ms) + 1

        raw = self._fetch_batch(
            symbol=normalized_symbol,
            interval=normalized_interval,
            limit=min(limit + 1, _MAX_LIMIT),
            start_time_ms=start_ms,
        )

        candles: list[Candle] = []
        now_ms = int(time.time() * 1000)
        for kline in raw:
            candle = _kline_to_candle(
                kline,
                symbol=normalized_symbol,
                interval=normalized_interval,
                now_ms=now_ms,
            )
            if candle is None:
                continue
            if not candle.is_closed:
                continue
            if candle.open_time_ms <= since_open_time_ms:
                continue
            candles.append(candle)

        return candles

    def _fetch_batch(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)

        base_url = self._base_url

        def _do_request():
            try:
                return tracked_public_get(
                    base_url, params=params, timeout=20, source="candles", ledger=self._rest_weight
                )
            except RestHostBackoff as error:
                raise CandleProviderError(
                    f"klines {symbol}/{interval} nao buscados: {error}"
                ) from error

        try:
            response = _KLINES_POLICY.run(_do_request)
        except RetryExhaustedError as error:
            raise CandleProviderError(
                f"Falha ao buscar klines {symbol}/{interval}: tentativas esgotadas"
            ) from error
        except requests.RequestException as error:
            raise CandleProviderError(
                f"Erro HTTP ao buscar klines {symbol}/{interval}: {error}"
            ) from error

        if response.status_code >= 400:
            raise CandleProviderError(
                f"Erro HTTP {response.status_code} ao buscar klines {symbol}/{interval}: "
                f"{response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as error:
            raise CandleProviderError("Resposta invalida: JSON invalido") from error

        if not isinstance(data, list):
            raise CandleProviderError(
                f"Resposta invalida: esperado lista, recebido {type(data).__name__}"
            )
        return data


def _kline_to_candle(
    kline: list[Any],
    *,
    symbol: str,
    interval: str,
    now_ms: int,
) -> Candle | None:
    if not isinstance(kline, list) or len(kline) < 6:
        return None
    try:
        open_time_ms = int(kline[0])
        close_time_ms = (
            int(kline[6]) if len(kline) > 6 else expected_close_time_ms(open_time_ms, interval)
        )
        open_price = Decimal(str(kline[1]))
        high = Decimal(str(kline[2]))
        low = Decimal(str(kline[3]))
        close = Decimal(str(kline[4]))
        volume = Decimal(str(kline[5]))
    except (IndexError, InvalidOperation, TypeError, ValueError):
        return None

    is_closed = close_time_ms + CLOSED_CANDLE_SAFETY_MARGIN_MS < now_ms

    candle = Candle(
        symbol=symbol,
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=close_time_ms,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        is_closed=is_closed,
        source="binance_rest",
    )
    try:
        validate_candle(candle, now_ms=now_ms + 60_000)
    except Exception:
        return None
    return candle
