from dataclasses import dataclass, field
from typing import Any

from governor.user_data_cache import UserDataShadowCache
from governor.user_data_cache_usage import UserDataCacheUsageShadowEvaluation


@dataclass(frozen=True)
class UserDataCacheAssistedPlanShadow:
    symbol: str
    status: str
    reason: str
    would_use_position_cache: bool
    would_use_account_cache: bool
    would_use_order_cache: bool
    fallback_rest_required: bool
    cache_age_seconds: float | None
    last_audit_status: str
    last_audit_age_seconds: float | None
    shadow_only: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "would_use_position_cache": self.would_use_position_cache,
            "would_use_account_cache": self.would_use_account_cache,
            "would_use_order_cache": self.would_use_order_cache,
            "fallback_rest_required": self.fallback_rest_required,
            "cache_age_seconds": self.cache_age_seconds,
            "last_audit_status": self.last_audit_status,
            "last_audit_age_seconds": self.last_audit_age_seconds,
            "shadow_only": self.shadow_only,
            "details": self.details,
        }


def build_user_data_cache_assisted_plan_shadow(
    *,
    cache: UserDataShadowCache | None,
    symbol: str,
    usage_evaluation: UserDataCacheUsageShadowEvaluation | None,
) -> UserDataCacheAssistedPlanShadow:
    normalized_symbol = str(symbol or "").upper().strip()

    if cache is None:
        return _plan(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason="CACHE_NOT_AVAILABLE",
            would_use_position_cache=False,
            would_use_account_cache=False,
            would_use_order_cache=False,
            fallback_rest_required=True,
            cache_age_seconds=None,
            last_audit_status="",
            last_audit_age_seconds=None,
            details={"cache_available": False},
        )

    if usage_evaluation is None:
        return _plan(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason="USAGE_EVALUATION_MISSING",
            would_use_position_cache=False,
            would_use_account_cache=False,
            would_use_order_cache=False,
            fallback_rest_required=True,
            cache_age_seconds=None,
            last_audit_status="",
            last_audit_age_seconds=None,
            details={"usage_evaluation_available": False},
        )

    usage_payload = usage_evaluation.to_dict()
    last_audit = _last_audit_from_usage(usage_evaluation)

    if not usage_evaluation.would_use_cache:
        component_plan = _component_scoped_plan(
            cache=cache,
            normalized_symbol=normalized_symbol,
            usage_evaluation=usage_evaluation,
            usage_payload=usage_payload,
            last_audit=last_audit,
        )
        if component_plan is not None:
            return component_plan

        return _plan(
            symbol=normalized_symbol,
            status="INELIGIBLE",
            reason=usage_evaluation.reason,
            would_use_position_cache=False,
            would_use_account_cache=False,
            would_use_order_cache=False,
            fallback_rest_required=True,
            cache_age_seconds=usage_evaluation.cache_age_seconds,
            last_audit_status=usage_evaluation.last_audit_status,
            last_audit_age_seconds=usage_evaluation.last_audit_age_seconds,
            details={
                "usage_evaluation": usage_payload,
                "last_audit": last_audit,
            },
        )

    position_available = normalized_symbol in cache.positions_by_symbol
    account_available = bool(cache.balances_by_asset)
    account_runtime_balance_fields_trusted = _account_runtime_balance_fields_trusted(cache)

    position_audit_match = bool(last_audit.get("position_match", False))
    account_audit_match = bool(last_audit.get("account_match", False))
    orders_audit_match = bool(last_audit.get("orders_match", False))

    would_use_position_cache = (
        position_available and not usage_evaluation.dirty_position and position_audit_match
    )
    would_use_account_cache = (
        account_available
        and account_runtime_balance_fields_trusted
        and not usage_evaluation.account_dirty
        and account_audit_match
    )
    would_use_order_cache = orders_audit_match

    fallback_rest_required = not all(
        [
            would_use_position_cache,
            would_use_account_cache,
            would_use_order_cache,
        ]
    )

    if fallback_rest_required:
        status = "PARTIAL"
        reason = "CACHE_ASSISTED_PLAN_PARTIAL"
    else:
        status = "READY"
        reason = "CACHE_ASSISTED_PLAN_READY"
    component_fallback_reasons = {
        "position": _component_fallback_reason(
            available=position_available,
            dirty=usage_evaluation.dirty_position,
            audit_match=position_audit_match,
            dirty_reason="CACHE_DIRTY_POSITION",
            missing_reason="POSITION_CACHE_MISSING",
            audit_reason="AUDIT_POSITION_MISMATCH",
        ),
        "account_info": _component_fallback_reason(
            available=account_available,
            dirty=usage_evaluation.account_dirty,
            audit_match=account_audit_match,
            dirty_reason="CACHE_ACCOUNT_DIRTY",
            missing_reason="ACCOUNT_CACHE_MISSING",
            audit_reason="AUDIT_ACCOUNT_MISMATCH",
            trusted=account_runtime_balance_fields_trusted,
            untrusted_reason="ACCOUNT_RUNTIME_BALANCE_FIELDS_UNTRUSTED",
        ),
        "open_orders": _component_fallback_reason(
            available=True,
            dirty=False,
            audit_match=orders_audit_match,
            dirty_reason="",
            missing_reason="",
            audit_reason="AUDIT_ORDERS_MISMATCH",
        ),
    }

    return _plan(
        symbol=normalized_symbol,
        status=status,
        reason=reason,
        would_use_position_cache=would_use_position_cache,
        would_use_account_cache=would_use_account_cache,
        would_use_order_cache=would_use_order_cache,
        fallback_rest_required=fallback_rest_required,
        cache_age_seconds=usage_evaluation.cache_age_seconds,
        last_audit_status=usage_evaluation.last_audit_status,
        last_audit_age_seconds=usage_evaluation.last_audit_age_seconds,
        details={
            "usage_evaluation": usage_payload,
            "last_audit": last_audit,
            "position_available": position_available,
            "account_available": account_available,
            "account_runtime_balance_fields_trusted": (account_runtime_balance_fields_trusted),
            "position_audit_match": position_audit_match,
            "account_audit_match": account_audit_match,
            "orders_audit_match": orders_audit_match,
            "component_fallback_reasons": {
                key: value for key, value in component_fallback_reasons.items() if value
            },
            "safety_note": (
                "Fase 10I-6A: plano shadow de cache-assisted heavy snapshot. "
                "Nao substitui REST, nao muda decisao e nao altera ordens."
            ),
        },
    )


