from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backtesting.domain.realism import FundingRatePoint


def load_funding_rates_from_csv(path: str | Path) -> tuple[FundingRatePoint, ...]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV de funding nao encontrado: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fields = set(reader.fieldnames or ())
        if not {"timestamp", "rate"}.issubset(fields):
            raise ValueError("CSV de funding exige colunas timestamp e rate")
        points = []
        for line, row in enumerate(reader, start=2):
            timestamp = _normalized_timestamp(str(row.get("timestamp", "")), line=line)
            try:
                rate = Decimal(str(row.get("rate", "")).strip())
            except InvalidOperation as error:
                raise ValueError(f"Funding invalido na linha {line}") from error
            points.append(FundingRatePoint(timestamp=timestamp, rate=rate))
    if not points:
        raise ValueError("CSV de funding nao contem registros")
    points.sort(key=lambda point: point.timestamp)
    timestamps = [point.timestamp for point in points]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("CSV de funding possui timestamps duplicados")
    return tuple(points)


def _normalized_timestamp(value: str, *, line: int) -> str:
    if not value.strip():
        raise ValueError(f"Funding sem timestamp na linha {line}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Timestamp de funding invalido na linha {line}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()
