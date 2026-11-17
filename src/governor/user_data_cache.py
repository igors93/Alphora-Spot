import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.models import FuturesPosition, PositionDirection


@dataclass(frozen=True)
class UserDataCacheShadowUpdate:
    event_type: str
    category: str
    operational_type: str
    severity: str
    symbols: list[str]
    order_cache_updated: bool
    position_cache_updated: bool
    account_cache_updated: bool
    dirty_position_symbols: list[str]
    account_dirty: bool
    shadow_only: bool
    cache_summary: dict[str, Any]
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "category": self.category,
            "operational_type": self.operational_type,
            "severity": self.severity,
            "symbols": self.symbols,
            "order_cache_updated": self.order_cache_updated,
            "position_cache_updated": self.position_cache_updated,
            "account_cache_updated": self.account_cache_updated,
            "dirty_position_symbols": self.dirty_position_symbols,
            "account_dirty": self.account_dirty,
            "shadow_only": self.shadow_only,
            "cache_summary": self.cache_summary,
            "details": self.details,
        }


class UserDataShadowCache:
    def __init__(self, *, max_orders_per_symbol: int = 200):
        self.max_orders_per_symbol = max(1, int(max_orders_per_symbol))
        self.orders_by_symbol: dict[str, dict[str, dict[str, Any]]] = {}
        self.positions_by_symbol: dict[str, dict[str, Any]] = {}
        self.balances_by_asset: dict[str, dict[str, Any]] = {}
        self.account_meta: dict[str, Any] = {}
        self.dirty_position_symbols: set[str] = set()
        self.dirty_order_symbols: set[str] = set()
        self.account_dirty = False
        self.last_event_at: float | None = None
        self.last_order_update_at: float | None = None
        self.last_position_update_at: float | None = None
        self.last_account_update_at: float | None = None
        self._lock = threading.RLock()

    def apply_event(self, event: dict[str, Any]) -> UserDataCacheShadowUpdate | None:
        if not isinstance(event, dict):
            return None

        classification = event.get("classification", {})
        if not isinstance(classification, dict):
            classification = {}

        raw = event.get("raw", {})
        if not isinstance(raw, dict):
            raw = {}

        event_type = str(event.get("event_type", "") or "").upper().strip()
        category = (
            str(event.get("operational_category") or classification.get("category", "") or "")
            .upper()
            .strip()
        )
        operational_type = (
            str(event.get("operational_type") or classification.get("operational_type", "") or "")
            .upper()
            .strip()
        )
        severity = (
            str(event.get("severity") or classification.get("severity", "INFO") or "INFO")
            .upper()
            .strip()
        )

        symbols = _normalized_symbols(
            classification.get("symbols") or ([event.get("symbol")] if event.get("symbol") else [])
        )
        shadow_only = bool(event.get("shadow_only", True))
        received_at = _float_or_now(event.get("received_at"))

        should_update_order_cache = bool(classification.get("should_update_order_cache", False))
        should_update_position_cache = bool(
            classification.get("should_update_position_cache", False)
        )
        should_update_account_cache = bool(classification.get("should_update_account_cache", False))

        if not any(
            [
                should_update_order_cache,
                should_update_position_cache,
                should_update_account_cache,
            ]
        ):
            return None

        with self._lock:
            self.last_event_at = received_at

            order_symbols = (
                self._apply_order_update(raw=raw, received_at=received_at)
                if should_update_order_cache
                else []
            )
            position_symbols = (
                self._apply_position_update(raw=raw, received_at=received_at)
                if should_update_position_cache
                else []
            )
            account_updated = (
                self._apply_account_update(
                    raw=raw,
                    event_type=event_type,
                    received_at=received_at,
                )
                if should_update_account_cache
                else False
            )

            dirty_position_symbols = []
            if should_update_position_cache and not position_symbols:
                dirty_position_symbols = symbols
                self.dirty_position_symbols.update(symbols)

            account_dirty = False
            if should_update_account_cache and not account_updated:
                account_dirty = True
                self.account_dirty = True

            updated_symbols = sorted(set(symbols) | set(order_symbols) | set(position_symbols))

            return UserDataCacheShadowUpdate(
                event_type=event_type,
                category=category,
                operational_type=operational_type,
                severity=severity,
                symbols=updated_symbols,
                order_cache_updated=bool(order_symbols),
                position_cache_updated=bool(position_symbols),
                account_cache_updated=account_updated,
                dirty_position_symbols=sorted(dirty_position_symbols),
                account_dirty=account_dirty,
                shadow_only=shadow_only,
                cache_summary=self.summary(),
                details={
                    "order_symbols": sorted(order_symbols),
                    "position_symbols": sorted(position_symbols),
                    "classification_flags": {
                        "should_update_order_cache": should_update_order_cache,
                        "should_update_position_cache": should_update_position_cache,
                        "should_update_account_cache": should_update_account_cache,
                    },
                },
            )

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "orders_symbols_count": len(self.orders_by_symbol),
                "orders_total_count": sum(len(orders) for orders in self.orders_by_symbol.values()),
                "positions_count": len(self.positions_by_symbol),
                "balances_count": len(self.balances_by_asset),
                "dirty_position_symbols": sorted(self.dirty_position_symbols),
                "dirty_order_symbols": sorted(self.dirty_order_symbols),
                "account_dirty": self.account_dirty,
                "last_event_at": self.last_event_at,
                "last_order_update_at": self.last_order_update_at,
                "last_position_update_at": self.last_position_update_at,
                "last_account_update_at": self.last_account_update_at,
            }

    def futures_position_for_symbol(self, symbol: str) -> FuturesPosition | None:
        cached = self.position_snapshot_with_timestamp(symbol)
        return cached[0] if cached is not None else None

    def position_snapshot_with_timestamp(
        self,
        symbol: str,
    ) -> tuple[FuturesPosition, float] | None:
        normalized_symbol = str(symbol or "").upper().strip()
        if not normalized_symbol:
            return None

        with self._lock:
            cached = self.positions_by_symbol.get(normalized_symbol)
            if not cached:
                return None
            value = self._position_from_cached(normalized_symbol, cached)
            observed_at = _float_or_now(cached.get("updated_at"))
            return value, observed_at

    def positions_snapshot(self) -> dict[str, FuturesPosition]:
        with self._lock:
            return {
                symbol: self._position_from_cached(symbol, cached)
                for symbol, cached in self.positions_by_symbol.items()
            }

    def account_info_snapshot(self) -> dict[str, Any] | None:
        cached = self.account_info_snapshot_with_timestamp()
        return cached[0] if cached is not None else None

    def account_info_snapshot_with_timestamp(
        self,
    ) -> tuple[dict[str, Any], float] | None:
        with self._lock:
            if not self.balances_by_asset:
                return None

            assets: list[dict[str, Any]] = []
            latest_updated_at = 0.0
            for asset, balance in sorted(self.balances_by_asset.items()):
                wallet_balance = str(balance.get("wallet_balance", "") or "0")
                cross_wallet_balance = str(
                    balance.get("cross_wallet_balance", "") or wallet_balance
                )
                runtime_balance_fields_trusted = bool(
                    balance.get("runtime_balance_fields_trusted", False)
                )
                updated_at = _float_or_now(balance.get("updated_at"))
                latest_updated_at = max(latest_updated_at, updated_at)

                asset_payload = {
                    "asset": asset,
                    "walletBalance": wallet_balance,
                    "crossWalletBalance": cross_wallet_balance,
                    "source": "user_data_shadow_cache",
                    "updatedAt": balance.get("updated_at"),
                    "runtimeBalanceFieldsTrusted": runtime_balance_fields_trusted,
                }
                for source_key, output_key in (
                    ("available_balance", "availableBalance"),
                    ("locked_balance", "locked"),
                    ("margin_balance", "marginBalance"),
                    ("cross_un_pnl", "crossUnPnl"),
                    ("unrealized_profit", "unrealizedProfit"),
                ):
                    if source_key in balance:
                        asset_payload[output_key] = str(balance.get(source_key) or "0")

                assets.append(asset_payload)

            snapshot = {
                "assets": assets,
                "source": "user_data_shadow_cache",
                "shadow_only": True,
                "runtime_balance_fields_trusted": all(
                    bool(asset.get("runtimeBalanceFieldsTrusted")) for asset in assets
                ),
                "account_meta": dict(self.account_meta),
            }
            observed_at = latest_updated_at or _float_or_now(self.last_account_update_at)
            return snapshot, observed_at

    def open_orders_snapshot(self, symbol: str) -> list[dict[str, Any]]:
        cached = self.open_orders_snapshot_with_timestamp(symbol)
        return cached[0] if cached is not None else []

    def open_orders_snapshot_with_timestamp(
        self,
        symbol: str,
    ) -> tuple[list[dict[str, Any]], float] | None:
        normalized_symbol = str(symbol or "").upper().strip()
        if not normalized_symbol:
            return None

        with self._lock:
            if normalized_symbol not in self.orders_by_symbol:
                return None
            orders = self.orders_by_symbol.get(normalized_symbol, {})
            open_orders: list[dict[str, Any]] = []
            latest_updated_at = 0.0

            for order in orders.values():
                latest_updated_at = max(
                    latest_updated_at,
                    _float_or_now(order.get("updated_at")),
                )
                status = str(order.get("order_status", "") or "").upper().strip()
                if status not in {"NEW", "PARTIALLY_FILLED"}:
                    continue

                open_orders.append(
                    {
                        "symbol": normalized_symbol,
                        "orderId": order.get("order_id", ""),
                        "clientOrderId": order.get("client_order_id", ""),
                        "side": order.get("side", ""),
                        "type": order.get("order_type", ""),
                        "status": status,
                        "origQty": order.get("quantity", ""),
                        "price": order.get("price", ""),
                        "executedQty": order.get("filled_quantity", ""),
                        "avgPrice": order.get("average_price", ""),
                        "reduceOnly": order.get("reduce_only", False),
                        "positionSide": order.get("position_side", ""),
                        "source": "user_data_shadow_cache",
                        "updatedAt": order.get("updated_at"),
                    }
                )

            observed_at = latest_updated_at or _float_or_now(self.last_order_update_at)
            return open_orders, observed_at

    @staticmethod
    def _position_from_cached(
        normalized_symbol: str,
        cached: dict[str, Any],
    ) -> FuturesPosition:
        amount = _to_decimal(cached.get("position_amount", "0"))
        if amount > 0:
            direction = PositionDirection.LONG
        elif amount < 0:
            direction = PositionDirection.SHORT
        else:
            direction = PositionDirection.NONE

        return FuturesPosition(
            symbol=normalized_symbol,
            direction=direction,
            quantity=abs(amount),
            entry_price=_to_decimal(cached.get("entry_price", "0")),
            unrealized_pnl=_to_decimal(cached.get("unrealized_pnl", "0")),
        )

    def reconcile_position_from_rest(
        self,
        *,
        symbol: str,
        position: FuturesPosition,
        received_at: float,
    ) -> bool:
        normalized_symbol = str(symbol or "").upper().strip()
        if not normalized_symbol:
            return False

        quantity = _to_decimal(getattr(position, "quantity", "0"))
        direction = getattr(getattr(position, "direction", None), "value", "")

        if direction == "SHORT":
            amount = -quantity
        elif direction == "LONG":
            amount = quantity
        else:
            amount = Decimal("0")

        with self._lock:
            self.positions_by_symbol[normalized_symbol] = {
                "symbol": normalized_symbol,
                "position_amount": str(amount),
                "entry_price": str(getattr(position, "entry_price", "0") or "0"),
                "unrealized_pnl": str(getattr(position, "unrealized_pnl", "0") or "0"),
                "break_even_price": "",
                "position_side": "BOTH",
                "margin_type": "",
                "isolated_wallet": "",
                "updated_at": received_at,
                "source": "rest_reconciliation",
            }
            self.dirty_position_symbols.discard(normalized_symbol)
            self.last_position_update_at = received_at
            self.last_event_at = received_at
        return True

    def reconcile_account_from_rest(
        self,
        *,
        account_info: dict[str, Any],
        received_at: float,
    ) -> bool:
        assets = account_info.get("assets", [])
        if not isinstance(assets, list):
            return False

        updated = False
        with self._lock:
            for asset_payload in assets:
                if not isinstance(asset_payload, dict):
                    continue

                asset = str(asset_payload.get("asset", "") or "").upper().strip()
                if not asset:
                    continue

                wallet_balance = str(asset_payload.get("walletBalance", "") or "0")
                cross_wallet_balance = str(
                    asset_payload.get("crossWalletBalance", "") or wallet_balance
                )
                runtime_balance_fields_trusted = "availableBalance" in asset_payload

                balance = {
                    "asset": asset,
                    "wallet_balance": wallet_balance,
                    "cross_wallet_balance": cross_wallet_balance,
                    "balance_change": "",
                    "updated_at": received_at,
                    "source": "rest_reconciliation",
                    "runtime_balance_fields_trusted": runtime_balance_fields_trusted,
                }
                for source_key, output_key in (
                    ("availableBalance", "available_balance"),
                    ("marginBalance", "margin_balance"),
                    ("crossUnPnl", "cross_un_pnl"),
                    ("unrealizedProfit", "unrealized_profit"),
                ):
                    if source_key in asset_payload:
                        balance[output_key] = str(asset_payload.get(source_key) or "0")

                self.balances_by_asset[asset] = balance
                updated = True

            if updated:
                self.account_dirty = False
                self.account_meta["last_rest_reconciliation_at"] = received_at
                self.last_account_update_at = received_at
                self.last_event_at = received_at

        return updated

    def reconcile_open_orders_from_rest(
        self,
        *,
        symbol: str,
        open_orders: list[dict[str, Any]],
        received_at: float,
    ) -> bool:
        normalized_symbol = str(symbol or "").upper().strip()
        if not normalized_symbol:
            return False

        if not isinstance(open_orders, list):
            return False

        reconciled_orders: dict[str, dict[str, Any]] = {}

        for order in open_orders:
            if not isinstance(order, dict):
                continue

            order_id = str(order.get("orderId", order.get("i", "")) or "").strip()
            client_order_id = str(order.get("clientOrderId", order.get("c", "")) or "").strip()
            cache_key = order_id or client_order_id
            if not cache_key:
                continue

            status = str(order.get("status", order.get("X", "NEW")) or "NEW")
            status = status.upper().strip()

            reconciled_orders[cache_key] = {
                "symbol": normalized_symbol,
                "order_id": order_id,
                "client_order_id": client_order_id,
                "side": str(order.get("side", order.get("S", "")) or "").upper().strip(),
                "order_type": str(order.get("type", order.get("o", "")) or "").upper().strip(),
                "execution_type": "REST_RECONCILIATION",
                "order_status": status,
                "position_side": str(order.get("positionSide", order.get("ps", "")) or "")
                .upper()
                .strip(),
                "reduce_only": bool(order.get("reduceOnly", False)),
                "quantity": str(order.get("origQty", order.get("q", "")) or ""),
                "price": str(order.get("price", order.get("p", "")) or ""),
                "average_price": str(order.get("avgPrice", order.get("ap", "")) or ""),
                "last_filled_quantity": "",
                "filled_quantity": str(order.get("executedQty", order.get("z", "")) or ""),
                "last_filled_price": "",
                "realized_pnl": "",
                "commission": "",
                "commission_asset": "",
                "updated_at": received_at,
                "source": "rest_reconciliation",
            }

        with self._lock:
            self.orders_by_symbol[normalized_symbol] = reconciled_orders
            self.dirty_order_symbols.discard(normalized_symbol)
            self.last_order_update_at = received_at
            self.last_event_at = received_at
        return True

    def _apply_order_update(
        self,
        *,
        raw: dict[str, Any],
        received_at: float,
    ) -> list[str]:
        if str(raw.get("e", "")).upper() == "SPOT_STREAM_RESYNC_REQUIRED":
            self.dirty_order_symbols.update(self.orders_by_symbol)
            self.orders_by_symbol.clear()
            self.dirty_position_symbols.update(self.positions_by_symbol)
            return []
        if str(raw.get("e", "")).upper() == "LISTSTATUS":
            symbol = str(raw.get("s", "") or "").upper().strip()
            if symbol:
                self.orders_by_symbol.pop(symbol, None)
                self.dirty_order_symbols.add(symbol)
            return []
        order = raw if str(raw.get("e", "")).upper() == "EXECUTIONREPORT" else raw.get("o")
        if not isinstance(order, dict):
            return []

        symbol = str(order.get("s", "") or "").upper().strip()
        if not symbol:
            return []

        order_id = str(order.get("i", "") or "").strip()
        client_order_id = str(order.get("c", "") or "").strip()
        cache_key = order_id or client_order_id
        if not cache_key:
            return []

        orders = self.orders_by_symbol.setdefault(symbol, {})
        orders[cache_key] = {
            "symbol": symbol,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "side": str(order.get("S", "") or "").upper().strip(),
            "order_type": str(order.get("o", "") or "").upper().strip(),
            "execution_type": str(order.get("x", "") or "").upper().strip(),
            "order_status": str(order.get("X", "") or "").upper().strip(),
            "position_side": str(order.get("ps", "") or "").upper().strip(),
            "reduce_only": bool(order.get("R", False)),
            "quantity": str(order.get("q", "") or ""),
            "price": str(order.get("p", "") or ""),
            "average_price": str(order.get("ap", "") or ""),
            "last_filled_quantity": str(order.get("l", "") or ""),
            "filled_quantity": str(order.get("z", "") or ""),
            "last_filled_price": str(order.get("L", "") or ""),
            "realized_pnl": str(order.get("rp", "") or ""),
            "commission": str(order.get("n", "") or ""),
            "commission_asset": str(order.get("N", "") or ""),
            "cumulative_quote_quantity": str(order.get("Z", "") or ""),
            "last_quote_quantity": str(order.get("Y", "") or ""),
            "trade_id": str(order.get("t", "") or ""),
            "order_list_id": str(order.get("g", "") or ""),
            "updated_at": received_at,
        }

        self._trim_orders(symbol)
        self.last_order_update_at = received_at
        return [symbol]

    def _apply_position_update(
        self,
        *,
        raw: dict[str, Any],
        received_at: float,
    ) -> list[str]:
        position_payloads = _position_payloads(raw)
        updated_symbols = []

        for position in position_payloads:
            symbol = str(position.get("s", "") or "").upper().strip()
            if not symbol:
                continue

            self.positions_by_symbol[symbol] = {
                "symbol": symbol,
                "position_amount": str(position.get("pa", position.get("positionAmt", "")) or ""),
                "entry_price": str(position.get("ep", position.get("entryPrice", "")) or ""),
                "unrealized_pnl": str(
                    position.get("up", position.get("unRealizedProfit", "")) or ""
                ),
                "break_even_price": str(position.get("bep", "") or ""),
                "position_side": str(position.get("ps", "") or "").upper().strip(),
                "margin_type": str(position.get("mt", "") or "").upper().strip(),
                "isolated_wallet": str(position.get("iw", "") or ""),
                "updated_at": received_at,
            }
            self.dirty_position_symbols.discard(symbol)
            updated_symbols.append(symbol)

        if updated_symbols:
            self.last_position_update_at = received_at

        return sorted(set(updated_symbols))

    def _apply_account_update(
        self,
        *,
        raw: dict[str, Any],
        event_type: str,
        received_at: float,
    ) -> bool:
        account = raw.get("a")
        updated = False

        if event_type == "OUTBOUNDACCOUNTPOSITION":
            for balance in raw.get("B", []):
                if not isinstance(balance, dict):
                    continue
                asset = str(balance.get("a", "") or "").upper().strip()
                if not asset:
                    continue
                free = _to_decimal(balance.get("f"))
                locked = _to_decimal(balance.get("l"))
                self.balances_by_asset[asset] = {
                    "asset": asset, "wallet_balance": str(free + locked),
                    "cross_wallet_balance": str(free + locked),
                    "available_balance": str(free), "locked_balance": str(locked),
                    "runtime_balance_fields_trusted": True,
                    "updated_at": received_at, "source": "spot_user_stream",
                }
                updated = True
            if updated:
                self.last_account_update_at = received_at
            return updated

        if event_type == "BALANCEUPDATE":
            self.account_meta["last_spot_balance_delta"] = {
                "asset": raw.get("a"), "delta": raw.get("d"), "at": received_at,
            }
            return False

        if isinstance(account, dict):
            balances = account.get("B")
            if isinstance(balances, list):
                for balance in balances:
                    if not isinstance(balance, dict):
                        continue

                    asset = str(balance.get("a", "") or "").upper().strip()
                    if not asset:
                        continue

                    self.balances_by_asset[asset] = {
                        "asset": asset,
                        "wallet_balance": str(balance.get("wb", "") or ""),
                        "cross_wallet_balance": str(balance.get("cw", "") or ""),
                        "balance_change": str(balance.get("bc", "") or ""),
                        "updated_at": received_at,
                        "runtime_balance_fields_trusted": False,
                    }
                    updated = True

            reason = str(account.get("m", "") or "").upper().strip()
            if reason:
                self.account_meta["last_account_update_reason"] = reason
                self.account_meta["last_account_update_at"] = received_at
                updated = True

        if event_type == "MARGIN_CALL":
            self.account_meta["last_margin_call_at"] = received_at
            updated = True

        if event_type == "ACCOUNT_CONFIG_UPDATE":
            self.account_meta["last_account_config_update_at"] = received_at
            updated = True

        if updated:
            self.account_dirty = False
            self.last_account_update_at = received_at

        return updated

    def _trim_orders(self, symbol: str) -> None:
        orders = self.orders_by_symbol.get(symbol)
        if not orders:
            return

        while len(orders) > self.max_orders_per_symbol:
            oldest_key = next(iter(orders))
            orders.pop(oldest_key, None)


def _position_payloads(raw: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []

    account = raw.get("a")
    if isinstance(account, dict):
        positions = account.get("P")
        if isinstance(positions, list):
            payloads.extend(item for item in positions if isinstance(item, dict))

    margin_positions = raw.get("p")
    if isinstance(margin_positions, list):
        payloads.extend(item for item in margin_positions if isinstance(item, dict))

    return payloads


def _normalized_symbols(values: Any) -> list[str]:
    if not isinstance(values, list | tuple | set):
        values = [values]

    return sorted({str(value).upper().strip() for value in values if str(value or "").strip()})


def _float_or_now(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")
