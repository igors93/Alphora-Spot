import unicodedata
from pathlib import Path

from scanner.signal_score import SignalPriority, default_next_check_seconds


def _priority_rank(priority: SignalPriority) -> int:
    ranks = {
        SignalPriority.COLD: 0,
        SignalPriority.WARM: 1,
        SignalPriority.HOT: 2,
        SignalPriority.READY: 3,
    }
    return ranks[priority]


def _dirty_symbol_can_wake(
    priority: SignalPriority,
    *,
    now: float,
    last_checked_at: float,
    loop_interval_seconds: float,
) -> bool:
    if priority is SignalPriority.COLD:
        return False

    if priority is SignalPriority.READY:
        return True

    baseline = default_next_check_seconds(priority)
    if priority is SignalPriority.WARM:
        min_interval = min(baseline, 5.0)
    else:
        min_interval = min(baseline, 1.0)

    min_interval = max(float(loop_interval_seconds), min_interval)
    return now - last_checked_at >= min_interval


def _is_warmup_score(score) -> bool:
    reason = str(getattr(score, "reason", "")).lower()
    normalized_reason = (
        unicodedata.normalize("NFKD", reason).encode("ascii", "ignore").decode("ascii")
    )
    return (
        "aguardando historico minimo" in normalized_reason
        or "warmup" in normalized_reason
        or "aquec" in normalized_reason
    )


def _journal_signature(paths: list[Path]) -> tuple[tuple[str, int, int], ...]:
    signature = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            signature.append((str(path), 0, 0))
            continue
        signature.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def _json_default(value):
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _extract_available_usdt(account_info: dict) -> str | None:
    return _extract_usdt_balance_field(account_info, "availableBalance")


def _extract_wallet_usdt(account_info: dict) -> str | None:
    return _extract_usdt_balance_field(account_info, "walletBalance")


def _extract_usdt_balance_field(account_info: dict, field: str) -> str | None:
    if account_info.get("runtime_balance_fields_trusted") is False:
        return None

    for asset in account_info.get("assets", []):
        if asset.get("asset") == "USDT":
            value = asset.get(field)
            if value is None or value == "":
                return None
            return str(value)

    for balance in account_info.get("balances", []):
        if balance.get("asset") == "USDT":
            free_raw = balance.get("free")
            locked_raw = balance.get("locked", "0")
            if free_raw is None or free_raw == "":
                return None
            if field == "availableBalance":
                return str(free_raw)
            if field == "walletBalance":
                from decimal import Decimal

                return str(Decimal(str(free_raw)) + Decimal(str(locked_raw or "0")))
    return None


def _enum_value(value) -> str:
    return str(getattr(value, "value", value))
