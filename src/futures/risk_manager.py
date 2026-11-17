from dataclasses import dataclass
from decimal import Decimal

from domain.models import TradeAction
from domain.symbol_rules import SymbolRules
from utils.events import EventChannel, observe_event


@dataclass(frozen=True)
class QuantityCalculation:
    symbol: str
    price: Decimal
    requested_notional: Decimal
    raw_quantity: Decimal
    rounded_quantity: Decimal
    actual_notional: Decimal
    minimum_required_notional: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal


class TradeNotExecutableError(ValueError):
    def __init__(self, reason: str, calculation: QuantityCalculation):
        self.reason = reason
        self.calculation = calculation
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        c = self.calculation
        return (
            f"{self.reason} | "
            f"symbol={c.symbol} | "
            f"price={c.price} | "
            f"requested_notional={c.requested_notional} | "
            f"raw_quantity={c.raw_quantity} | "
            f"rounded_quantity={c.rounded_quantity} | "
            f"actual_notional={c.actual_notional} | "
            f"minimum_required_notional≈{c.minimum_required_notional} | "
            f"quantity_step={c.quantity_step} | "
            f"min_quantity={c.min_quantity} | "
            f"min_notional={c.min_notional}"
        )


class FuturesRiskManager:
    def __init__(
        self,
        position_notional_usdt: float,
        stop_loss_pct: float,
        take_profit_pct: float,
    ):
        self.position_notional_usdt = Decimal(str(position_notional_usdt))
        self.stop_loss_pct = Decimal(str(stop_loss_pct))
        self.take_profit_pct = Decimal(str(take_profit_pct))

        if self.position_notional_usdt <= 0:
            raise ValueError("position_notional_usdt precisa ser maior que zero")

        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct precisa ser maior que zero")

        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct precisa ser maior que zero")

    def calculate_quantity(self, price: Decimal, rules: SymbolRules) -> Decimal:
        if price <= 0:
            raise ValueError("price precisa ser maior que zero")

        raw_quantity = self.position_notional_usdt / price
        quantity = rules.round_quantity(raw_quantity)

        calculation = QuantityCalculation(
            symbol=rules.symbol,
            price=price,
            requested_notional=self.position_notional_usdt,
            raw_quantity=raw_quantity,
            rounded_quantity=quantity,
            actual_notional=quantity * price,
            minimum_required_notional=rules.minimum_required_notional(price),
            quantity_step=rules.quantity_step,
            min_quantity=rules.min_quantity,
            min_notional=rules.min_notional,
        )
        if quantity != raw_quantity:
            observe_event(
                "quantity_rounded",
                component=__name__,
                category="exchange",
                channel=EventChannel.AUDIT,
                symbol=rules.symbol,
                raw_quantity=raw_quantity,
                rounded_quantity=quantity,
                quantity_step=rules.quantity_step,
                price=price,
            )

        if quantity <= 0:
            observe_event(
                "notional_too_small",
                component=__name__,
                category="exchange",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                symbol=rules.symbol,
                calculation=calculation,
                reason="rounded_quantity_zero",
            )
            raise TradeNotExecutableError(
                "Position notional pequeno demais: arredondamento gerou quantidade zero",
                calculation,
            )

        try:
            rules.validate_quantity(quantity=quantity, price=price)
        except ValueError as error:
            observe_event(
                "exchange_rule_validation_failed",
                component=__name__,
                category="exchange",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                symbol=rules.symbol,
                calculation=calculation,
                error=error,
            )
            raise TradeNotExecutableError(
                f"Trade inviável pelas regras do símbolo: {error}",
                calculation,
            ) from error

        return quantity

    def normalize_planned_quantity(
        self,
        planned_quantity: Decimal,
        price: Decimal,
        rules: SymbolRules,
    ) -> Decimal:
        if price <= 0:
            raise ValueError("price precisa ser maior que zero")

        raw_quantity = Decimal(str(planned_quantity))
        quantity = rules.round_quantity(raw_quantity)

        calculation = QuantityCalculation(
            symbol=rules.symbol,
            price=price,
            requested_notional=raw_quantity * price,
            raw_quantity=raw_quantity,
            rounded_quantity=quantity,
            actual_notional=quantity * price,
            minimum_required_notional=rules.minimum_required_notional(price),
            quantity_step=rules.quantity_step,
            min_quantity=rules.min_quantity,
            min_notional=rules.min_notional,
        )
        if quantity != raw_quantity:
            observe_event(
                "quantity_rounded",
                component=__name__,
                category="exchange",
                channel=EventChannel.AUDIT,
                symbol=rules.symbol,
                raw_quantity=raw_quantity,
                rounded_quantity=quantity,
                quantity_step=rules.quantity_step,
                price=price,
                source="planned_quantity",
            )

        if quantity <= 0:
            observe_event(
                "notional_too_small",
                component=__name__,
                category="exchange",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                symbol=rules.symbol,
                calculation=calculation,
                reason="planned_quantity_rounded_zero",
            )
            raise TradeNotExecutableError(
                "Planned quantity invalida: arredondamento gerou quantidade zero",
                calculation,
            )

        try:
            rules.validate_quantity(quantity=quantity, price=price)
        except ValueError as error:
            observe_event(
                "exchange_rule_validation_failed",
                component=__name__,
                category="exchange",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                symbol=rules.symbol,
                calculation=calculation,
                error=error,
                source="planned_quantity",
            )
            raise TradeNotExecutableError(
                f"Planned quantity inviavel pelas regras do simbolo: {error}",
                calculation,
            ) from error

        return quantity

    def calculate_stop_price(
        self,
        action: TradeAction,
        entry_price: Decimal,
        rules: SymbolRules,
    ) -> Decimal:
        if action == TradeAction.OPEN_LONG:
            price = entry_price * (Decimal("1") - self.stop_loss_pct)
            return rules.round_price_down(price)

        if action == TradeAction.OPEN_SHORT:
            price = entry_price * (Decimal("1") + self.stop_loss_pct)
            return rules.round_price_up(price)

        raise ValueError(f"Ação inválida para stop price: {action}")

    def calculate_take_profit_price(
        self,
        action: TradeAction,
        entry_price: Decimal,
        rules: SymbolRules,
    ) -> Decimal:
        if action == TradeAction.OPEN_LONG:
            price = entry_price * (Decimal("1") + self.take_profit_pct)
            return rules.round_price_up(price)

        if action == TradeAction.OPEN_SHORT:
            price = entry_price * (Decimal("1") - self.take_profit_pct)
            return rules.round_price_down(price)

        raise ValueError(f"Ação inválida para take profit price: {action}")
