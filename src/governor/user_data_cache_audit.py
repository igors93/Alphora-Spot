from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from governor.user_data_cache import UserDataShadowCache


@dataclass(frozen=True)
class UserDataCacheAuditResult:
    symbol: str
    status: str
    reason: str
    cache_age_seconds: float | None
    position_status: str
    orders_status: str
    account_status: str
    position_match: bool
    orders_match: bool
    account_match: bool
    rest_position_amount: str
    cache_position_amount: str
    rest_open_order_refs: list[str]
    cache_open_order_refs: list[str]
    account_dirty: bool
    dirty_position: bool
    shadow_only: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "cache_age_seconds": self.cache_age_seconds,
            "position_status": self.position_status,
            "orders_status": self.orders_status,
            "account_status": self.account_status,
            "position_match": self.position_match,
            "orders_match": self.orders_match,
            "account_match": self.account_match,
            "rest_position_amount": self.rest_position_amount,
            "cache_position_amount": self.cache_position_amount,
            "rest_open_order_refs": self.rest_open_order_refs,
            "cache_open_order_refs": self.cache_open_order_refs,
            "account_dirty": self.account_dirty,
            "dirty_position": self.dirty_position,
            "shadow_only": self.shadow_only,
            "details": self.details,
        }


def audit_user_data_shadow_cache(
    *,
    cache: UserDataShadowCache,
    symbol: str,
    heavy_snapshot: Any,
    now: float,
    max_cache_age_seconds: float = 30.0,
) -> UserDataCacheAuditResult:
    normalized_symbol = str(symbol or "").upper().strip()
    cache_summary = cache.summary()
    last_event_at = cache_summary.get("last_event_at")

    cache_age_seconds = None
    if last_event_at is not None:
        cache_age_seconds = max(0.0, float(now) - float(last_event_at))

    stale = cache_age_seconds is None or (
        max_cache_age_seconds > 0 and cache_age_seconds > max_cache_age_seconds
    )

    position_status, position_reason, rest_position_amount, cache_position_amount = _audit_position(
        cache, normalized_symbol, heavy_snapshot
    )
    orders_status, orders_reason, rest_open_refs, cache_open_refs = _audit_orders(
        cache,
        normalized_symbol,
        heavy_snapshot,
    )
    account_status, account_reason = _audit_account(cache, heavy_snapshot)

    component_statuses = [position_status, orders_status, account_status]

    if stale:
        status = "STALE"
        reason = "CACHE_STALE_OR_EMPTY"
    elif "MISMATCH" in component_statuses:
        status = "MISMATCH"
        reason = _first_reason_for_status(
            [
                (position_status, position_reason),
                (orders_status, orders_reason),
                (account_status, account_reason),
            ],
            target_status="MISMATCH",
            fallback="CACHE_REST_MISMATCH",
        )
    elif "INSUFFICIENT_DATA" in component_statuses:
        status = "INSUFFICIENT_DATA"
        reason = _first_reason_for_status(
            [
                (position_status, position_reason),
                (orders_status, orders_reason),
                (account_status, account_reason),
            ],
            target_status="INSUFFICIENT_DATA",
            fallback="CACHE_INSUFFICIENT_DATA",
        )
    else:
        status = "MATCH"
        reason = "CACHE_MATCHES_REST"

    dirty_position = normalized_symbol in cache.dirty_position_symbols

    return UserDataCacheAuditResult(
        symbol=normalized_symbol,
        status=status,
        reason=reason,
        cache_age_seconds=cache_age_seconds,
        position_status=position_status,
        orders_status=orders_status,
        account_status=account_status,
        position_match=position_status == "MATCH",
        orders_match=orders_status == "MATCH",
        account_match=account_status == "MATCH",
        rest_position_amount=rest_position_amount,
        cache_position_amount=cache_position_amount,
        rest_open_order_refs=sorted(rest_open_refs),
        cache_open_order_refs=sorted(cache_open_refs),
        account_dirty=cache.account_dirty,
        dirty_position=dirty_position,
        shadow_only=True,
        details={
            "position_reason": position_reason,
            "orders_reason": orders_reason,
            "account_reason": account_reason,
            "cache_summary": cache_summary,
            "safety_note": (
                "Fase 10I-4: auditoria shadow compara cache privado com REST. "
                "Nao altera decisao, nao reduz REST e nao muda ordens."
            ),
        },
    )


def _audit_position(
    cache: UserDataShadowCache,
    symbol: str,
    heavy_snapshot: Any,
) -> tuple[str, str, str, str]:
    if symbol in cache.dirty_position_symbols:
        return "MISMATCH", "POSITION_MARKED_DIRTY", _rest_position_amount(heavy_snapshot), ""

    cached_position = cache.positions_by_symbol.get(symbol)
    rest_amount = _rest_position_amount(heavy_snapshot)

    if cached_position is None:
        return "INSUFFICIENT_DATA", "CACHE_POSITION_MISSING", rest_amount, ""

    cache_amount = str(cached_position.get("position_amount", "") or "0")

    if _decimal_equal(cache_amount, rest_amount):
        return "MATCH", "POSITION_MATCH", rest_amount, cache_amount

    return "MISMATCH", "POSITION_AMOUNT_MISMATCH", rest_amount, cache_amount


