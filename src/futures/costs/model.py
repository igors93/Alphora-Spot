import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from trading.context import TradeContext

if TYPE_CHECKING:
    from config import AppConfig

BPS = Decimal("10000")
COST_MODEL_VERSION = "1.1.0"
SPREAD_BPS_SEMANTICS = "FULL_BID_ASK_SPREAD"
SLIPPAGE_BPS_SEMANTICS = "PER_MARKET_FILL"


@dataclass(frozen=True)
class CostEstimate:
    entry_fee: Decimal
    exit_fee: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    funding_cost: Decimal
    round_trip_cost: Decimal
    minimum_profitable_move: Decimal
    diagnostics: dict[str, Any] = field(default_factory=dict)
    notional: Decimal = Decimal("0")
    round_trip_cost_bps: Decimal = Decimal("0")
    cost_model_version: str = COST_MODEL_VERSION


class CostModel:
    def __init__(
        self,
        *,
        taker_fee_pct: Decimal = Decimal("0.0004"),
        maker_fee_pct: Decimal = Decimal("0.0002"),
        estimated_spread_bps: Decimal = Decimal("2"),
        estimated_slippage_bps: Decimal = Decimal("3"),
        estimated_funding_rate: Decimal = Decimal("0"),
    ):
        self.taker_fee_pct = taker_fee_pct
        self.maker_fee_pct = maker_fee_pct
        self.estimated_spread_bps = estimated_spread_bps
        self.estimated_slippage_bps = estimated_slippage_bps
        self.estimated_funding_rate = estimated_funding_rate

    @classmethod
    def from_config(cls, config: "AppConfig") -> "CostModel":
        return cls(
            taker_fee_pct=Decimal(str(config.taker_fee_pct)),
            maker_fee_pct=Decimal(str(config.maker_fee_pct)),
            estimated_spread_bps=Decimal(str(config.estimated_spread_bps)),
            estimated_slippage_bps=Decimal(str(config.estimated_slippage_bps)),
            estimated_funding_rate=(
                Decimal(str(config.estimated_funding_rate))
                if config.funding_enabled
                else Decimal("0")
            ),
        )

    @classmethod
    def from_env(cls) -> "CostModel":
        funding_enabled = _env_bool("ALPHORA_FUNDING_ENABLED", True)
        return cls(
            taker_fee_pct=_env_decimal("ALPHORA_TAKER_FEE_PCT", "0.0004"),
            maker_fee_pct=_env_decimal("ALPHORA_MAKER_FEE_PCT", "0.0002"),
            estimated_spread_bps=_env_decimal_alias(
                "ALPHORA_ESTIMATED_SPREAD_BPS",
                "ALPHORA_SPREAD_BPS",
                "2",
            ),
            estimated_slippage_bps=_env_decimal_alias(
                "ALPHORA_ESTIMATED_SLIPPAGE_BPS",
                "ALPHORA_SLIPPAGE_BPS",
                "3",
            ),
            estimated_funding_rate=(
                _env_decimal_alias(
                    "ALPHORA_ESTIMATED_FUNDING_RATE",
                    "ALPHORA_DEFAULT_FUNDING_RATE",
                    "0",
                )
                if funding_enabled
                else Decimal("0")
            ),
        )

    def estimate(
        self,
        *,
        context: TradeContext,
        quantity: Decimal,
        execution_style: str = "MARKET",
    ) -> CostEstimate:
        notional = abs(quantity * context.market_price)
        normalized_execution_style = str(execution_style or "MARKET").upper().strip()
        market_entry = normalized_execution_style == "MARKET"
        entry_fee_rate = self.taker_fee_pct if market_entry else self.maker_fee_pct
        entry_fee = notional * entry_fee_rate
        exit_fee = notional * self.taker_fee_pct
        spread_bps = self._spread_bps(context)
        spread_source = "market_context" if context.spread_pct is not None else "estimated_config"
        # ``spread_bps`` is the full bid/ask width. A taker fill crosses half
        # that width relative to the mid price. Slippage is configured per
        # market fill. The planned exit is conservatively assumed to be taker.
        market_fill_count = 2 if market_entry else 1
        spread_cost = notional * spread_bps / BPS * Decimal(str(market_fill_count)) / Decimal("2")
        slippage_cost = (
            notional * self.estimated_slippage_bps / BPS * Decimal(str(market_fill_count))
        )
        funding_rate = context.funding_rate
        funding_source = "market_context" if funding_rate is not None else "estimated_config"
        if funding_rate is None:
            funding_rate = self.estimated_funding_rate
        funding_cost = notional * abs(funding_rate)
        round_trip = entry_fee + exit_fee + spread_cost + slippage_cost + funding_cost
        minimum_move = round_trip / quantity if quantity > 0 else Decimal("0")
        round_trip_bps = _cost_bps(round_trip, notional)
        minimum_move_bps = (
            minimum_move / abs(context.market_price) * BPS
            if quantity > 0 and context.market_price != 0
            else Decimal("0")
        )

        diagnostics = {
            "cost_model_version": COST_MODEL_VERSION,
            "execution_style": execution_style,
            "market_fill_count": market_fill_count,
            "notional": str(notional),
            "round_trip_cost": str(round_trip),
            "round_trip_cost_bps": str(round_trip_bps),
            "minimum_profitable_move": str(minimum_move),
            "minimum_profitable_move_bps": str(minimum_move_bps),
            "components": {
                "entry_fee": str(entry_fee),
                "exit_fee": str(exit_fee),
                "spread_cost": str(spread_cost),
                "slippage_cost": str(slippage_cost),
                "funding_cost": str(funding_cost),
            },
            "component_bps": {
                "entry_fee_bps": str(_cost_bps(entry_fee, notional)),
                "exit_fee_bps": str(_cost_bps(exit_fee, notional)),
                "spread_cost_bps": str(_cost_bps(spread_cost, notional)),
                "slippage_cost_bps": str(_cost_bps(slippage_cost, notional)),
                "funding_cost_bps": str(_cost_bps(funding_cost, notional)),
            },
            "rates": {
                "entry_fee_rate": str(entry_fee_rate),
                "exit_fee_rate": str(self.taker_fee_pct),
                "maker_fee_rate": str(self.maker_fee_pct),
                "spread_bps": str(spread_bps),
                "slippage_bps": str(self.estimated_slippage_bps),
                "funding_rate": str(funding_rate),
            },
            "sources": {
                "spread": spread_source,
                "funding": funding_source,
            },
            "semantics": {
                "spread_bps": SPREAD_BPS_SEMANTICS,
                "slippage_bps": SLIPPAGE_BPS_SEMANTICS,
                "exit_style_assumption": "MARKET_TAKER",
            },
            # Kept for backward compatibility with earlier diagnostics.
            "entry_fee_rate": str(entry_fee_rate),
            "exit_fee_rate": str(self.taker_fee_pct),
            "spread_bps": str(spread_bps),
            "slippage_bps": str(self.estimated_slippage_bps),
            "funding_rate": str(funding_rate),
        }

        return CostEstimate(
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            funding_cost=funding_cost,
            round_trip_cost=round_trip,
            minimum_profitable_move=minimum_move,
            diagnostics=diagnostics,
            notional=notional,
            round_trip_cost_bps=round_trip_bps,
            cost_model_version=COST_MODEL_VERSION,
        )

    def _spread_bps(self, context: TradeContext) -> Decimal:
        if context.spread_pct is not None:
            return abs(context.spread_pct) * BPS
        return self.estimated_spread_bps


def _cost_bps(cost: Decimal, notional: Decimal) -> Decimal:
    if notional <= 0:
        return Decimal("0")
    return cost / notional * BPS


def _env_decimal(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except Exception:
        return Decimal(default)


def _env_decimal_alias(primary_name: str, legacy_name: str, default: str) -> Decimal:
    raw_value = os.getenv(primary_name)
    if raw_value is None or not raw_value.strip():
        raw_value = os.getenv(legacy_name)

    if raw_value is None or not raw_value.strip():
        raw_value = default

    try:
        return Decimal(raw_value)
    except Exception:
        return Decimal(default)


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "sim"}:
        return True

    if normalized in {"false", "0", "no", "n", "nao", "não"}:
        return False

    return default
