from decimal import Decimal, InvalidOperation
from typing import Any

BPS = Decimal("10000")


def decimal_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


def decimal_or_none_from_any(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
