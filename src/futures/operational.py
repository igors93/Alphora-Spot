import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class OperationalState(Enum):
    ENTRY_PENDING = "ENTRY_PENDING"
    POSITION_OPEN_UNPROTECTED = "POSITION_OPEN_UNPROTECTED"
    POSITION_OPEN_PROTECTED = "POSITION_OPEN_PROTECTED"
    PROTECTIONS_PENDING = "PROTECTIONS_PENDING"
    CLOSE_PENDING = "CLOSE_PENDING"
    WAITING_FLAT_CONFIRMATION = "WAITING_FLAT_CONFIRMATION"
    FLAT_CONFIRMED = "FLAT_CONFIRMED"
    STOPPING_SAFE = "STOPPING_SAFE"
    STOPPED_WITH_PROTECTION = "STOPPED_WITH_PROTECTION"
    STOPPED_FLAT = "STOPPED_FLAT"
    STOP_BLOCKED_UNPROTECTED = "STOP_BLOCKED_UNPROTECTED"
    MANUAL_RECONCILE_REQUIRED = "MANUAL_RECONCILE_REQUIRED"


class StopMode(Enum):
    STOP_KEEP_PROTECTIONS = "STOP_KEEP_PROTECTIONS"
    SAFE_STOP = "SAFE_STOP"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


MANUAL_RECONCILE_REASONS = {
    "entry_order_status_unknown",
    "close_order_status_unknown",
    "algo_order_status_unknown",
    "position_close_not_confirmed",
    "position_close_direction_changed",
}


@dataclass(frozen=True)
class ManualReconcilePayload:
    symbol: str
    reason: str
    timestamp: float
    client_order_id: str | None = None
    client_algo_id: str | None = None
    trade_id: str | None = None
    last_known_position: dict[str, Any] | None = None
    instruction: str = (
        "Verifique manualmente posicao, ordens normais e algo orders na Binance. "
        "Depois execute ops.resolve_manual_reconcile com --confirm RESOLVED_MANUALLY."
    )
    context: dict[str, Any] = field(default_factory=dict)


def manual_reconcile_path(runtime_dir: str | Path, symbol: str) -> Path:
    normalized = symbol.lower().strip()
    return Path(runtime_dir) / f"manual_reconcile_required_{normalized}.json"


def manual_reconcile_required(runtime_dir: str | Path, symbol: str) -> bool:
    return manual_reconcile_path(runtime_dir, symbol).exists()


def write_manual_reconcile_required(
    *,
    runtime_dir: str | Path,
    symbol: str,
    reason: str,
    context: dict[str, Any],
    last_known_position: dict[str, Any] | None,
) -> Path:
    payload = ManualReconcilePayload(
        symbol=symbol.upper().strip(),
        reason=reason,
        timestamp=time.time(),
        client_order_id=_first_context_value(
            context,
            "client_order_id",
            "entry_client_order_id",
            "close_client_order_id",
        ),
        client_algo_id=_first_context_value(context, "client_algo_id"),
        trade_id=_first_context_value(context, "trade_id"),
        last_known_position=last_known_position,
        context=context,
    )
    path = manual_reconcile_path(runtime_dir, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(asdict(payload), file, ensure_ascii=False, indent=2, default=str)
        file.write("\n")
    return path


def clear_manual_reconcile_required(runtime_dir: str | Path, symbol: str) -> bool:
    path = manual_reconcile_path(runtime_dir, symbol)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _first_context_value(context: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = context.get(key)
        if value:
            return str(value)
    return None
