import csv
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests

from binance.public_rest import RestHostBackoff, tracked_public_get
from binance.rest_weight import shared_rest_weight_ledger
from binance.retry_policies import build_binance_public_read_policy
from resilience.retry_engine import RetryExhaustedError

BINANCE_FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
MAX_KLINES_LIMIT = 1500

_KLINES_BATCH_POLICY = build_binance_public_read_policy(
    policy_name="klines_batch_fetch",
    budget_key="klines:batch-downloader",
)


@dataclass(frozen=True)
class BinanceKlineCandle:
    timestamp: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class BinanceKlinesDownloadError(RuntimeError):
    pass


def download_klines_to_csv(
    *,
    symbol: str,
    interval: str,
    start: str,
    end: str,
    output_path: str | None = None,
    sleep_seconds: float = 0.2,
) -> Path:
    request = build_download_request(
        symbol=symbol,
        interval=interval,
        start=start,
        end=end,
        output_path=output_path,
    )
    klines = download_klines(
        symbol=request.symbol,
        interval=request.interval,
        start_time_ms=request.start_time_ms,
        end_time_ms=request.end_time_ms,
        sleep_seconds=sleep_seconds,
    )
    candles = klines_to_candles(klines)
    write_candles_csv(candles, request.output_path)
    return request.output_path


@dataclass(frozen=True)
class KlinesDownloadRequest:
    symbol: str
    interval: str
    start: str
    end: str
    start_time_ms: int
    end_time_ms: int
    output_path: Path


def build_download_request(
    *,
    symbol: str,
    interval: str,
    start: str,
    end: str,
    output_path: str | None = None,
) -> KlinesDownloadRequest:
    normalized_symbol = symbol.upper().strip()
    normalized_interval = interval.strip()

    if not normalized_symbol:
        raise ValueError("symbol nao pode ficar vazio")

    if not normalized_interval:
        raise ValueError("interval nao pode ficar vazio")

    start_time_ms = parse_date_to_ms(start, end_of_day=False)
    end_time_ms = parse_date_to_ms(end, end_of_day=True)

    if start_time_ms > end_time_ms:
        raise ValueError("data inicial nao pode ser maior que data final")

    path = (
        Path(output_path)
        if output_path
        else default_output_path(
            symbol=normalized_symbol,
            interval=normalized_interval,
            start=start,
            end=end,
        )
    )

    return KlinesDownloadRequest(
        symbol=normalized_symbol,
        interval=normalized_interval,
        start=start,
        end=end,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        output_path=path,
    )


def default_output_path(*, symbol: str, interval: str, start: str, end: str) -> Path:
    safe_symbol = symbol.upper().strip()
    safe_interval = interval.strip()
    return Path("data") / "market" / f"{safe_symbol}_{safe_interval}_{start}_{end}.csv"


def parse_date_to_ms(value: str, *, end_of_day: bool) -> int:
    value = value.strip()
    if not value:
        raise ValueError("data nao pode ficar vazia")

    try:
        if len(value) == 10:
            parsed = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
            if end_of_day:
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999000)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
    except ValueError as error:
        raise ValueError(f"data invalida: {value}") from error

    return int(parsed.timestamp() * 1000)


def download_klines(
    *,
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int,
    sleep_seconds: float = 0.2,
) -> list[list[Any]]:
    all_klines: list[list[Any]] = []
    current_start = start_time_ms

    while current_start <= end_time_ms:
        batch = _fetch_klines_batch(
            symbol=symbol,
            interval=interval,
            start_time_ms=current_start,
            end_time_ms=end_time_ms,
        )

        if not batch:
            break

        all_klines.extend(batch)
        last_open_time = int(batch[-1][0])
        next_start = last_open_time + 1

        if next_start <= current_start:
            raise BinanceKlinesDownloadError("Resposta invalida: open time nao avancou")

        current_start = next_start

        if len(batch) < MAX_KLINES_LIMIT:
            break

        time.sleep(sleep_seconds)

    return drop_unclosed_klines(all_klines)


def drop_unclosed_klines(
    klines: list[list[Any]],
    *,
    now_ms: int | None = None,
    safety_margin_ms: int = 2_000,
) -> list[list[Any]]:
    """Remove the still-forming candle Binance returns when the range reaches now.

    A partial candle saved to CSV looks complete to backtests and AI training.
    """
    reference_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    closed: list[list[Any]] = []
    for kline in klines:
        if len(kline) > 6:
            try:
                close_time_ms = int(kline[6])
            except (TypeError, ValueError):
                closed.append(kline)
                continue
            if close_time_ms + safety_margin_ms >= reference_ms:
                continue
        closed.append(kline)
    return closed


def _fetch_klines_batch(
    *,
    symbol: str,
    interval: str,
    start_time_ms: int,
    end_time_ms: int,
) -> list[list[Any]]:
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
        "limit": MAX_KLINES_LIMIT,
    }

    def _raw_request():
        try:
            return tracked_public_get(
                BINANCE_FUTURES_KLINES_URL,
                params=params,
                timeout=20,
                source="research:klines",
                ledger=shared_rest_weight_ledger(),
            )
        except RestHostBackoff as error:
            raise BinanceKlinesDownloadError(f"Falha ao baixar klines: {error}") from error

    try:
        response = _KLINES_BATCH_POLICY.run(_raw_request)
    except RetryExhaustedError as error:
        raise BinanceKlinesDownloadError(
            "Falha ao baixar klines: tentativas esgotadas (418/429/5xx)"
        ) from error
    except requests.RequestException as error:
        raise BinanceKlinesDownloadError(f"Erro HTTP ao baixar klines: {error}") from error

    if response.status_code >= 400:
        raise BinanceKlinesDownloadError(
            f"Erro HTTP {response.status_code} ao baixar klines: {response.text}"
        )

    try:
        data = response.json()
    except ValueError as error:
        raise BinanceKlinesDownloadError("Resposta invalida: JSON nao pode ser lido") from error

    if not isinstance(data, list):
        raise BinanceKlinesDownloadError(
            f"Resposta invalida: esperado lista, recebido {type(data)}"
        )

    if any(not isinstance(item, list) or len(item) < 6 for item in data):
        raise BinanceKlinesDownloadError("Resposta invalida: kline sem campos obrigatorios")

    return data


def klines_to_candles(klines: list[list[Any]]) -> list[BinanceKlineCandle]:
    candles = []
    for index, kline in enumerate(klines):
        try:
            candles.append(
                BinanceKlineCandle(
                    timestamp=timestamp_ms_to_iso_utc(int(kline[0])),
                    open=Decimal(str(kline[1])),
                    high=Decimal(str(kline[2])),
                    low=Decimal(str(kline[3])),
                    close=Decimal(str(kline[4])),
                    volume=Decimal(str(kline[5])),
                )
            )
        except (IndexError, InvalidOperation, TypeError, ValueError) as error:
            raise BinanceKlinesDownloadError(
                f"Kline invalido na posicao {index}: {kline}"
            ) from error

    return candles


def write_candles_csv(candles: list[BinanceKlineCandle], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "timestamp": candle.timestamp,
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": str(candle.volume),
                }
            )


def timestamp_ms_to_iso_utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
