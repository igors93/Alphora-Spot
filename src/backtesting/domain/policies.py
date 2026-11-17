from enum import Enum


class SignalTimingPolicy(str, Enum):
    ON_CLOSE = "ON_CLOSE"
    ON_OPEN = "ON_OPEN"


class MarketEntryPolicy(str, Enum):
    SIGNAL_CLOSE = "SIGNAL_CLOSE"
    NEXT_OPEN = "NEXT_OPEN"


class LimitActivationPolicy(str, Enum):
    NEXT_CANDLE = "NEXT_CANDLE"


class EndOfDataPolicy(str, Enum):
    CLOSE_AT_LAST_PRICE = "CLOSE_AT_LAST_PRICE"
    MARK_TO_MARKET = "MARK_TO_MARKET"
    FAIL_IF_OPEN = "FAIL_IF_OPEN"


class SameCandleExitPolicy(str, Enum):
    CONSERVATIVE_STOP_FIRST = "CONSERVATIVE_STOP_FIRST"
    OPTIMISTIC_TAKE_FIRST = "OPTIMISTIC_TAKE_FIRST"
    NEAREST_TO_OPEN = "NEAREST_TO_OPEN"
    REJECT_AMBIGUOUS = "REJECT_AMBIGUOUS"


def normalize_policy(enum_type, value):
    if isinstance(value, enum_type):
        return value
    normalized = str(value or "").upper().strip()
    try:
        return enum_type(normalized)
    except ValueError:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"Politica invalida: {normalized}. Valores aceitos: {allowed}.") from None
