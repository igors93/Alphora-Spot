from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from backtesting.domain.config import BacktestSymbolConfig
from backtesting.infrastructure.candle_loader import Candle


@dataclass(frozen=True)
class BacktestSizingDiagnostic:
    symbol: str
    position_notional_usdt: Decimal
    reference_price: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    raw_quantity: Decimal
    rounded_quantity: Decimal
    effective_notional_after_rounding: Decimal
    minimum_required_notional: Decimal
    suggested_min_position_notional_usdt: Decimal
    tradable_with_config: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "position_notional_usdt": str(self.position_notional_usdt),
            "reference_price": str(self.reference_price),
            "quantity_step": str(self.quantity_step),
            "min_quantity": str(self.min_quantity),
            "min_notional": str(self.min_notional),
            "raw_quantity": str(self.raw_quantity),
            "rounded_quantity": str(self.rounded_quantity),
            "effective_notional_after_rounding": str(self.effective_notional_after_rounding),
            "minimum_required_notional": str(self.minimum_required_notional),
            "suggested_min_position_notional_usdt": str(self.suggested_min_position_notional_usdt),
            "tradable_with_config": self.tradable_with_config,
            "reason": self.reason,
        }


def build_sizing_diagnostics(
    symbol_configs: list[BacktestSymbolConfig],
    candles_by_symbol: dict[str, list[Candle]],
) -> dict[str, BacktestSizingDiagnostic]:
    return {
        config.symbol: evaluate_symbol_sizing(
            symbol_config=config,
            reference_price=_reference_price(candles_by_symbol.get(config.symbol, [])),
        )
        for config in symbol_configs
    }


def evaluate_symbol_sizing(
    *,
    symbol_config: BacktestSymbolConfig,
    reference_price: Decimal,
) -> BacktestSizingDiagnostic:
    rules = symbol_config.rules
    notional = Decimal(str(symbol_config.position_notional_usdt))
    if reference_price <= 0:
        return BacktestSizingDiagnostic(
            symbol=symbol_config.symbol,
            position_notional_usdt=notional,
            reference_price=reference_price,
            quantity_step=rules.quantity_step,
            min_quantity=rules.min_quantity,
            min_notional=rules.min_notional,
            raw_quantity=Decimal("0"),
            rounded_quantity=Decimal("0"),
            effective_notional_after_rounding=Decimal("0"),
            minimum_required_notional=Decimal("0"),
            suggested_min_position_notional_usdt=notional,
            tradable_with_config=False,
            reason="INVALID_REFERENCE_PRICE",
        )

    raw_quantity = notional / reference_price
    rounded_quantity = rules.round_quantity(raw_quantity)
    effective_notional = rounded_quantity * reference_price
    minimum_required_notional = rules.minimum_required_notional(reference_price)
    suggested_notional = _suggested_notional(minimum_required_notional, rules)

    tradable = True
    reason = "OK"
    if rounded_quantity <= 0:
        tradable = False
        reason = "POSITION_NOTIONAL_ROUNDS_TO_ZERO"
    else:
        try:
            rules.validate_quantity(rounded_quantity, reference_price)
        except ValueError as error:
            tradable = False
            reason = f"SYMBOL_RULES_NOT_SATISFIED: {error}"

    return BacktestSizingDiagnostic(
        symbol=symbol_config.symbol,
        position_notional_usdt=notional,
        reference_price=reference_price,
        quantity_step=rules.quantity_step,
        min_quantity=rules.min_quantity,
        min_notional=rules.min_notional,
        raw_quantity=raw_quantity,
        rounded_quantity=rounded_quantity,
        effective_notional_after_rounding=effective_notional,
        minimum_required_notional=minimum_required_notional,
        suggested_min_position_notional_usdt=suggested_notional,
        tradable_with_config=tradable,
        reason=reason,
    )


def sizing_diagnostics_as_dict(
    diagnostics: dict[str, BacktestSizingDiagnostic],
) -> dict[str, Any]:
    return {symbol: diagnostic.to_dict() for symbol, diagnostic in diagnostics.items()}


def untradable_symbols(
    diagnostics: dict[str, BacktestSizingDiagnostic],
) -> list[str]:
    return [
        symbol
        for symbol, diagnostic in sorted(diagnostics.items())
        if not diagnostic.tradable_with_config
    ]


def _reference_price(candles: list[Candle]) -> Decimal:
    for candle in candles:
        if candle.close > 0:
            return candle.close
    return Decimal("0")


def _suggested_notional(minimum_required_notional: Decimal, _rules) -> Decimal:
    return minimum_required_notional * Decimal("1.02")
