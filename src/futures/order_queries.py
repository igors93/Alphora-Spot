from dataclasses import dataclass


@dataclass(frozen=True)
class OpenOrdersResult:
    ok: bool
    orders: list[dict]
    error: str | None = None

    @classmethod
    def success(cls, orders: list[dict]) -> "OpenOrdersResult":
        return cls(ok=True, orders=orders, error=None)

    @classmethod
    def failure(cls, error: Exception) -> "OpenOrdersResult":
        return cls(ok=False, orders=[], error=str(error))
