import json
import logging
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from utils.events import EventChannel, EventRecord, normalize_severity, observe_record
from utils.jsonl_writer import shared_jsonl_writer
from utils.privacy import sanitize_for_audit, to_safe_fallback

JOURNAL_COMPONENT = "utils.trade_journal"


class TradeJournal:
    """Append-only JSONL journal of one file in ``data/``.

    Every line is also published on the event API (``AUDIT`` channel, ADR-006), so the run
    audit trail sees journal events without the journal knowing the audit logger.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, **data: Any) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "data": data,
        }

        safe_event = sanitize_for_audit(_make_jsonable(event))
        shared_jsonl_writer().append_line(self.path, json.dumps(safe_event, ensure_ascii=False))
        try:
            observe_record(journal_event_record(self.path, event_type, data))
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Falha ao publicar evento do journal | event_type=%s | error=%s",
                event_type,
                error,
            )


def journal_event_record(
    journal_path: str | Path, event_type: str, data: Mapping[str, Any]
) -> EventRecord:
    """The audit record of a journal line: category and severity inferred from the line."""

    payload = dict(data)
    severity = journal_event_severity(event_type, payload)
    payload.pop("severity", None)
    return EventRecord(
        event_type=str(event_type),
        component=JOURNAL_COMPONENT,
        category=journal_event_category(journal_path, event_type),
        channel=EventChannel.AUDIT,
        severity=severity,
        payload={"legacy_journal_path": str(journal_path), **payload},
    )


def journal_event_category(journal_path: str | Path, event_type: str) -> str:
    path_text = str(journal_path).lower()
    event = str(event_type or "").lower()
    if "scanner" in path_text or event.startswith("strategy") or event == "scanner_score":
        return "strategy"
    if "trade_audit" in path_text or "paper_trades" in path_text or "trades" in path_text:
        return "execution"
    if "backtest" in path_text or event.startswith("backtest"):
        return "backtesting"
    if "market" in event or "stale" in event or "websocket" in event:
        return "market_data"
    if "risk" in event or "daily" in event:
        return "risk"
    if "exchange" in event or "binance" in event or "notional" in event:
        return "exchange"
    if "governor" in path_text or "governor" in event or "symbol_" in event:
        return "governor"
    return "system"


def journal_event_severity(event_type: str, payload: Mapping[str, Any]) -> str:
    event = str(event_type or "").lower()
    explicit = payload.get("severity")
    if explicit:
        return normalize_severity(str(explicit))
    if any(token in event for token in ("failed", "error", "paused", "exception", "invalid")):
        return "ERROR"
    if any(token in event for token in ("warning", "stale", "blocked", "skipped", "missing")):
        return "WARNING"
    return "INFO"


def _make_jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_json_key(key): _make_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_make_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_make_jsonable(item) for item in value), key=str)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _make_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    return to_safe_fallback(value)


def _json_key(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    return str(to_safe_fallback(value))
