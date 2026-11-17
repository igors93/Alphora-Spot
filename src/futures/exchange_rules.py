from decimal import Decimal
from math import lcm

from domain.symbol_rules import SymbolRules, round_quantity_down
from utils.events import EventChannel, observe_event


def extract_symbol_rules(exchange_info: dict, symbol: str) -> SymbolRules:
    symbol_data = None

    for item in exchange_info.get("symbols", []):
        if item.get("symbol") == symbol:
            symbol_data = item
            break

    if symbol_data is None:
        raise ValueError(f"Símbolo {symbol} não encontrado em exchangeInfo")

    filters = {item["filterType"]: item for item in symbol_data.get("filters", [])}

    market_lot_filter = filters.get("MARKET_LOT_SIZE")
    base_lot_filter = filters.get("LOT_SIZE")
    lot_filter = market_lot_filter or base_lot_filter
    if (
        market_lot_filter
        and base_lot_filter
        and (
            Decimal(str(market_lot_filter.get("stepSize", "0") or "0")) <= 0
            or Decimal(str(market_lot_filter.get("minQty", "0") or "0")) <= 0
        )
    ):
        lot_filter = base_lot_filter

    price_filter = filters.get("PRICE_FILTER")
    min_notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")

    if not lot_filter:
        raise ValueError(f"{symbol} não possui filtro MARKET_LOT_SIZE/LOT_SIZE")

    if not price_filter:
        raise ValueError(f"{symbol} não possui filtro PRICE_FILTER")

    quantity_step = Decimal(lot_filter["stepSize"])
    min_quantity = Decimal(lot_filter["minQty"])
    price_tick = Decimal(price_filter["tickSize"])

    min_notional = Decimal("0")

    if min_notional_filter:
        min_notional = Decimal(
            min_notional_filter.get("notional") or min_notional_filter.get("minNotional") or "0"
        )

    rules = SymbolRules(
        symbol=symbol,
        quantity_step=quantity_step,
        min_quantity=min_quantity,
        min_notional=min_notional,
        price_tick=price_tick,
    )
    observe_event(
        "exchange_rule_loaded",
        component=__name__,
        category="exchange",
        channel=EventChannel.AUDIT,
        symbol=symbol,
        quantity_step=quantity_step,
        min_quantity=min_quantity,
        min_notional=min_notional,
        price_tick=price_tick,
    )
    return rules


def spot_symbol_filters(exchange_info: dict, symbol: str) -> dict[str, dict]:
    for item in exchange_info.get("symbols", []):
        if item.get("symbol") == symbol:
            if item.get("status") not in {None, "TRADING"}:
                raise ValueError(f"{symbol} nao esta em TRADING")
            return {
                entry["filterType"]: entry for entry in item.get("filters", [])
                if isinstance(entry, dict) and "filterType" in entry
            }
    raise ValueError(f"{symbol} nao encontrado em exchangeInfo Spot")


def round_spot_market_quantity(quantity: Decimal, filters: dict[str, dict]) -> Decimal:
    steps = [
        Decimal(str(filters[name].get("stepSize", "0")))
        for name in ("LOT_SIZE", "MARKET_LOT_SIZE") if name in filters
    ]
    steps = [step for step in steps if step > 0]
    if not steps:
        raise ValueError("Spot sem LOT_SIZE/MARKET_LOT_SIZE valido")
    scale = max(-step.as_tuple().exponent for step in steps)
    integers = [int(step * (10 ** scale)) for step in steps]
    common_step = Decimal(lcm(*integers)) / (10 ** scale)
    return round_quantity_down(quantity, common_step)


def validate_spot_order_filters(
    *, filters: dict[str, dict], quantity: Decimal, price: Decimal,
    side: str, market_order: bool, reference_price: Decimal,
) -> None:
    if quantity <= 0 or price <= 0:
        raise ValueError("Quantidade e preco Spot devem ser positivos")
    for name in ("LOT_SIZE", "MARKET_LOT_SIZE"):
        if name == "MARKET_LOT_SIZE" and not market_order:
            continue
        lot = filters.get(name)
        if not lot:
            continue
        step = Decimal(str(lot.get("stepSize", "0")))
        minimum = Decimal(str(lot.get("minQty", "0")))
        maximum = Decimal(str(lot.get("maxQty", "0")))
        if step > 0 and round_quantity_down(quantity, step) != quantity:
            raise ValueError(f"{name}: quantidade fora do step")
        if minimum > 0 and quantity < minimum:
            raise ValueError(f"{name}: quantidade abaixo do minimo")
        if maximum > 0 and quantity > maximum:
            raise ValueError(f"{name}: quantidade acima do maximo")

    price_filter = filters.get("PRICE_FILTER", {})
    if not market_order:
        tick = Decimal(str(price_filter.get("tickSize", "0")))
        minimum = Decimal(str(price_filter.get("minPrice", "0")))
        maximum = Decimal(str(price_filter.get("maxPrice", "0")))
        if tick > 0 and round_quantity_down(price, tick) != price:
            raise ValueError("PRICE_FILTER: preco fora do tick")
        if minimum > 0 and price < minimum:
            raise ValueError("PRICE_FILTER: preco abaixo do minimo")
        if maximum > 0 and price > maximum:
            raise ValueError("PRICE_FILTER: preco acima do maximo")

    notional = quantity * price
    minimum_filter = filters.get("MIN_NOTIONAL", {})
    if not market_order or minimum_filter.get("applyToMarket", True):
        minimum = Decimal(str(minimum_filter.get("minNotional") or minimum_filter.get("notional") or "0"))
        if minimum > 0 and notional < minimum:
            raise ValueError("MIN_NOTIONAL: ordem abaixo do minimo")
    notional_filter = filters.get("NOTIONAL", {})
    minimum = Decimal(str(notional_filter.get("minNotional", "0")))
    maximum = Decimal(str(notional_filter.get("maxNotional", "0")))
    if (not market_order or notional_filter.get("applyMinToMarket", True)) and minimum > 0 and notional < minimum:
        raise ValueError("NOTIONAL: ordem abaixo do minimo")
    if (not market_order or notional_filter.get("applyMaxToMarket", True)) and maximum > 0 and notional > maximum:
        raise ValueError("NOTIONAL: ordem acima do maximo")

    if not market_order and reference_price > 0:
        percent = filters.get("PERCENT_PRICE", {})
        by_side = filters.get("PERCENT_PRICE_BY_SIDE", {})
        lower = by_side.get("bidMultiplierDown" if side == "BUY" else "askMultiplierDown") or percent.get("multiplierDown")
        upper = by_side.get("bidMultiplierUp" if side == "BUY" else "askMultiplierUp") or percent.get("multiplierUp")
        if lower is not None and price < reference_price * Decimal(str(lower)):
            raise ValueError("PERCENT_PRICE: preco abaixo do intervalo permitido")
        if upper is not None and price > reference_price * Decimal(str(upper)):
            raise ValueError("PERCENT_PRICE: preco acima do intervalo permitido")


__all__ = [
    "extract_symbol_rules", "spot_symbol_filters", "round_spot_market_quantity",
    "validate_spot_order_filters",
]
