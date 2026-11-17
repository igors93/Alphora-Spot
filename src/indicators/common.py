from collections import deque
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import TypeVar

T = TypeVar("T")


def to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as error:
        raise ValueError(f"Valor decimal invalido: {value}") from error


def has_min_length(values: Sequence, minimum: int) -> bool:
    if minimum < 0:
        raise ValueError("minimum nao pode ser negativo")
    return len(values) >= minimum


def tail_window(values: Iterable[T], window: int) -> list[T]:
    if window <= 0:
        raise ValueError("window precisa ser maior que zero")
    return list(deque(values, maxlen=window))
