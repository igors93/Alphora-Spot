from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from futures.planning.quality_scorer import QualityDecision
from trading.context import TradeContext
from trading.enums import OPEN_ACTIONS, TradeAction, TradePlanStatus
from trading.intent import TradeIntent


@dataclass(frozen=True)
class SizedPosition:
    quantity: Decimal
    notional: Decimal
    size_multiplier: Decimal
    block_reason: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class PositionSizer:
    def size(
        self,
        *,
        intent: TradeIntent,
        context: TradeContext,
        quality: QualityDecision,
    ) -> SizedPosition:
        if intent.action == TradeAction.CLOSE_POSITION.value:
            quantity = context.position.quantity
            return SizedPosition(
                quantity=quantity,
                notional=quantity * context.market_price,
                size_multiplier=Decimal("1"),
                diagnostics={"mode": "close_current_position_quantity"},
            )

        if intent.action not in OPEN_ACTIONS:
            return SizedPosition(
                quantity=Decimal("0"),
                notional=Decimal("0"),
                size_multiplier=Decimal("0"),
                block_reason="NO_OPEN_ACTION",
            )

        if quality.status in {
            TradePlanStatus.BLOCK_SAFETY.value,
            TradePlanStatus.BLOCK_ECONOMIC.value,
        }:
            return SizedPosition(
                quantity=Decimal("0"),
                notional=Decimal("0"),
                size_multiplier=Decimal("0"),
                block_reason=quality.block_reason,
            )

        market_price = Decimal(str(context.market_price))
        if market_price <= 0:
            return SizedPosition(
                quantity=Decimal("0"),
                notional=Decimal("0"),
                size_multiplier=Decimal("0"),
                block_reason="MARKET_PRICE_UNAVAILABLE",
                diagnostics={"market_price": str(market_price)},
            )

        rules = context.exchange_rules
        base_notional = Decimal(str(context.symbol_config.position_notional_usdt))
        target_notional = min(base_notional, base_notional * quality.size_multiplier)
        min_notional = rules.min_notional
        if target_notional < min_notional:
            if base_notional < min_notional:
                return SizedPosition(
                    quantity=Decimal("0"),
                    notional=Decimal("0"),
                    size_multiplier=Decimal("0"),
                    block_reason="PROFILE_NOTIONAL_BELOW_MIN_NOTIONAL",
                    diagnostics={
                        "base_notional": str(base_notional),
                        "min_notional": str(min_notional),
                    },
                )
            target_notional = min_notional
            # Rounding down here would always land below min_notional; take the
            # smallest executable size instead, as long as the profile allows it.
            quantity = rules.minimum_executable_quantity(market_price)
            if quantity * market_price > base_notional:
                return SizedPosition(
                    quantity=Decimal("0"),
                    notional=Decimal("0"),
                    size_multiplier=Decimal("0"),
                    block_reason="PROFILE_NOTIONAL_BELOW_MIN_NOTIONAL",
                    diagnostics={
                        "base_notional": str(base_notional),
                        "min_notional": str(min_notional),
                        "minimum_executable_notional": str(quantity * market_price),
                    },
                )
        else:
            quantity = rules.round_quantity(target_notional / market_price)

        notional = quantity * market_price
        if quantity <= 0 or quantity < rules.min_quantity or notional < min_notional:
            return SizedPosition(
                quantity=Decimal("0"),
                notional=notional,
                size_multiplier=Decimal("0"),
                block_reason="SIZED_NOTIONAL_BELOW_MIN_NOTIONAL",
                diagnostics={"min_notional": str(min_notional)},
            )
        return SizedPosition(
            quantity=quantity,
            notional=notional,
            size_multiplier=quality.size_multiplier,
            diagnostics={
                "base_notional": str(base_notional),
                "target_notional": str(target_notional),
                "min_notional": str(min_notional),
            },
        )
