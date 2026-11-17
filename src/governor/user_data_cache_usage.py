from dataclasses import dataclass, field
from typing import Any

from governor.user_data_cache import UserDataShadowCache
from governor.user_data_cache_stale_guard import evaluate_user_data_cache_stale_guard


@dataclass(frozen=True)
class UserDataCacheUsageShadowEvaluation:
    symbol: str
    status: str
    reason: str
    would_use_cache: bool
    fallback_required: bool
    cache_age_seconds: float | None
    stream_connected: bool
    stream_last_message_age_seconds: float | None
    last_audit_status: str
    last_audit_age_seconds: float | None
    dirty_position: bool
    account_dirty: bool
    shadow_only: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "would_use_cache": self.would_use_cache,
            "fallback_required": self.fallback_required,
            "cache_age_seconds": self.cache_age_seconds,
            "stream_connected": self.stream_connected,
            "stream_last_message_age_seconds": self.stream_last_message_age_seconds,
            "last_audit_status": self.last_audit_status,
            "last_audit_age_seconds": self.last_audit_age_seconds,
            "dirty_position": self.dirty_position,
            "account_dirty": self.account_dirty,
            "shadow_only": self.shadow_only,
            "details": self.details,
        }


def evaluate_user_data_cache_usage_shadow(
    *,
    cache: UserDataShadowCache | None,
    symbol: str,
    now: float,
    stream_health: dict[str, Any] | None,
    last_audit: dict[str, Any] | None,
    max_cache_age_seconds: float = 10.0,
    max_stream_message_age_seconds: float = 30.0,
    max_audit_age_seconds: float = 60.0,
    require_audit_match: bool = True,
    require_active_listen_key: bool = True,
) -> UserDataCacheUsageShadowEvaluation:
    normalized_symbol = str(symbol or "").upper().strip()
    stream_health = stream_health or {"enabled": False}
    last_audit = last_audit or {}

    if cache is None:
        return _evaluation(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason="CACHE_NOT_AVAILABLE",
            would_use_cache=False,
            fallback_required=True,
            cache_age_seconds=None,
            stream_connected=False,
            stream_last_message_age_seconds=None,
            last_audit_status="",
            last_audit_age_seconds=None,
            dirty_position=False,
            account_dirty=False,
            details={"cache_available": False},
        )

    cache_summary = cache.summary()
    last_event_at = cache_summary.get("last_event_at")
    cache_age_seconds = _age_seconds(now=now, timestamp=last_event_at)

    stream_enabled = bool(stream_health.get("enabled", False))
    stream_connected = bool(stream_health.get("connected", False))
    stream_last_message_age_seconds = _optional_float(stream_health.get("last_message_age_seconds"))

    audit_status = str(last_audit.get("status", "") or "").upper().strip()
    last_audit_age_seconds = _age_seconds(
        now=now,
        timestamp=last_audit.get("recorded_at"),
    )

    dirty_position = normalized_symbol in cache.dirty_position_symbols
    account_dirty = bool(cache.account_dirty)

    common_details = {
        "cache_summary": cache_summary,
        "stream_health": stream_health,
        "last_audit": last_audit,
        "max_cache_age_seconds": max_cache_age_seconds,
        "max_stream_message_age_seconds": max_stream_message_age_seconds,
        "max_audit_age_seconds": max_audit_age_seconds,
        "require_audit_match": require_audit_match,
        "require_active_listen_key": require_active_listen_key,
        "safety_note": (
            "Fase 10I-5: avaliacao shadow de elegibilidade do cache privado. "
            "Nao substitui REST, nao muda decisao e nao altera ordens."
        ),
    }
    stale_guard = evaluate_user_data_cache_stale_guard(
        cache=cache,
        symbol=normalized_symbol,
        now=now,
        stream_health=stream_health,
        last_audit=last_audit,
        max_cache_age_seconds=max_cache_age_seconds,
        max_stream_message_age_seconds=max_stream_message_age_seconds,
        max_audit_age_seconds=max_audit_age_seconds,
        require_audit_match=require_audit_match,
        require_active_listen_key=require_active_listen_key,
    )
    common_details["stale_guard"] = stale_guard.to_dict()

    if not stale_guard.cache_allowed:
        return _evaluation(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason=stale_guard.reason,
            would_use_cache=False,
            fallback_required=True,
            cache_age_seconds=cache_age_seconds,
            stream_connected=stream_connected,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if not stream_enabled:
        return _evaluation(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason="USER_DATA_STREAM_DISABLED",
            would_use_cache=False,
            fallback_required=True,
            cache_age_seconds=cache_age_seconds,
            stream_connected=stream_connected,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if not stream_connected:
        return _evaluation(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason="USER_DATA_STREAM_NOT_CONNECTED",
            would_use_cache=False,
            fallback_required=True,
            cache_age_seconds=cache_age_seconds,
            stream_connected=False,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if cache_age_seconds is None:
        return _evaluation(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason="CACHE_EMPTY",
            would_use_cache=False,
            fallback_required=True,
            cache_age_seconds=None,
            stream_connected=stream_connected,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if max_cache_age_seconds > 0 and cache_age_seconds > max_cache_age_seconds:
        return _evaluation(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason="CACHE_STALE",
            would_use_cache=False,
            fallback_required=True,
            cache_age_seconds=cache_age_seconds,
            stream_connected=stream_connected,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if dirty_position:
        return _evaluation(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason="CACHE_DIRTY_POSITION",
            would_use_cache=False,
            fallback_required=True,
            cache_age_seconds=cache_age_seconds,
            stream_connected=stream_connected,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=True,
            account_dirty=account_dirty,
            details=common_details,
        )

    if account_dirty:
        return _evaluation(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason="CACHE_ACCOUNT_DIRTY",
            would_use_cache=False,
            fallback_required=True,
            cache_age_seconds=cache_age_seconds,
            stream_connected=stream_connected,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=True,
            details=common_details,
        )

    if require_audit_match:
        if not audit_status:
            return _evaluation(
                symbol=normalized_symbol,
                status="INELIGIBLE",
                reason="AUDIT_MISSING",
                would_use_cache=False,
                fallback_required=True,
                cache_age_seconds=cache_age_seconds,
                stream_connected=stream_connected,
                stream_last_message_age_seconds=stream_last_message_age_seconds,
                last_audit_status=audit_status,
                last_audit_age_seconds=last_audit_age_seconds,
                dirty_position=dirty_position,
                account_dirty=account_dirty,
                details=common_details,
            )

        if audit_status != "MATCH":
            return _evaluation(
                symbol=normalized_symbol,
                status="INELIGIBLE",
                reason=f"AUDIT_{audit_status}",
                would_use_cache=False,
                fallback_required=True,
                cache_age_seconds=cache_age_seconds,
                stream_connected=stream_connected,
                stream_last_message_age_seconds=stream_last_message_age_seconds,
                last_audit_status=audit_status,
                last_audit_age_seconds=last_audit_age_seconds,
                dirty_position=dirty_position,
                account_dirty=account_dirty,
                details=common_details,
            )

        if last_audit_age_seconds is None or (
            max_audit_age_seconds > 0 and last_audit_age_seconds > max_audit_age_seconds
        ):
            return _evaluation(
                symbol=normalized_symbol,
                status="INELIGIBLE",
                reason="AUDIT_STALE",
                would_use_cache=False,
                fallback_required=True,
                cache_age_seconds=cache_age_seconds,
                stream_connected=stream_connected,
                stream_last_message_age_seconds=stream_last_message_age_seconds,
                last_audit_status=audit_status,
                last_audit_age_seconds=last_audit_age_seconds,
                dirty_position=dirty_position,
                account_dirty=account_dirty,
                details=common_details,
            )

    return _evaluation(
        symbol=normalized_symbol,
        status="ELIGIBLE",
        reason="CACHE_ELIGIBLE",
        would_use_cache=True,
        fallback_required=False,
        cache_age_seconds=cache_age_seconds,
        stream_connected=stream_connected,
        stream_last_message_age_seconds=stream_last_message_age_seconds,
        last_audit_status=audit_status,
        last_audit_age_seconds=last_audit_age_seconds,
        dirty_position=dirty_position,
        account_dirty=account_dirty,
        details=common_details,
    )


def _evaluation(
    *,
    symbol: str,
    status: str,
    reason: str,
    would_use_cache: bool,
    fallback_required: bool,
    cache_age_seconds: float | None,
    stream_connected: bool,
    stream_last_message_age_seconds: float | None,
    last_audit_status: str,
    last_audit_age_seconds: float | None,
    dirty_position: bool,
    account_dirty: bool,
    details: dict[str, Any],
) -> UserDataCacheUsageShadowEvaluation:
    return UserDataCacheUsageShadowEvaluation(
        symbol=symbol,
        status=status,
        reason=reason,
        would_use_cache=would_use_cache,
        fallback_required=fallback_required,
        cache_age_seconds=cache_age_seconds,
        stream_connected=stream_connected,
        stream_last_message_age_seconds=stream_last_message_age_seconds,
        last_audit_status=last_audit_status,
        last_audit_age_seconds=last_audit_age_seconds,
        dirty_position=dirty_position,
        account_dirty=account_dirty,
        details=details,
    )


def _age_seconds(*, now: float, timestamp: Any) -> float | None:
    if timestamp is None:
        return None

    try:
        return max(0.0, float(now) - float(timestamp))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None
