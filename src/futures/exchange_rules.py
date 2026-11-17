from decimal import Decimal

from domain.symbol_rules import SymbolRules
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


__all__ = ["extract_symbol_rules"]