def _component_scoped_plan(
    *,
    cache: UserDataShadowCache,
    normalized_symbol: str,
    usage_evaluation: UserDataCacheUsageShadowEvaluation,
    usage_payload: dict[str, Any],
    last_audit: dict[str, Any],
) -> UserDataCacheAssistedPlanShadow | None:
    reason = str(usage_evaluation.reason or "").upper().strip()
    if reason not in {
        "CACHE_DIRTY_POSITION",
        "CACHE_ACCOUNT_DIRTY",
        "AUDIT_MISMATCH",
    }:
        return None

    position_available = normalized_symbol in cache.positions_by_symbol
    account_available = bool(cache.balances_by_asset)
    account_runtime_balance_fields_trusted = _account_runtime_balance_fields_trusted(cache)
    position_audit_match = bool(last_audit.get("position_match", False))
    account_audit_match = bool(last_audit.get("account_match", False))
    orders_audit_match = bool(last_audit.get("orders_match", False))

    component_fallback_reasons = {
        "position": _component_fallback_reason(
            available=position_available,
            dirty=usage_evaluation.dirty_position,
            audit_match=position_audit_match,
            dirty_reason="CACHE_DIRTY_POSITION",
            missing_reason="POSITION_CACHE_MISSING",
            audit_reason="AUDIT_POSITION_MISMATCH",
        ),
        "account_info": _component_fallback_reason(
            available=account_available,
            dirty=usage_evaluation.account_dirty,
            audit_match=account_audit_match,
            dirty_reason="CACHE_ACCOUNT_DIRTY",
            missing_reason="ACCOUNT_CACHE_MISSING",
            audit_reason="AUDIT_ACCOUNT_MISMATCH",
            trusted=account_runtime_balance_fields_trusted,
            untrusted_reason="ACCOUNT_RUNTIME_BALANCE_FIELDS_UNTRUSTED",
        ),
        "open_orders": _component_fallback_reason(
            available=True,
            dirty=False,
            audit_match=orders_audit_match,
            dirty_reason="",
            missing_reason="",
            audit_reason="AUDIT_ORDERS_MISMATCH",
        ),
    }

    would_use_position_cache = component_fallback_reasons["position"] == ""
    would_use_account_cache = component_fallback_reasons["account_info"] == ""
    would_use_order_cache = component_fallback_reasons["open_orders"] == ""
    any_cache_used = any([would_use_position_cache, would_use_account_cache, would_use_order_cache])
    if not any_cache_used:
        return None

    return _plan(
        symbol=normalized_symbol,
        status="PARTIAL",
        reason="CACHE_ASSISTED_PLAN_PARTIAL",
        would_use_position_cache=would_use_position_cache,
        would_use_account_cache=would_use_account_cache,
        would_use_order_cache=would_use_order_cache,
        fallback_rest_required=True,
        cache_age_seconds=usage_evaluation.cache_age_seconds,
        last_audit_status=usage_evaluation.last_audit_status,
        last_audit_age_seconds=usage_evaluation.last_audit_age_seconds,
        details={
            "usage_evaluation": usage_payload,
            "last_audit": last_audit,
            "component_fallback_reasons": {
                key: value for key, value in component_fallback_reasons.items() if value
            },
            "position_available": position_available,
            "account_available": account_available,
            "account_runtime_balance_fields_trusted": (account_runtime_balance_fields_trusted),
            "position_audit_match": position_audit_match,
            "account_audit_match": account_audit_match,
            "orders_audit_match": orders_audit_match,
            "safety_note": (
                "Plano parcial: um componente sujo/mismatch cai para REST sem "
                "invalidar componentes ainda auditados."
            ),
        },
    )


