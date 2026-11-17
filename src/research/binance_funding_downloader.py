from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests

from binance.public_rest import RestHostBackoff, tracked_public_get
from binance.rest_weight import shared_rest_weight_ledger
from binance.retry_policies import build_binance_public_read_policy
from research.binance_klines_downloader import parse_date_to_ms
from resilience.retry_engine import RetryExhaustedError

BINANCE_FUTURES_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
MAX_FUNDING_LIMIT = 1000

_FUNDING_BATCH_POLICY = build_binance_public_read_policy(
    policy_name="funding_batch_fetch",
    budget_key="funding:batch-downloader",
)


class BinanceFundingDownloadError(RuntimeError):
    pass


def download_funding_to_csv(
    *,
    symbol: str,
    start: str,
    end: str,
    output_path: str | Path | None = None,
    sleep_seconds: float = 0.2,
) -> Path:
    normalized_symbol = str(symbol or "").upper().strip()
    if not normalized_symbol:
        raise ValueError("symbol nao pode ficar vazio")
    start_time_ms = parse_date_to_ms(start, end_of_day=False)
    end_time_ms = parse_date_to_ms(end, end_of_day=True)
    if start_time_ms > end_time_ms:
        raise ValueError("data inicial nao pode ser maior que data final")
    path = (
        Path(output_path)
        if output_path
        else Path("data") / "market" / f"{normalized_symbol}_funding_{start}_{end}.csv"
    )
    rows = download_funding_rates(
        symbol=normalized_symbol,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        sleep_seconds=sleep_seconds,
    )
    write_funding_csv(rows, path)
    return path


def download_funding_rates(
    *,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    sleep_seconds: float = 0.2,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current_start = int(start_time_ms)
    while current_start <= end_time_ms:
        batch = _fetch_funding_batch(
            symbol=symbol,
            start_time_ms=current_start,
            end_time_ms=end_time_ms,
        )
        if not batch:
            break
        rows.extend(batch)
        last_timestamp = int(batch[-1]["fundingTime"])
        next_start = last_timestamp + 1
        if next_start <= current_start:
            raise BinanceFundingDownloadError("Resposta invalida: fundingTime nao avancou")
        current_start = next_start
        if len(batch) < MAX_FUNDING_LIMIT:
            break
        time.sleep(max(0.0, float(sleep_seconds)))
    unique = {int(row["fundingTime"]): row for row in rows}
    return [unique[timestamp] for timestamp in sorted(unique)]


def _fetch_funding_batch(
    *,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
) -> list[dict[str, Any]]:
    params = {
        "symbol": symbol,
        "startTime": start_time_ms,
        "endTime": end_time_ms,
        "limit": MAX_FUNDING_LIMIT,
    }

    def _raw_request():
        try:
            return tracked_public_get(
                BINANCE_FUTURES_FUNDING_URL,
                params=params,
                timeout=20,
                source="research:funding",
                ledger=shared_rest_weight_ledger(),
            )
        except RestHostBackoff as error:
            raise BinanceFundingDownloadError(f"Falha ao baixar funding: {error}") from error

    try:
        response = _FUNDING_BATCH_POLICY.run(_raw_request)
    except RetryExhaustedError as error:
        raise BinanceFundingDownloadError(
            "Falha ao baixar funding: tentativas esgotadas (418/429/5xx)"
        ) from error
    except requests.RequestException as error:
        raise BinanceFundingDownloadError(f"Erro HTTP ao baixar funding: {error}") from error
    if response.status_code >= 400:
        raise BinanceFundingDownloadError(
            f"Erro HTTP {response.status_code} ao baixar funding: {response.text}"
        )
    payload = response.json()
    if not isinstance(payload, list):
        raise BinanceFundingDownloadError("Resposta de funding precisa ser uma lista")
    return [dict(item) for item in payload if isinstance(item, dict)]


def write_funding_csv(rows: list[dict[str, Any]], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    prepared: list[tuple[str, Decimal]] = []
    for row in rows:
        try:
            timestamp_ms = int(row["fundingTime"])
            rate = Decimal(str(row["fundingRate"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise BinanceFundingDownloadError("Registro de funding invalido") from error
        if not rate.is_finite():
            raise BinanceFundingDownloadError("Funding rate nao finita")
        timestamp = datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=timezone.utc,
        ).isoformat()
        prepared.append((timestamp, rate))
    if not prepared:
        raise BinanceFundingDownloadError("Nenhum funding encontrado no intervalo")
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["timestamp", "rate"])
        writer.writerows((timestamp, str(rate)) for timestamp, rate in prepared)
    return output


__all__ = [
    "BinanceFundingDownloadError",
    "download_funding_rates",
    "download_funding_to_csv",
    "write_funding_csv",
]
