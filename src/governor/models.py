from dataclasses import dataclass, field
from enum import Enum
from time import time
from typing import Any

from scanner.signal_score import SignalPriority as SymbolPriority

__all__ = [
    "GovernorDecision",
    "PortfolioSnapshot",
    "SymbolPriority",
    "SymbolRuntimeSnapshot",
    "SymbolRuntimeStatus",
]


class SymbolRuntimeStatus(Enum):
    DISABLED = "DISABLED"
    WARMUP = "WARMUP"
    COLD = "COLD"
    WARM = "WARM"
    MONITORING = "MONITORING"
    HOT_CHECK = "HOT_CHECK"
    READY = "READY"
    IN_POSITION = "IN_POSITION"
    ENTRY_PENDING = "ENTRY_PENDING"
    POSITION_OPEN_UNPROTECTED = "POSITION_OPEN_UNPROTECTED"
    POSITION_OPEN_PROTECTED = "POSITION_OPEN_PROTECTED"
    PROTECTIONS_PENDING = "PROTECTIONS_PENDING"
    CLOSE_PENDING = "CLOSE_PENDING"
    WAITING_FLAT_CONFIRMATION = "WAITING_FLAT_CONFIRMATION"
    FLAT_CONFIRMED = "FLAT_CONFIRMED"
    STOPPING_SAFE = "STOPPING_SAFE"
    STOPPED_WITH_PROTECTION = "STOPPED_WITH_PROTECTION"
    STOPPED_FLAT = "STOPPED_FLAT"
    STOP_BLOCKED_UNPROTECTED = "STOP_BLOCKED_UNPROTECTED"
    MANUAL_RECONCILE_REQUIRED = "MANUAL_RECONCILE_REQUIRED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class GovernorDecision:
    allowed: bool
    symbol: str
    action: str
    reason: str
    checks: dict[str, Any] = field(default_factory=dict)
    risk_snapshot: dict[str, Any] = field(default_factory=dict)
    portfolio_snapshot: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time)


@dataclass(frozen=True)
class SymbolRuntimeSnapshot:
    symbol: str
    enabled: bool
    status: SymbolRuntimeStatus
    score: int = 0
    priority: SymbolPriority = SymbolPriority.COLD
    last_price: str = "0"
    last_checked_at: float = 0.0
    next_check_at: float = 0.0
    data_age_seconds: float = 0.0
    stale: bool = False
    position_direction: str = "NONE"
    position_quantity: str = "0"
    entry_price: str = "0"
    unrealized_pnl: str = "0"
    operational_state: str = ""
    protection_warning: str = ""
    strategy: str = ""
    reason: str = ""
    error: str = ""


@dataclass(frozen=True)
class PortfolioSnapshot:
    total_open_positions: int = 0
    max_open_positions: int = 0
    total_exposure_usdt: str = "0"
    max_total_exposure_usdt: str = "0"
    exposure_by_symbol: dict[str, str] = field(default_factory=dict)
    exposure_by_direction: dict[str, str] = field(default_factory=dict)
    exposure_by_group: dict[str, str] = field(default_factory=dict)
    daily_realized_pnl: str = "0"
    daily_trades: int = 0
    consecutive_losses: int = 0
    blocked_symbols: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time)
