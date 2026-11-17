from dataclasses import dataclass, field
from typing import Any

from governor.user_data_cache import UserDataShadowCache


@dataclass(frozen=True)
class UserDataCacheStaleGuardEvaluation:
    symbol: str
    status: str
    reason: str
    cache_allowed: bool
    fallback_required: bool
    cache_age_seconds: float | None
    stream_enabled: bool
    stream_running: bool
    stream_connected: bool
    listen_key_active: bool
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
            "cache_allowed": self.cache_allowed,
            "fallback_required": self.fallback_required,
            "cache_age_seconds": self.cache_age_seconds,
            "stream_enabled": self.stream_enabled,
            "stream_running": self.stream_running,
            "stream_connected": self.stream_connected,
            "listen_key_active": self.listen_key_active,
            "stream_last_message_age_seconds": self.stream_last_message_age_seconds,
            "last_audit_status": self.last_audit_status,
            "last_audit_age_seconds": self.last_audit_age_seconds,
            "dirty_position": self.dirty_position,
            "account_dirty": self.account_dirty,
            "shadow_only": self.shadow_only,
            "details": self.details,
        }


def evaluate_user_data_cache_stale_guard(
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
) -> UserDataCacheStaleGuardEvaluation:
    normalized_symbol = str(symbol or "").upper().strip()
    stream_health = stream_health or {"enabled": False}
    last_audit = last_audit or {}

    cache_summary = cache.summary() if cache is not None else {}
    cache_age_seconds = _age_seconds(
        now=now,
        timestamp=cache_summary.get("last_event_at"),
    )

    stream_enabled = bool(stream_health.get("enabled", False))
    stream_running = bool(stream_health.get("running", False))
    stream_connected = bool(stream_health.get("connected", False))
    listen_key_active = _listen_key_active(stream_health, stream_connected)
    stream_last_message_age_seconds = _optional_float(stream_health.get("last_message_age_seconds"))

    audit_status = str(last_audit.get("status", "") or "").upper().strip()
    last_audit_age_seconds = _age_seconds(
        now=now,
        timestamp=last_audit.get("recorded_at"),
    )

    dirty_position = cache is not None and normalized_symbol in cache.dirty_position_symbols
    account_dirty = bool(cache.account_dirty) if cache is not None else False

    common_details = {
        "cache_available": cache is not None,
        "cache_summary": cache_summary,
        "stream_health": stream_health,
        "last_audit": last_audit,
        "max_cache_age_seconds": max_cache_age_seconds,
        "max_stream_message_age_seconds": max_stream_message_age_seconds,
        "max_audit_age_seconds": max_audit_age_seconds,
        "require_audit_match": require_audit_match,
        "require_active_listen_key": require_active_listen_key,
        "safety_note": (
            "Fase 10I-7A: stale guard do cache privado. "
            "Quando o guard bloqueia, o sistema deve usar fallback REST."
        ),
    }

    if cache is None:
        return _blocked(
            symbol=normalized_symbol,
            status="CACHE_NOT_AVAILABLE",
            reason="CACHE_NOT_AVAILABLE",
            cache_age_seconds=None,
            stream_enabled=stream_enabled,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=listen_key_active,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=False,
            account_dirty=False,
            details=common_details,
        )

    if not stream_enabled:
        return _blocked(
            symbol=normalized_symbol,
            status="DISABLED",
            reason="USER_DATA_STREAM_DISABLED",
            cache_age_seconds=cache_age_seconds,
            stream_enabled=False,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=listen_key_active,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if not stream_running or not stream_connected:
        return _blocked(
            symbol=normalized_symbol,
            status="DISCONNECTED",
            reason="USER_DATA_STREAM_NOT_CONNECTED",
            cache_age_seconds=cache_age_seconds,
            stream_enabled=stream_enabled,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=listen_key_active,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if require_active_listen_key and not listen_key_active:
        return _blocked(
            symbol=normalized_symbol,
            status="LISTEN_KEY_INACTIVE",
            reason="USER_DATA_STREAM_LISTEN_KEY_INACTIVE",
            cache_age_seconds=cache_age_seconds,
            stream_enabled=stream_enabled,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=False,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if stream_last_message_age_seconds is None:
        return _blocked(
            symbol=normalized_symbol,
            status="NO_MESSAGES",
            reason="USER_DATA_STREAM_NO_MESSAGES",
            cache_age_seconds=cache_age_seconds,
            stream_enabled=stream_enabled,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=listen_key_active,
            stream_last_message_age_seconds=None,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if (
        max_stream_message_age_seconds > 0
        and stream_last_message_age_seconds > max_stream_message_age_seconds
    ):
        return _blocked(
            symbol=normalized_symbol,
            status="STREAM_STALE",
            reason="USER_DATA_STREAM_STALE",
            cache_age_seconds=cache_age_seconds,
            stream_enabled=stream_enabled,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=listen_key_active,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if cache_age_seconds is None:
        return _blocked(
            symbol=normalized_symbol,
            status="CACHE_EMPTY",
            reason="CACHE_EMPTY",
            cache_age_seconds=None,
            stream_enabled=stream_enabled,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=listen_key_active,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if max_cache_age_seconds > 0 and cache_age_seconds > max_cache_age_seconds:
        return _blocked(
            symbol=normalized_symbol,
            status="CACHE_STALE",
            reason="CACHE_STALE",
            cache_age_seconds=cache_age_seconds,
            stream_enabled=stream_enabled,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=listen_key_active,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=account_dirty,
            details=common_details,
        )

    if dirty_position:
        return _blocked(
            symbol=normalized_symbol,
            status="DIRTY_POSITION",
            reason="CACHE_DIRTY_POSITION",
            cache_age_seconds=cache_age_seconds,
            stream_enabled=stream_enabled,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=listen_key_active,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=True,
            account_dirty=account_dirty,
            details=common_details,
        )

    if account_dirty:
        return _blocked(
            symbol=normalized_symbol,
            status="ACCOUNT_DIRTY",
            reason="CACHE_ACCOUNT_DIRTY",
            cache_age_seconds=cache_age_seconds,
            stream_enabled=stream_enabled,
            stream_running=stream_running,
            stream_connected=stream_connected,
            listen_key_active=listen_key_active,
            stream_last_message_age_seconds=stream_last_message_age_seconds,
            last_audit_status=audit_status,
            last_audit_age_seconds=last_audit_age_seconds,
            dirty_position=dirty_position,
            account_dirty=True,
            details=common_details,
        )

    if require_audit_match:
        if not audit_status:
            return _blocked(
                symbol=normalized_symbol,
                status="AUDIT_MISSING",
                reason="AUDIT_MISSING",
                cache_age_seconds=cache_age_seconds,
                stream_enabled=stream_enabled,
                stream_running=stream_running,
                stream_connected=stream_connected,
                listen_key_active=listen_key_active,
                stream_last_message_age_seconds=stream_last_message_age_seconds,
                last_audit_status=audit_status,
                last_audit_age_seconds=last_audit_age_seconds,
                dirty_position=dirty_position,
                account_dirty=account_dirty,
                details=common_details,
            )

        if audit_status != "MATCH":
            return _blocked(
                symbol=normalized_symbol,
                status="AUDIT_MISMATCH",
                reason=f"AUDIT_{audit_status}",
                cache_age_seconds=cache_age_seconds,
                stream_enabled=stream_enabled,
                stream_running=stream_running,
                stream_connected=stream_connected,
                listen_key_active=listen_key_active,
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
            return _blocked(
                symbol=normalized_symbol,
                status="AUDIT_STALE",
                reason="AUDIT_STALE",
                cache_age_seconds=cache_age_seconds,
                stream_enabled=stream_enabled,
                stream_running=stream_running,
                stream_connected=stream_connected,
                listen_key_active=listen_key_active,
                stream_last_message_age_seconds=stream_last_message_age_seconds,
                last_audit_status=audit_status,
                last_audit_age_seconds=last_audit_age_seconds,
                dirty_position=dirty_position,
                account_dirty=account_dirty,
                details=common_details,
            )

    return UserDataCacheStaleGuardEvaluation(
        symbol=normalized_symbol,
        status="HEALTHY",
        reason="CACHE_GUARD_HEALTHY",
        cache_allowed=True,
        fallback_required=False,
        cache_age_seconds=cache_age_seconds,
        stream_enabled=stream_enabled,
        stream_running=stream_running,
        stream_connected=stream_connected,
        listen_key_active=listen_key_active,
        stream_last_message_age_seconds=stream_last_message_age_seconds,
        last_audit_status=audit_status,
        last_audit_age_seconds=last_audit_age_seconds,
        dirty_position=False,
        account_dirty=False,
        details=common_details,
    )


def _blocked(
    *,
    symbol: str,
    status: str,
    reason: str,
    cache_age_seconds: float | None,
    stream_enabled: bool,
    stream_running: bool,
    stream_connected: bool,
    listen_key_active: bool,
    stream_last_message_age_seconds: float | None,
    last_audit_status: str,
    last_audit_age_seconds: float | None,
    dirty_position: bool,
    account_dirty: bool,
    details: dict[str, Any],
) -> UserDataCacheStaleGuardEvaluation:
    return UserDataCacheStaleGuardEvaluation(
        symbol=symbol,
        status=status,
        reason=reason,
        cache_allowed=False,
        fallback_required=True,
        cache_age_seconds=cache_age_seconds,
        stream_enabled=stream_enabled,
        stream_running=stream_running,
        stream_connected=stream_connected,
        listen_key_active=listen_key_active,
        stream_last_message_age_seconds=stream_last_message_age_seconds,
        last_audit_status=last_audit_status,
        last_audit_age_seconds=last_audit_age_seconds,
        dirty_position=dirty_position,
        account_dirty=account_dirty,
        details=details,
    )


def _listen_key_active(
    stream_health: dict[str, Any],
    stream_connected: bool,
) -> bool:
    if "listen_key_active" not in stream_health:
        return stream_connected

    return bool(stream_health.get("listen_key_active", False))


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
