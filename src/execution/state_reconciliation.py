from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from operational_module_modes import ReconciliationMode, parse_module_mode

RECONCILIATION_VERSION = "1.0.0"

RECONCILIATION_STATUS_OK = "OK"
RECONCILIATION_STATUS_WARNING = "WARNING"
RECONCILIATION_STATUS_DIVERGENT = "DIVERGENT"
RECONCILIATION_STATUS_BLOCKED = "BLOCKED"
RECONCILIATION_STATUS_REPAIRED = "REPAIRED"
RECONCILIATION_STATUS_REPAIR_FAILED = "REPAIR_FAILED"


@dataclass(frozen=True)
class ExchangeStateSnapshot:
    symbol: str
    local_position: Any = None
    exchange_position: Any = None
    normal_orders: list[dict[str, Any]] = field(default_factory=list)
    algo_orders: list[dict[str, Any]] = field(default_factory=list)
    robot_normal_orders: list[dict[str, Any]] = field(default_factory=list)
    robot_algo_orders: list[dict[str, Any]] = field(default_factory=list)
    has_open_position: bool = False
    has_stop_protection: bool = False
    has_take_profit_protection: bool = False
    has_orphan_orders: bool = False
    data_source: str = "UNKNOWN"
    timestamp: Any = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "local_position": _position_payload(self.local_position),
            "exchange_position": _position_payload(self.exchange_position),
            "normal_orders_count": len(self.normal_orders),
            "algo_orders_count": len(self.algo_orders),
            "robot_normal_orders_count": len(self.robot_normal_orders),
            "robot_algo_orders_count": len(self.robot_algo_orders),
            "has_open_position": self.has_open_position,
            "has_stop_protection": self.has_stop_protection,
            "has_take_profit_protection": self.has_take_profit_protection,
            "has_orphan_orders": self.has_orphan_orders,
            "data_source": self.data_source,
            "timestamp": self.timestamp,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    severity: str
    symbol: str
    description: str
    can_block: bool
    can_repair: bool
    suggested_action: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "symbol": self.symbol,
            "description": self.description,
            "can_block": self.can_block,
            "can_repair": self.can_repair,
            "suggested_action": self.suggested_action,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class ReconciliationDecision:
    mode: ReconciliationMode
    status: str
    issues: list[ReconciliationIssue]
    should_block_new_entries: bool
    repairs_attempted: int = 0
    repairs_succeeded: int = 0
    repairs_failed: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)
    affected_result: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "status": self.status,
            "issues": [issue.to_dict() for issue in self.issues],
            "issues_count": len(self.issues),
            "should_block_new_entries": self.should_block_new_entries,
            "repairs_attempted": self.repairs_attempted,
            "repairs_succeeded": self.repairs_succeeded,
            "repairs_failed": self.repairs_failed,
            "affected_result": self.affected_result,
            "diagnostics": dict(self.diagnostics),
        }


class StateReconciliationEngine:
    def evaluate(
        self,
        snapshot: ExchangeStateSnapshot,
        *,
        mode: ReconciliationMode | str,
        repair_client: Any = None,
    ) -> ReconciliationDecision:
        normalized_mode = _normalize_mode(mode)
        diagnostics = {
            "version": RECONCILIATION_VERSION,
            "mode": normalized_mode.value,
            "snapshot": snapshot.to_dict(),
        }
        if normalized_mode is ReconciliationMode.OFF:
            return ReconciliationDecision(
                mode=normalized_mode,
                status=RECONCILIATION_STATUS_OK,
                issues=[],
                should_block_new_entries=False,
                diagnostics=diagnostics,
                affected_result=False,
            )

        issues = _detect_issues(snapshot)
        should_block = normalized_mode.can_block and any(
            issue.can_block and issue.severity == "CRITICAL" for issue in issues
        )
        status = _status_for_issues(
            issues,
            mode=normalized_mode,
            should_block=should_block,
        )
        decision = ReconciliationDecision(
            mode=normalized_mode,
            status=status,
            issues=issues,
            should_block_new_entries=should_block,
            diagnostics=diagnostics,
            affected_result=should_block,
        )
        if normalized_mode.can_repair:
            decision = _record_safe_repair_skips(snapshot, decision)
        if normalized_mode.can_repair and repair_client is not None:
            decision = _apply_safe_repairs(
                snapshot,
                decision,
                repair_client=repair_client,
            )
        return decision


