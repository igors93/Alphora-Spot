"""Exchange trading rules of one symbol and the rounding they impose."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    price_tick: Decimal

    def round_quantity(self, quantity: Decimal) -> Decimal:
        return round_quantity_down(quantity, self.quantity_step)

    def round_quantity_up(self, quantity: Decimal) -> Decimal:
        return round_quantity_up(quantity, self.quantity_step)

    def round_price(self, price: Decimal) -> Decimal:
        return self.round_price_down(price)

    def round_price_down(self, price: Decimal) -> Decimal:
        return round_price_down(price, self.price_tick)

    def round_price_up(self, price: Decimal) -> Decimal:
        return round_price_up(price, self.price_tick)

    def minimum_executable_quantity(self, price: Decimal) -> Decimal:
        if price <= 0:
            raise ValueError("price precisa ser maior que zero")

        minimum_by_quantity = max(self.min_quantity, self.quantity_step)

        if self.min_notional <= 0:
            return minimum_by_quantity

        minimum_by_notional = self.round_quantity_up(self.min_notional / price)

        return max(
            minimum_by_quantity,
            minimum_by_notional,
        )

    def minimum_required_notional(self, price: Decimal) -> Decimal:
        return self.minimum_executable_quantity(price) * price

    def validate_quantity(self, quantity: Decimal, price: Decimal) -> None:
        if price <= 0:
            raise ValueError("Preço precisa ser maior que zero")

        if quantity <= 0:
            raise ValueError("Quantidade precisa ser maior que zero")

        rounded_quantity = self.round_quantity(quantity)
        if rounded_quantity != quantity:
            raise ValueError(f"Quantidade {quantity} não respeita o step {self.quantity_step}")

        if quantity < self.min_quantity:
            raise ValueError(
                f"Quantidade {quantity} menor que a mínima permitida {self.min_quantity}"
            )

        notional = quantity * price

        if self.min_notional > 0 and notional < self.min_notional:
            raise ValueError(
                f"Notional {notional} menor que o mínimo permitido {self.min_notional}. "
                f"Aumente ALPHORA_POSITION_NOTIONAL_USDT."
            )


def format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def round_quantity_down(value: Decimal, step: Decimal) -> Decimal:
    return _round_down_to_step(value, step)


def round_quantity_up(value: Decimal, step: Decimal) -> Decimal:
    return _round_up_to_step(value, step)


def round_price_down(value: Decimal, tick: Decimal) -> Decimal:
    return _round_down_to_step(value, tick)


def round_price_up(value: Decimal, tick: Decimal) -> Decimal:
    return _round_up_to_step(value, tick)


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step precisa ser maior que zero")

    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_up_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step precisa ser maior que zero")

    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step