def _audit_orders(
    cache: UserDataShadowCache,
    symbol: str,
    heavy_snapshot: Any,
) -> tuple[str, str, set[str], set[str]]:
    rest_refs = _rest_open_order_refs(heavy_snapshot)
    cache_refs = _cache_open_order_refs(cache, symbol)

    if not rest_refs and not cache_refs:
        return "MATCH", "NO_OPEN_ORDERS", rest_refs, cache_refs

    if rest_refs and not cache.orders_by_symbol.get(symbol):
        return "INSUFFICIENT_DATA", "CACHE_ORDER_DATA_MISSING", rest_refs, cache_refs

    if rest_refs == cache_refs:
        return "MATCH", "OPEN_ORDERS_MATCH", rest_refs, cache_refs

    return "MISMATCH", "OPEN_ORDERS_MISMATCH", rest_refs, cache_refs


def _audit_account(
    cache: UserDataShadowCache,
    heavy_snapshot: Any,
) -> tuple[str, str]:
    if cache.account_dirty:
        return "MISMATCH", "ACCOUNT_MARKED_DIRTY"

    if not cache.balances_by_asset:
        return "INSUFFICIENT_DATA", "CACHE_ACCOUNT_DATA_MISSING"

    rest_assets = _rest_assets_by_asset(heavy_snapshot)
    if not rest_assets:
        return "INSUFFICIENT_DATA", "REST_ACCOUNT_ASSETS_MISSING"

    common_assets = set(cache.balances_by_asset) & set(rest_assets)
    if not common_assets:
        return "INSUFFICIENT_DATA", "NO_COMMON_ACCOUNT_ASSETS"

    for asset in common_assets:
        cache_balance = cache.balances_by_asset[asset].get("wallet_balance", "")
        rest_balance = rest_assets[asset].get("walletBalance", "")
        if not _decimal_equal(cache_balance, rest_balance):
            return "MISMATCH", f"ACCOUNT_BALANCE_MISMATCH_{asset}"

    return "MATCH", "ACCOUNT_BALANCES_MATCH"


def _rest_position_amount(heavy_snapshot: Any) -> str:
    position = getattr(heavy_snapshot, "position", None)
    if position is None:
        return "0"

    direction = getattr(getattr(position, "direction", None), "value", "")
    quantity = _to_decimal(getattr(position, "quantity", "0"))

    if direction == "SHORT":
        return str(-quantity)

    if direction == "LONG":
        return str(quantity)

    return "0"


def _rest_open_order_refs(heavy_snapshot: Any) -> set[str]:
    refs: set[str] = set()
    open_orders = getattr(heavy_snapshot, "open_orders", []) or []

    if not isinstance(open_orders, list):
        return refs

    for order in open_orders:
        if not isinstance(order, dict):
            continue

        order_id = str(order.get("orderId", order.get("i", "")) or "").strip()
        client_order_id = str(order.get("clientOrderId", order.get("c", "")) or "").strip()

        if order_id:
            refs.add(order_id)
        if client_order_id:
            refs.add(client_order_id)

    return refs


def _cache_open_order_refs(cache: UserDataShadowCache, symbol: str) -> set[str]:
    refs: set[str] = set()
    orders = cache.orders_by_symbol.get(symbol, {})

    for order in orders.values():
        status = str(order.get("order_status", "") or "").upper().strip()
        if status not in {"NEW", "PARTIALLY_FILLED"}:
            continue

        order_id = str(order.get("order_id", "") or "").strip()
        client_order_id = str(order.get("client_order_id", "") or "").strip()

        if order_id:
            refs.add(order_id)
        if client_order_id:
            refs.add(client_order_id)

    return refs


def _rest_assets_by_asset(heavy_snapshot: Any) -> dict[str, dict[str, Any]]:
    account_info = getattr(heavy_snapshot, "account_info", {}) or {}
    if not isinstance(account_info, dict):
        return {}

    assets = account_info.get("assets", [])
    if not isinstance(assets, list):
        return {}

    mapped: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue

        name = str(asset.get("asset", "") or "").upper().strip()
        if name:
            mapped[name] = asset

    return mapped


def _decimal_equal(left: Any, right: Any) -> bool:
    return _to_decimal(left) == _to_decimal(right)


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _first_reason_for_status(
    status_reasons: list[tuple[str, str]],
    *,
    target_status: str,
    fallback: str,
) -> str:
    for status, reason in status_reasons:
        if status == target_status and reason:
            return reason

    return fallback