def build_exchange_state_snapshot(
    *,
    symbol: str,
    local_position: Any = None,
    exchange_position: Any = None,
    normal_orders: list[dict[str, Any]] | None = None,
    algo_orders: list[dict[str, Any]] | None = None,
    robot_normal_orders: list[dict[str, Any]] | None = None,
    robot_algo_orders: list[dict[str, Any]] | None = None,
    data_source: str = "UNKNOWN",
    timestamp: Any = None,
    diagnostics: dict[str, Any] | None = None,
) -> ExchangeStateSnapshot:
    normal = list(normal_orders or [])
    algo = list(algo_orders or [])
    robot_normal = list(robot_normal_orders or [])
    robot_algo = list(robot_algo_orders or [])
    exchange_open = _position_is_open(exchange_position)
    has_stop = any(_is_stop_order(order) for order in robot_algo)
    has_take = any(_is_take_profit_order(order) for order in robot_algo)
    return ExchangeStateSnapshot(
        symbol=str(symbol or "").upper().strip(),
        local_position=local_position,
        exchange_position=exchange_position,
        normal_orders=normal,
        algo_orders=algo,
        robot_normal_orders=robot_normal,
        robot_algo_orders=robot_algo,
        has_open_position=exchange_open,
        has_stop_protection=exchange_open and has_stop,
        has_take_profit_protection=exchange_open and has_take,
        has_orphan_orders=(not exchange_open and bool(robot_normal or robot_algo)),
        data_source=data_source,
        timestamp=timestamp,
        diagnostics=dict(diagnostics or {}),
    )


def _detect_issues(snapshot: ExchangeStateSnapshot) -> list[ReconciliationIssue]:
    issues: list[ReconciliationIssue] = []
    symbol = snapshot.symbol
    local_open = _position_is_open(snapshot.local_position)
    exchange_open = _position_is_open(snapshot.exchange_position)

    if exchange_open and not local_open:
        issues.append(
            _issue(
                "EXCHANGE_POSITION_WITHOUT_LOCAL_STATE",
                "CRITICAL",
                symbol,
                "Exchange possui posicao aberta, mas estado local nao acompanha.",
                can_block=True,
                can_repair=False,
                suggested_action="MANUAL_RECONCILE",
            )
        )
    if local_open and not exchange_open:
        issues.append(
            _issue(
                "LOCAL_POSITION_WITHOUT_EXCHANGE_POSITION",
                "WARNING",
                symbol,
                "Estado local indica posicao aberta, mas exchange esta flat.",
                can_block=True,
                can_repair=False,
                suggested_action="VERIFY_LOCAL_STATE",
            )
        )
    if exchange_open and not snapshot.has_stop_protection:
        issues.append(
            _issue(
                "POSITION_WITHOUT_STOP",
                "CRITICAL",
                symbol,
                "Posicao aberta sem stop de protecao do robo.",
                can_block=True,
                can_repair=False,
                suggested_action="RESTORE_PROTECTION_OR_MANUAL_RECONCILE",
            )
        )
    if exchange_open and not snapshot.has_take_profit_protection:
        issues.append(
            _issue(
                "POSITION_WITHOUT_TAKE_PROFIT",
                "CRITICAL",
                symbol,
                "Posicao aberta sem take profit de protecao do robo.",
                can_block=True,
                can_repair=False,
                suggested_action="RESTORE_PROTECTION_OR_MANUAL_RECONCILE",
            )
        )
    if snapshot.has_orphan_orders:
        issues.append(
            _issue(
                "ORPHAN_ROBOT_ORDERS",
                "WARNING",
                symbol,
                "Ordens do robo abertas sem posicao na exchange.",
                can_block=False,
                can_repair=True,
                suggested_action="CANCEL_ORPHAN_ROBOT_ORDERS",
                diagnostics={
                    "robot_normal_orders_count": len(snapshot.robot_normal_orders),
                    "robot_algo_orders_count": len(snapshot.robot_algo_orders),
                },
            )
        )
    stop_count = sum(1 for order in snapshot.robot_algo_orders if _is_stop_order(order))
    take_count = sum(1 for order in snapshot.robot_algo_orders if _is_take_profit_order(order))
    if exchange_open and stop_count > 1:
        issues.append(
            _issue(
                "DUPLICATED_STOP_PROTECTION",
                "WARNING",
                symbol,
                "Mais de um stop do robo encontrado para a mesma posicao.",
                can_block=True,
                can_repair=False,
                suggested_action="MANUAL_REVIEW_DUPLICATED_PROTECTION",
                diagnostics={"stop_count": stop_count},
            )
        )
    if exchange_open and take_count > 1:
        issues.append(
            _issue(
                "DUPLICATED_TAKE_PROFIT_PROTECTION",
                "WARNING",
                symbol,
                "Mais de um take profit do robo encontrado para a mesma posicao.",
                can_block=True,
                can_repair=False,
                suggested_action="MANUAL_REVIEW_DUPLICATED_PROTECTION",
                diagnostics={"take_profit_count": take_count},
            )
        )
    quantity_delta = _quantity_delta(snapshot.local_position, snapshot.exchange_position)
    if local_open and exchange_open and quantity_delta > Decimal("0.00000001"):
        issues.append(
            _issue(
                "LOCAL_EXCHANGE_QUANTITY_MISMATCH",
                "CRITICAL",
                symbol,
                "Quantidade local diverge da quantidade na exchange.",
                can_block=True,
                can_repair=False,
                suggested_action="MANUAL_RECONCILE",
                diagnostics={"quantity_delta": str(quantity_delta)},
            )
        )
    if bool(snapshot.diagnostics.get("user_stream_stale")):
        issues.append(
            _issue(
                "USER_STREAM_STALE",
                "WARNING",
                symbol,
                "Dados privados do user stream estao stale.",
                can_block=False,
                can_repair=False,
                suggested_action="REFRESH_REST_SNAPSHOT",
            )
        )
    return issues


