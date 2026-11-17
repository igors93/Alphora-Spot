from __future__ import annotations

from typing import Any


class BridgeError(Exception):
    """User-facing failure: the interface shows ``title`` and ``message`` verbatim."""

    def __init__(self, title: str, message: str) -> None:
        super().__init__(message)
        self.title = str(title)
        self.message = str(message)

    def as_payload(self) -> dict[str, Any]:
        return {"title": self.title, "message": self.message}
