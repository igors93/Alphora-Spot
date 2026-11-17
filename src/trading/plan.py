from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

TRADE_PLAN_VERSION = "1.0.0"
TRADE_PLAN_SOURCE_SHADOW = "SHADOW"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_plan_id() -> str:
    return f"tp_{uuid4().hex}"


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    action: str
    status: str
    strategy_name: str
    signal_confidence: int
    quality_score: int
    entry_reference_price: Decimal
    quantity: Decimal
    notional: Decimal
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    estimated_entry_fee: Decimal
    estimated_exit_fee: Decimal
    estimated_spread_cost: Decimal
    estimated_slippage_cost: Decimal
    estimated_funding_cost: Decimal
    estimated_round_trip_cost: Decimal
    expected_net_gain: Decimal | None
    expected_net_loss: Decimal | None
    reward_risk: Decimal | None
    size_multiplier: Decimal
    execution_style: str
    management_style: str
    reason: str
    block_reason: str | None
    plan_id: str = field(default_factory=_new_plan_id)
    plan_version: str = TRADE_PLAN_VERSION
    source: str = TRADE_PLAN_SOURCE_SHADOW
    created_at: str = field(default_factory=_utc_now_iso)
    diagnostics: dict[str, Any] = field(default_factory=dict)