def _component_fallback_reason(
    *,
    available: bool,
    dirty: bool,
    audit_match: bool,
    dirty_reason: str,
    missing_reason: str,
    audit_reason: str,
    trusted: bool = True,
    untrusted_reason: str = "",
) -> str:
    if not available:
        return missing_reason

    if dirty:
        return dirty_reason

    if not trusted:
        return untrusted_reason

    if not audit_match:
        return audit_reason

    return ""


def _account_runtime_balance_fields_trusted(cache: UserDataShadowCache) -> bool:
    account_info = cache.account_info_snapshot()
    if not isinstance(account_info, dict):
        return False
    return account_info.get("runtime_balance_fields_trusted") is True


def _plan(
    *,
    symbol: str,
    status: str,
    reason: str,
    would_use_position_cache: bool,
    would_use_account_cache: bool,
    would_use_order_cache: bool,
    fallback_rest_required: bool,
    cache_age_seconds: float | None,
    last_audit_status: str,
    last_audit_age_seconds: float | None,
    details: dict[str, Any],
) -> UserDataCacheAssistedPlanShadow:
    return UserDataCacheAssistedPlanShadow(
        symbol=symbol,
        status=status,
        reason=reason,
        would_use_position_cache=would_use_position_cache,
        would_use_account_cache=would_use_account_cache,
        would_use_order_cache=would_use_order_cache,
        fallback_rest_required=fallback_rest_required,
        cache_age_seconds=cache_age_seconds,
        last_audit_status=last_audit_status,
        last_audit_age_seconds=last_audit_age_seconds,
        details=details,
    )


def _last_audit_from_usage(
    usage_evaluation: UserDataCacheUsageShadowEvaluation,
) -> dict[str, Any]:
    details = usage_evaluation.details
    if not isinstance(details, dict):
        return {}

    last_audit = details.get("last_audit", {})
    if not isinstance(last_audit, dict):
        return {}

    return last_audit
