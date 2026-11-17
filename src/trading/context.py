from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from config.symbol_context import SymbolTradingConfig
from domain.models import FuturesPosition
from domain.symbol_rules import SymbolRules


@dataclass(frozen=True)
class TradeContext:
    symbol: str
    market_price: Decimal
    mark_price: Decimal | None
    index_price: Decimal | None
    spread_pct: Decimal | None
    volatility: Decimal | None
    atr: Decimal | None
    funding_rate: Decimal | None
    next_funding_time: int | None
    position: FuturesPosition
    exchange_rules: SymbolRules
    symbol_config: SymbolTradingConfig
    portfolio_snapshot: dict[str, Any]
    data_age_seconds: float
    stale: bool
    candles: tuple[Any, ...] = field(default_factory=tuple)
