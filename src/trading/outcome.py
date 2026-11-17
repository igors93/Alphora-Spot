from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from domain.models import PositionDirection
from domain.trade_result import TradeResult

TRADE_PLAN_OUTCOME_VERSION = "1.0.0"
TRADE_PLAN_OUTCOME_SOURCE_PAPER = "PAPER"
TRADE_PLAN_OUTCOME_SOURCE_LIVE = "LIVE"

OUTCOME_WIN = "WIN"
OUTCOME_LOSS = "LOSS"
OUTCOME_BREAKEVEN = "BREAKEVEN"
OUTCOME_UNKNOWN = "UNKNOWN"

PLAN_LINKED = "LINKED"
PLAN_UNLINKED = "UNLINKED"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_outcome_id() -> str:
    return f"out_{uuid4().hex}"


@dataclass(frozen=True)
class TradePlanOutcome:
    symbol: str
    direction: str
    entry_price: Decimal
    exit_price: Decimal | None
    quantity: Decimal
    notional: Decimal
    trade_plan_id: str | None
    active_trade_id: str | None
    gross_pnl: Decimal | None
    net_pnl: Decimal | None
    return_pct: Decimal | None
    fees: Decimal | None = None
    slippage_cost: Decimal | None = None
    spread_cost: Decimal | None = None
    funding_cost: Decimal | None = None
    exit_reason: str = ""
    strategy_reason_entry: str = ""
    strategy_reason_exit: str = ""
    planned_stop_price: Decimal | None = None
    planned_take_profit_price: Decimal | None = None
    adaptive_adjustments_applied: int = 0
    last_adaptive_adjustment_plan_id: str | None = None
    outcome_status: str = OUTCOME_UNKNOWN
    plan_link_status: str = PLAN_UNLINKED
    pnl_status: str = ""
    pnl_source: str = ""
    source: str = TRADE_PLAN_OUTCOME_SOURCE_PAPER
    outcome_id: str = field(default_factory=_new_outcome_id)
    outcome_version: str = TRADE_PLAN_OUTCOME_VERSION
    created_at: str = field(default_factory=_utc_now_iso)
    diagnostics: dict[str, Any] = field(default_factory=dict)


TradeOutcome = TradePlanOutcome


def build_trade_plan_outcome_from_trade_result(
    *,
    trade_result: TradeResult,
    trade_plan_id: str | None,
    active_trade_id: str | None = None,
    planned_stop_price: Decimal | None = None,
    planned_take_profit_price: Decimal | None = None,
    adaptive_adjustments_applied: int = 0,
    last_adaptive_adjustment_plan_id: str | None = None,
    source: str = TRADE_PLAN_OUTCOME_SOURCE_PAPER,
) -> TradePlanOutcome:
    return TradePlanOutcome(
        symbol=trade_result.symbol,
        direction=trade_result.direction.value,
        entry_price=trade_result.entry_price,
        exit_price=trade_result.exit_price,
        quantity=trade_result.quantity,
        notional=trade_result.notional,
        trade_plan_id=trade_plan_id,
        active_trade_id=active_trade_id,
        gross_pnl=trade_result.gross_pnl,
        net_pnl=trade_result.net_pnl,
        return_pct=trade_result.return_pct,
        fees=trade_result.fees,
        slippage_cost=trade_result.slippage_cost,
        spread_cost=trade_result.spread_cost,
        funding_cost=trade_result.funding_cost,
        exit_reason=trade_result.exit_reason,
        strategy_reason_entry=trade_result.strategy_reason_entry,
        strategy_reason_exit=trade_result.strategy_reason_exit,
        planned_stop_price=planned_stop_price,
        planned_take_profit_price=planned_take_profit_price,
        adaptive_adjustments_applied=adaptive_adjustments_applied,
        last_adaptive_adjustment_plan_id=last_adaptive_adjustment_plan_id,
        outcome_status=_classify_outcome(trade_result.net_pnl),
        plan_link_status=_plan_link_status(trade_plan_id),
        pnl_status="confirmed",
        pnl_source="trade_result",
        source=source,
        diagnostics={
            "has_trade_plan_id": trade_plan_id is not None,
            "has_adaptive_adjustment": adaptive_adjustments_applied > 0,
        },
    )


def build_trade_plan_outcome_from_live_payload(
    *,
    symbol: str,
    direction: PositionDirection,
    entry_price: Decimal,
    quantity: Decimal,
    trade_plan_id: str | None,
    active_trade_id: str | None,
    planned_stop_price: Decimal | None,
    planned_take_profit_price: Decimal | None,
    exit_reason: str,
    pnl_result: dict,
) -> TradePlanOutcome:
    close_price = _optional_decimal(pnl_result.get("close_price_estimate"))
    gross_pnl = _optional_decimal(pnl_result.get("gross_pnl_estimate"))
    net_pnl = _optional_decimal(pnl_result.get("net_pnl"))
    commission_income = _optional_decimal(pnl_result.get("commission"))
    funding_income = _optional_decimal(pnl_result.get("funding_fee"))
    notional = entry_price * quantity
    return_pct = net_pnl / notional if net_pnl is not None and notional > 0 else None

    return TradePlanOutcome(
        symbol=symbol,
        direction=direction.value,
        entry_price=entry_price,
        exit_price=close_price,
        quantity=quantity,
        notional=notional,
        trade_plan_id=trade_plan_id,
        active_trade_id=active_trade_id,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        return_pct=return_pct,
        fees=(-commission_income if commission_income is not None else None),
        funding_cost=(-funding_income if funding_income is not None else None),
        exit_reason=exit_reason,
        planned_stop_price=planned_stop_price,
        planned_take_profit_price=planned_take_profit_price,
        outcome_status=_classify_outcome(net_pnl),
        plan_link_status=_plan_link_status(trade_plan_id),
        pnl_status=str(pnl_result.get("pnl_status", "")),
        pnl_source=str(pnl_result.get("pnl_source", "")),
        source=TRADE_PLAN_OUTCOME_SOURCE_LIVE,
        diagnostics={
            "has_trade_plan_id": trade_plan_id is not None,
            "live_pnl_confirmed": net_pnl is not None,
            "raw_pnl_result": pnl_result,
        },
    )


def _classify_outcome(net_pnl: Decimal | None) -> str:
    if net_pnl is None:
        return OUTCOME_UNKNOWN

    if net_pnl > 0:
        return OUTCOME_WIN

    if net_pnl < 0:
        return OUTCOME_LOSS

    return OUTCOME_BREAKEVEN


def _plan_link_status(trade_plan_id: str | None) -> str:
    return PLAN_LINKED if trade_plan_id else PLAN_UNLINKED


def _optional_decimal(value) -> Decimal | None:
    if value is None:
        return None

    try:
        return Decimal(str(value))
    except Exception:
        return None
