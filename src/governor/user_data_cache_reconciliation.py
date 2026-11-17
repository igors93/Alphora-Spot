import time
from dataclasses import dataclass, field
from typing import Any

from governor.user_data_cache import UserDataShadowCache


@dataclass(frozen=True)
class UserDataCacheReconciliationResult:
    symbol: str
    status: str
    reason: str
    position_reconciled: bool
    account_reconciled: bool
    open_orders_reconciled: bool
    dirty_position_cleared: bool
    account_dirty_cleared: bool
    rest_backed_position: bool
    rest_backed_account: bool
    rest_backed_open_orders: bool
    position_source: str
    account_source: str
    open_orders_source: str
    final_position_source: str
    open_orders_count: int
    cache_summary: dict[str, Any]
    shadow_only: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "position_reconciled": self.position_reconciled,
            "account_reconciled": self.account_reconciled,
            "open_orders_reconciled": self.open_orders_reconciled,
            "dirty_position_cleared": self.dirty_position_cleared,
            "account_dirty_cleared": self.account_dirty_cleared,
            "rest_backed_position": self.rest_backed_position,
            "rest_backed_account": self.rest_backed_account,
            "rest_backed_open_orders": self.rest_backed_open_orders,
            "position_source": self.position_source,
            "account_source": self.account_source,
            "open_orders_source": self.open_orders_source,
            "final_position_source": self.final_position_source,
            "open_orders_count": self.open_orders_count,
            "cache_summary": self.cache_summary,
            "shadow_only": self.shadow_only,
            "details": self.details,
        }


def reconcile_user_data_cache_from_rest(
    *,
    cache: UserDataShadowCache | None,
    symbol: str,
    heavy_snapshot: Any,
    now: float,
    rest_backed_position: bool,
    rest_backed_account: bool,
    rest_backed_open_orders: bool,
    position_source: str | None = None,
    account_source: str | None = None,
    open_orders_source: str | None = None,
    final_position_source: str | None = None,
) -> UserDataCacheReconciliationResult:
    normalized_symbol = str(symbol or "").upper().strip()

    position_source = _component_source(
        position_source,
        rest_backed=rest_backed_position,
    )
    account_source = _component_source(
        account_source,
        rest_backed=rest_backed_account,
    )
    open_orders_source = _component_source(
        open_orders_source,
        rest_backed=rest_backed_open_orders,
    )
    final_position_source = str(final_position_source or "").upper().strip()

    if cache is None:
        return UserDataCacheReconciliationResult(
            symbol=normalized_symbol,
            status="SKIPPED",
            reason="CACHE_NOT_AVAILABLE",
            position_reconciled=False,
            account_reconciled=False,
            open_orders_reconciled=False,
            dirty_position_cleared=False,
            account_dirty_cleared=False,
            rest_backed_position=rest_backed_position,
            rest_backed_account=rest_backed_account,
            rest_backed_open_orders=rest_backed_open_orders,
            position_source=position_source,
            account_source=account_source,
            open_orders_source=open_orders_source,
            final_position_source=final_position_source,
            open_orders_count=0,
            cache_summary={},
            details={"cache_available": False},
        )

    dirty_before = normalized_symbol in cache.dirty_position_symbols
    account_dirty_before = bool(cache.account_dirty)

    position_reconciled = False
    account_reconciled = False
    open_orders_reconciled = False

    if rest_backed_position and position_source == "REST":
        position = getattr(heavy_snapshot, "position", None)
        if position is not None:
            position_reconciled = cache.reconcile_position_from_rest(
                symbol=normalized_symbol,
                position=position,
                received_at=time.time(),
            )

    if rest_backed_account and account_source == "REST":
        account_info = getattr(heavy_snapshot, "account_info", {}) or {}
        if isinstance(account_info, dict):
            account_reconciled = cache.reconcile_account_from_rest(
                account_info=account_info,
                received_at=time.time(),
            )

    if rest_backed_open_orders and open_orders_source == "REST":
        open_orders = getattr(heavy_snapshot, "open_orders", [])
        if isinstance(open_orders, list):
            open_orders_reconciled = cache.reconcile_open_orders_from_rest(
                symbol=normalized_symbol,
                open_orders=open_orders,
                received_at=time.time(),
            )

    dirty_after = normalized_symbol in cache.dirty_position_symbols
    account_dirty_after = bool(cache.account_dirty)

    dirty_position_cleared = dirty_before and not dirty_after
    account_dirty_cleared = account_dirty_before and not account_dirty_after

    if any([position_reconciled, account_reconciled, open_orders_reconciled]):
        status = "RECONCILED"
        reason = "REST_RECONCILIATION_APPLIED"
    elif any([rest_backed_position, rest_backed_account, rest_backed_open_orders]):
        status = "SKIPPED"
        reason = "NO_SAFE_REST_SOURCE"
    else:
        status = "SKIPPED"
        reason = "NO_REST_BACKED_COMPONENTS"

    return UserDataCacheReconciliationResult(
        symbol=normalized_symbol,
        status=status,
        reason=reason,
        position_reconciled=position_reconciled,
        account_reconciled=account_reconciled,
        open_orders_reconciled=open_orders_reconciled,
        dirty_position_cleared=dirty_position_cleared,
        account_dirty_cleared=account_dirty_cleared,
        rest_backed_position=rest_backed_position,
        rest_backed_account=rest_backed_account,
        rest_backed_open_orders=rest_backed_open_orders,
        position_source=position_source,
        account_source=account_source,
        open_orders_source=open_orders_source,
        final_position_source=final_position_source,
        open_orders_count=len(getattr(heavy_snapshot, "open_orders", []) or []),
        cache_summary=cache.summary(),
        details={
            "source_guard": {
                "position_source": position_source,
                "account_source": account_source,
                "open_orders_source": open_orders_source,
                "final_position_source": final_position_source,
            },
            "safety_note": (
                "Fase 10I-7B: reconciliacao REST atualiza cache privado para "
                "limpar dirty/mismatch. Nao altera decisao, ordens ou execucao."
            ),
        },
    )


def _component_source(source: str | None, *, rest_backed: bool) -> str:
    normalized = str(source or "").upper().strip()
    if normalized:
        return normalized

    return "REST" if rest_backed else "USER_DATA_CACHE"