def _apply_safe_repairs(
    snapshot: ExchangeStateSnapshot,
    decision: ReconciliationDecision,
    *,
    repair_client: Any,
) -> ReconciliationDecision:
    repairable_orphans = [issue for issue in decision.issues if issue.code == "ORPHAN_ROBOT_ORDERS"]
    if not repairable_orphans or snapshot.has_open_position:
        return decision

    attempted = 0
    succeeded = 0
    errors: list[str] = []
    for order in snapshot.robot_normal_orders:
        attempted += 1
        try:
            repair_client.cancel_order(
                symbol=snapshot.symbol,
                order_id=order.get("orderId"),
                orig_client_order_id=_normal_client_order_id(order) or None,
            )
            succeeded += 1
        except Exception as error:  # pragma: no cover - kept defensive for live client variants
            errors.append(str(error))
    for order in snapshot.robot_algo_orders:
        attempted += 1
        try:
            repair_client.cancel_algo_order(
                algo_id=order.get("algoId"),
                client_algo_id=_algo_client_id(order) or None,
            )
            succeeded += 1
        except Exception as error:  # pragma: no cover
            errors.append(str(error))

    status = (
        RECONCILIATION_STATUS_REPAIRED
        if attempted == succeeded
        else RECONCILIATION_STATUS_REPAIR_FAILED
    )
    diagnostics = {
        **decision.diagnostics,
        "repair_errors": errors,
        "safe_repair": "CANCEL_ORPHAN_ROBOT_ORDERS",
    }
    return replace(
        decision,
        status=status,
        repairs_attempted=attempted,
        repairs_succeeded=succeeded,
        repairs_failed=max(0, attempted - succeeded),
        diagnostics=diagnostics,
        affected_result=decision.affected_result or attempted > 0,
    )


