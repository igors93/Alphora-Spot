from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from utils.privacy import sanitize_for_audit, to_safe_fallback


class EventImportance(str, Enum):
    CRITICAL = "CRITICAL"
    TELEMETRY = "TELEMETRY"


@dataclass(frozen=True)
class EventWriteResult:
    success: bool
    failed_destinations: tuple[str, ...] = ()
    reason: str = "OK"


class EventSink:
    """Central event fan-out for governor pipeline events.

    Critical events use the safe append path; telemetry failures are logged and
    do not block protection or close flows.
    """

    def __init__(self, *, destinations: tuple[Any, ...], logger: Any = None):
        # ``is not None``: a journal that defines ``__len__`` is falsy while empty, and
        # dropping it would silently lose critical pipeline events.
        self.destinations = tuple(
            destination for destination in destinations if destination is not None
        )
        self.logger = logger

    def emit(
        self,
        event_type: str,
        *,
        critical: bool = True,
        importance: EventImportance | None = None,
        **payload: Any,
    ) -> EventWriteResult:
        resolved_importance = importance or (
            EventImportance.CRITICAL if critical else EventImportance.TELEMETRY
        )
        sanitized = sanitize_for_audit(_make_jsonable(payload))
        failures: list[str] = []
        for destination in self.destinations:
            try:
                destination.append(event_type, **sanitized)
            except Exception as error:
                destination_name = type(destination).__name__
                failures.append(destination_name)
                if self.logger is not None:
                    log = (
                        self.logger.error
                        if resolved_importance is EventImportance.CRITICAL
                        else self.logger.warning
                    )
                    log(
                        "Falha ao emitir evento do pipeline | event_type=%s | "
                        "importance=%s | destination=%s | error=%s",
                        event_type,
                        resolved_importance.value,
                        destination_name,
                        error,
                    )
        if failures:
            return EventWriteResult(
                success=False,
                failed_destinations=tuple(failures),
                reason="EVENT_WRITE_FAILED",
            )
        return EventWriteResult(success=True)


def _make_jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_json_key(key): _make_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_make_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_make_jsonable(item) for item in value), key=str)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _make_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return to_safe_fallback(value)


def _json_key(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    return str(to_safe_fallback(value))
