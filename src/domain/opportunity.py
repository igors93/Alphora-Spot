"""Operable trade opportunity and its policy-resolved outcome.

Every forecast and every training sample in Alphora belongs to a ``TradeOpportunity``:
a candidate trade that could have been known at ``knowledge_time`` (from closed data up
to ``data_watermark``) and executed under a versioned ``TradingPolicy``.

Non-path-dependent constraints (exchange rounding, minimum notional, protection buffer,
anti-HFT cadence, allowed timeframe, entry fill rule) are recorded in
``ExecutabilityCheck``; a candidate that fails any of them becomes ``NotExecutable`` and
must neither receive a forecast nor enter a training set. Path-dependent portfolio state
(open positions, exposure, daily PnL) is recorded separately in ``PathDependentContext``
so datasets do not depend on a single portfolio trajectory.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from domain.barriers import (
    LIQUIDATION,
    LONG,
    NOT_FILLED,
    OPEN,
    SHORT,
    SL_FIRST,
    STOP_LOSS,
    TAKE_PROFIT,
    TIMEOUT,
    TP_FIRST,
)


def _dec_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)).normalize(), "f")


def format_knowledge_time(timestamp_ms: int) -> str:
    """Format an epoch millisecond watermark as an ISO-8601 UTC string."""

    dt = datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=timezone.utc)
    if int(timestamp_ms) % 1000 == 0:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.isoformat().replace("+00:00", "Z")


def build_opportunity_id(
    *,
    symbol: str,
    direction: str,
    watermark_time_ms: int,
    origin: str,
    policy_version: str,
    policy_variant: str = "default",
) -> str:
    """Deterministic identifier shared by live and backtest for the same candle and policy."""

    normalized_symbol = str(symbol or "").upper().strip()
    normalized_direction = str(direction or "").upper().strip()
    normalized_origin = str(origin or "strategy").strip()
    normalized_variant = str(policy_variant or "default").strip()
    normalized_policy = str(policy_version or "").strip()
    dt = datetime.fromtimestamp(int(watermark_time_ms) / 1000.0, tz=timezone.utc)
    compact_ts = dt.strftime("%Y%m%dT%H%M%SZ")
    short_origin = re.sub(r"[^a-z0-9]+", "", normalized_origin.split(".")[-1].lower())[:16]
    if not short_origin:
        short_origin = "strategy"
    canonical = (
        f"{normalized_symbol}|{normalized_direction}|{int(watermark_time_ms)}|"
        f"{normalized_origin}|{normalized_variant}|{normalized_policy}"
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"opp-{normalized_symbol}-{compact_ts}-{short_origin}-{digest}"


@dataclass(frozen=True)
class DataWatermark:
    """What market data was closed and available when the opportunity was evaluated."""

    timeframe: str
    last_closed_open_time_ms: int | None
    last_closed_close_time_ms: int | None
    candle_count: int
    funding_rate_known: bool = False
    mark_price_known: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "last_closed_open_time_ms": self.last_closed_open_time_ms,
            "last_closed_close_time_ms": self.last_closed_close_time_ms,
            "candle_count": self.candle_count,
            "funding_rate_known": self.funding_rate_known,
            "mark_price_known": self.mark_price_known,
        }


@dataclass(frozen=True)
class ExecutabilityCheckItem:
    """One non-path-dependent hard constraint evaluated at ``knowledge_time``."""

    name: str
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ExecutabilityCheck:
    """Aggregate result of all non-path-dependent executability checks."""

    executable: bool
    rejection_reason: str | None
    checks: tuple[ExecutabilityCheckItem, ...]

    @property
    def failed_checks(self) -> tuple[ExecutabilityCheckItem, ...]:
        return tuple(item for item in self.checks if not item.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "rejection_reason": self.rejection_reason,
            "checks": [item.to_dict() for item in self.checks],
        }


@dataclass(frozen=True)
class PathDependentContext:
    """Portfolio state at decision time; recorded as context rather than used to drop samples."""

    position_open: bool = False
    open_positions_count: int = 0
    gross_exposure_usdt: Decimal = Decimal("0")
    daily_realized_pnl_usdt: Decimal = Decimal("0")
    daily_trades: int = 0
    consecutive_losses: int = 0
    available_balance_usdt: Decimal | None = None
    correlation_group: str | None = None
    group_has_exposure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_open": self.position_open,
            "open_positions_count": self.open_positions_count,
            "gross_exposure_usdt": _dec_str(self.gross_exposure_usdt),
            "daily_realized_pnl_usdt": _dec_str(self.daily_realized_pnl_usdt),
            "daily_trades": self.daily_trades,
            "consecutive_losses": self.consecutive_losses,
            "available_balance_usdt": _dec_str(self.available_balance_usdt),
            "correlation_group": self.correlation_group,
            "group_has_exposure": self.group_has_exposure,
        }


@dataclass(frozen=True)
class OpportunityPlan:
    """The trade geometry and per-outcome cost terms produced by ``TradingPolicy``."""

    entry_order_type: str
    market_entry_policy: str
    entry_reference_price: Decimal
    limit_price: Decimal | None
    limit_timeout_candles: int
    limit_fallback_to_market: bool
    stop_price: Decimal
    take_profit_price: Decimal
    exit_source: str
    dynamic_exit_used: bool
    max_holding_bars: int
    timeframe: str
    quantity: Decimal
    notional: Decimal
    reward_risk: Decimal | None = None
    breakeven_probability: Decimal | None = None
    r_win: Decimal | None = None
    r_loss: Decimal | None = None
    cost_take_bps: Decimal | None = None
    cost_stop_bps: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_order_type": self.entry_order_type,
            "market_entry_policy": self.market_entry_policy,
            "entry_reference_price": _dec_str(self.entry_reference_price),
            "limit_price": _dec_str(self.limit_price),
            "limit_timeout_candles": self.limit_timeout_candles,
            "limit_fallback_to_market": self.limit_fallback_to_market,
            "stop_price": _dec_str(self.stop_price),
            "take_profit_price": _dec_str(self.take_profit_price),
            "exit_source": self.exit_source,
            "dynamic_exit_used": self.dynamic_exit_used,
            "max_holding_bars": self.max_holding_bars,
            "timeframe": self.timeframe,
            "quantity": _dec_str(self.quantity),
            "notional": _dec_str(self.notional),
            "reward_risk": _dec_str(self.reward_risk),
            "breakeven_probability": _dec_str(self.breakeven_probability),
            "r_win": _dec_str(self.r_win),
            "r_loss": _dec_str(self.r_loss),
            "cost_take_bps": _dec_str(self.cost_take_bps),
            "cost_stop_bps": _dec_str(self.cost_stop_bps),
        }


@dataclass(frozen=True)
class TradeOpportunity:
    """A candidate trade tied to a knowledge timestamp, watermark and ``TradingPolicy``."""

    opportunity_id: str
    symbol: str
    direction: str
    action: str
    origin: str
    policy_variant: str
    policy_version: str
    knowledge_time: str
    knowledge_time_ms: int
    data_watermark: DataWatermark
    plan: OpportunityPlan
    executability: ExecutabilityCheck
    path_context: PathDependentContext
    trade_plan_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        return self.executability.executable

    @property
    def rejection_reason(self) -> str | None:
        return self.executability.rejection_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "action": self.action,
            "origin": self.origin,
            "policy_variant": self.policy_variant,
            "policy_version": self.policy_version,
            "knowledge_time": self.knowledge_time,
            "knowledge_time_ms": self.knowledge_time_ms,
            "executable": self.executable,
            "rejection_reason": self.rejection_reason,
            "data_watermark": self.data_watermark.to_dict(),
            "plan": self.plan.to_dict(),
            "executability": self.executability.to_dict(),
            "path_context": self.path_context.to_dict(),
            "trade_plan_id": self.trade_plan_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class NotExecutable(TradeOpportunity):
    """A candidate that failed one or more hard executability checks at ``knowledge_time``."""


@dataclass(frozen=True)
class OpportunityOutcome:
    """How ``TradingPolicy.outcome`` resolved a ``TradeOpportunity`` over subsequent candles."""

    opportunity_id: str
    policy_version: str
    outcome: str
    filled: bool
    fill_status: str
    entry_price: Decimal
    exit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    exit_reason: str
    entry_index: int
    exit_index: int
    wait_candles: int
    bars_held: int
    entry_maker: bool = False
    maker_exit: bool = False
    gap: bool = False
    ambiguous: bool = False
    ambiguity_resolution: str = ""
    refined: bool = False
    gross_r: Decimal = Decimal("0")
    cost_r: Decimal = Decimal("0")
    net_r: Decimal = Decimal("0")
    net_bps: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "policy_version": self.policy_version,
            "outcome": self.outcome,
            "filled": self.filled,
            "fill_status": self.fill_status,
            "entry_price": _dec_str(self.entry_price),
            "exit_price": _dec_str(self.exit_price),
            "stop_price": _dec_str(self.stop_price),
            "take_profit_price": _dec_str(self.take_profit_price),
            "exit_reason": self.exit_reason,
            "entry_index": self.entry_index,
            "exit_index": self.exit_index,
            "wait_candles": self.wait_candles,
            "bars_held": self.bars_held,
            "entry_maker": self.entry_maker,
            "maker_exit": self.maker_exit,
            "gap": self.gap,
            "ambiguous": self.ambiguous,
            "ambiguity_resolution": self.ambiguity_resolution,
            "refined": self.refined,
            "gross_r": _dec_str(self.gross_r),
            "cost_r": _dec_str(self.cost_r),
            "net_r": _dec_str(self.net_r),
            "net_bps": _dec_str(self.net_bps),
        }


def map_barrier_kind_to_outcome(kind: str) -> str:
    """Translate a ``domain.barriers`` outcome kind into an opportunity outcome label."""

    normalized = str(kind or "").upper().strip()
    if normalized in {TAKE_PROFIT, TP_FIRST}:
        return TP_FIRST
    if normalized in {STOP_LOSS, LIQUIDATION, SL_FIRST}:
        return SL_FIRST
    if normalized == TIMEOUT:
        return TIMEOUT
    if normalized == NOT_FILLED:
        return NOT_FILLED
    return OPEN


@dataclass(frozen=True)
class OutcomeDistribution:
    """Empirical fill probability ``p_fill`` and conditional barrier rates of a policy."""

    total_resolved: int
    filled_count: int
    not_filled_count: int
    tp_first_count: int
    sl_first_count: int
    timeout_count: int
    open_count: int
    p_fill: Decimal
    p_tp: Decimal
    p_sl: Decimal
    p_timeout: Decimal
    mean_r_timeout: Decimal
    realized_expectancy_r: Decimal
    realized_expectancy_filled_r: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_resolved": self.total_resolved,
            "filled_count": self.filled_count,
            "not_filled_count": self.not_filled_count,
            "tp_first_count": self.tp_first_count,
            "sl_first_count": self.sl_first_count,
            "timeout_count": self.timeout_count,
            "open_count": self.open_count,
            "p_fill": _dec_str(self.p_fill),
            "p_tp": _dec_str(self.p_tp),
            "p_sl": _dec_str(self.p_sl),
            "p_timeout": _dec_str(self.p_timeout),
            "mean_r_timeout": _dec_str(self.mean_r_timeout),
            "realized_expectancy_r": _dec_str(self.realized_expectancy_r),
            "realized_expectancy_filled_r": _dec_str(self.realized_expectancy_filled_r),
        }


def summarize_outcomes(outcomes: Sequence[OpportunityOutcome]) -> OutcomeDistribution:
    """Compute ``p_fill``, conditional ``(p_tp, p_sl, p_timeout)`` and ``R_timeout`` from outcomes."""

    open_count = sum(1 for item in outcomes if item.outcome == OPEN)
    resolved = [item for item in outcomes if item.outcome != OPEN]
    total_resolved = len(resolved)
    not_filled_count = sum(1 for item in resolved if item.outcome == NOT_FILLED or not item.filled)
    filled = [item for item in resolved if item.filled and item.outcome != NOT_FILLED]
    filled_count = len(filled)
    tp_count = sum(1 for item in filled if item.outcome == TP_FIRST)
    sl_count = sum(1 for item in filled if item.outcome == SL_FIRST)
    timeout_items = [item for item in filled if item.outcome == TIMEOUT]
    timeout_count = len(timeout_items)

    p_fill = Decimal(filled_count) / Decimal(total_resolved) if total_resolved > 0 else Decimal("0")
    if filled_count > 0:
        p_tp = Decimal(tp_count) / Decimal(filled_count)
        p_sl = Decimal(sl_count) / Decimal(filled_count)
        p_timeout = max(Decimal("0"), Decimal("1") - p_tp - p_sl)
        expectancy_filled = sum((item.net_r for item in filled), Decimal("0")) / Decimal(
            filled_count
        )
    else:
        p_tp = Decimal("0")
        p_sl = Decimal("0")
        p_timeout = Decimal("0")
        expectancy_filled = Decimal("0")

    mean_r_timeout = (
        sum((item.net_r for item in timeout_items), Decimal("0")) / Decimal(timeout_count)
        if timeout_count > 0
        else Decimal("0")
    )
    expectancy_all = (
        sum((item.net_r for item in resolved), Decimal("0")) / Decimal(total_resolved)
        if total_resolved > 0
        else Decimal("0")
    )
    return OutcomeDistribution(
        total_resolved=total_resolved,
        filled_count=filled_count,
        not_filled_count=not_filled_count,
        tp_first_count=tp_count,
        sl_first_count=sl_count,
        timeout_count=timeout_count,
        open_count=open_count,
        p_fill=p_fill,
        p_tp=p_tp,
        p_sl=p_sl,
        p_timeout=p_timeout,
        mean_r_timeout=mean_r_timeout,
        realized_expectancy_r=expectancy_all,
        realized_expectancy_filled_r=expectancy_filled,
    )


__all__ = [
    "DataWatermark",
    "ExecutabilityCheck",
    "ExecutabilityCheckItem",
    "LONG",
    "NOT_FILLED",
    "NotExecutable",
    "OPEN",
    "OpportunityOutcome",
    "OpportunityPlan",
    "OutcomeDistribution",
    "PathDependentContext",
    "SHORT",
    "SL_FIRST",
    "TIMEOUT",
    "TP_FIRST",
    "TradeOpportunity",
    "build_opportunity_id",
    "format_knowledge_time",
    "map_barrier_kind_to_outcome",
    "summarize_outcomes",
]
