from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from domain.models import PositionDirection


@dataclass(frozen=True)
class TradeResult:
    symbol: str
    direction: PositionDirection
    entry_time: str
    exit_time: str
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    notional: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage_cost: Decimal
    spread_cost: Decimal
    funding_cost: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    exit_reason: str
    strategy_reason_entry: str
    strategy_reason_exit: str
    dynamic_exit_used: bool = False
    exit_source: str = "GLOBAL_FALLBACK"
    recommended_stop_price: Decimal | None = None
    recommended_take_profit_price: Decimal | None = None
    actual_stop_price_used: Decimal | None = None
    actual_take_profit_price_used: Decimal | None = None
    reward_risk_liquido: Decimal | None = None
    raw_reward_risk: Decimal | None = None
    cost_to_reward_ratio: Decimal | None = None
    risk_pct: Decimal | None = None
    reward_pct: Decimal | None = None
    planned_exit_rejection_reason: str | None = None
    planned_exit_diagnostics: dict[str, Any] | None = None
    smart_sizing_mode: str = "OFF"
    smart_sizing_used: bool = False
    smart_sizing_affected_result: bool = False
    base_notional_usdt: Decimal | None = None
    final_notional_usdt: Decimal | None = None
    sizing_multiplier: Decimal | None = None
    sizing_reason: str | None = None
    sizing_diagnostics: dict[str, Any] | None = None
    smart_execution_mode: str = "OFF"
    smart_execution_used: bool = False
    smart_execution_affected_result: bool = False
    entry_order_type: str = "MARKET"
    limit_price: Decimal | None = None
    limit_fill_status: str | None = None
    limit_wait_candles: int = 0
    fallback_to_market_used: bool = False
    smart_execution_reason: str | None = None
    smart_execution_diagnostics: dict[str, Any] | None = None
    entry_maker: bool = False
    exit_maker: bool = False
    requested_quantity: Decimal | None = None
    filled_quantity: Decimal | None = None
    entry_volume_participation: Decimal | None = None
    exit_volume_participation: Decimal | None = None
    market_impact_bps: Decimal = Decimal("0")
    liquidation_price: Decimal | None = None
    liquidation_fee: Decimal = Decimal("0")
    was_liquidated: bool = False
    effective_entry_price: Decimal | None = None
    effective_exit_price: Decimal | None = None