def _record_safe_repair_skips(
    snapshot: ExchangeStateSnapshot,
    decision: ReconciliationDecision,
) -> ReconciliationDecision:
    skipped = []
    if snapshot.has_open_position:
        for issue in decision.issues:
            if issue.code in {"POSITION_WITHOUT_STOP", "POSITION_WITHOUT_TAKE_PROFIT"}:
                skipped.append(
                    {
                        "issue_code": issue.code,
                        "reason": "REPAIR_SKIPPED_NO_VALID_PLANNED_EXIT",
                        "safe_repair": "RESTORE_PROTECTION",
                    }
                )
    if not skipped:
        return decision
    diagnostics = {
        **decision.diagnostics,
        "repair_skipped": skipped,
        "repair_policy": ("Protecao automatica continua desativada sem planned_exit confiavel."),
    }
    return replace(decision, diagnostics=diagnostics)


def _status_for_issues(
    issues: list[ReconciliationIssue],
    *,
    mode: ReconciliationMode,
    should_block: bool,
) -> str:
    if not issues:
        return RECONCILIATION_STATUS_OK
    if should_block and mode.can_block:
        return RECONCILIATION_STATUS_BLOCKED
    if any(issue.severity == "CRITICAL" for issue in issues):
        return RECONCILIATION_STATUS_DIVERGENT
    return RECONCILIATION_STATUS_WARNING


def _issue(
    code: str,
    severity: str,
    symbol: str,
    description: str,
    *,
    can_block: bool,
    can_repair: bool,
    suggested_action: str,
    diagnostics: dict[str, Any] | None = None,
) -> ReconciliationIssue:
    return ReconciliationIssue(
        code=code,
        severity=severity,
        symbol=symbol,
        description=description,
        can_block=can_block,
        can_repair=can_repair,
        suggested_action=suggested_action,
        diagnostics=dict(diagnostics or {}),
    )


def _position_is_open(position: Any) -> bool:
    if position is None:
        return False
    if hasattr(position, "is_open"):
        return bool(position.is_open)
    quantity = _position_quantity(position)
    return quantity != 0


def _position_quantity(position: Any) -> Decimal:
    if position is None:
        return Decimal("0")
    if isinstance(position, dict):
        raw = (
            position.get("quantity")
            or position.get("positionAmt")
            or position.get("position_quantity")
            or "0"
        )
    else:
        raw = getattr(position, "quantity", None)
        if raw is None:
            raw = getattr(position, "positionAmt", "0")
    return abs(_decimal_or_zero(raw))


def _quantity_delta(local_position: Any, exchange_position: Any) -> Decimal:
    return abs(_position_quantity(local_position) - _position_quantity(exchange_position))


def _is_stop_order(order: dict[str, Any]) -> bool:
    order_type = _order_type(order)
    return "STOP" in order_type and "TAKE" not in order_type


def _is_take_profit_order(order: dict[str, Any]) -> bool:
    return "TAKE_PROFIT" in _order_type(order)


def _order_type(order: dict[str, Any]) -> str:
    return str(order.get("orderType") or order.get("type") or order.get("origType") or "").upper()


def _normal_client_order_id(order: dict[str, Any]) -> str:
    return str(order.get("clientOrderId") or order.get("origClientOrderId") or "")


def _algo_client_id(order: dict[str, Any]) -> str:
    return str(
        order.get("clientAlgoId") or order.get("clientAlgoID") or order.get("clientOrderId") or ""
    )


def _position_payload(position: Any) -> dict[str, Any] | None:
    if position is None:
        return None
    direction = getattr(position, "direction", None)
    if isinstance(position, dict):
        return dict(position)
    return {
        "symbol": getattr(position, "symbol", None),
        "direction": getattr(direction, "value", direction),
        "quantity": str(getattr(position, "quantity", "0")),
        "entry_price": str(getattr(position, "entry_price", "0")),
        "is_open": bool(getattr(position, "is_open", False)),
    }


def _normalize_mode(value: ReconciliationMode | str) -> ReconciliationMode:
    return parse_module_mode(value, ReconciliationMode, name="state_reconciliation_mode")  # type: ignore[return-value]


def _decimal_or_zero(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not decimal.is_finite():
        return Decimal("0")
    return decimal


__all__ = [
    "ExchangeStateSnapshot",
    "ReconciliationDecision",
    "ReconciliationIssue",
    "StateReconciliationEngine",
    "build_exchange_state_snapshot",
]
