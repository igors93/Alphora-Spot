from __future__ import annotations

import logging
from typing import Any


class NullEventSink:
    non_blocking_sink = True

    def emit(self, event_type: str, **payload: Any) -> None:
        return None


class LoggingEventSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("market_data.snapshot_engine")

    def emit(self, event_type: str, **payload: Any) -> None:
        self.logger.info("snapshot engine event=%s payload=%s", event_type, payload)
