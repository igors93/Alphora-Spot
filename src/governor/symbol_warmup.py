from dataclasses import dataclass, field
from typing import Any

OPEN_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}


@dataclass(frozen=True)
class SymbolWarmupResult:
    symbol: str
    action: str
    complete: bool
    blocks_new_entry: bool
    reason: str
    observed_prices: int
    min_observed_prices: int
    data_age_seconds: float
    max_data_age_seconds: float
    stale: bool
    position_open: bool
    mode: str = "PROTECTIVE"
    exchange_rules_available: bool | None = None
    mark_price_available: bool | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "complete": self.complete,
            "blocks_new_entry": self.blocks_new_entry,
            "reason": self.reason,
            "observed_prices": self.observed_prices,
            "min_observed_prices": self.min_observed_prices,
            "data_age_seconds": self.data_age_seconds,
            "max_data_age_seconds": self.max_data_age_seconds,
            "stale": self.stale,
            "position_open": self.position_open,
            "mode": self.mode,
            "exchange_rules_available": self.exchange_rules_available,
            "mark_price_available": self.mark_price_available,
            "diagnostics": dict(self.diagnostics),
        }


class SymbolWarmupEvaluator:
    def __init__(
        self,
        *,
        min_observed_prices: int = 20,
        max_data_age_seconds: float = 30.0,
        mode: str = "PROTECTIVE",
    ):
        self.min_observed_prices = max(1, int(min_observed_prices or 20))
        self.max_data_age_seconds = max(0.0, float(max_data_age_seconds or 30.0))
        self.mode = str(mode or "PROTECTIVE").upper().strip()

    def evaluate(
        self,
        *,
        symbol: str,
        snapshot,
        action: str | None,
        position_open: bool = False,
        min_observed_prices: int | None = None,
        heavy_snapshot=None,
    ) -> SymbolWarmupResult:
        normalized_symbol = str(symbol or "").upper().strip()
        normalized_action = str(action or "").upper().strip()
        required_prices = max(
            1,
            int(
                min_observed_prices if min_observed_prices is not None else self.min_observed_prices
            ),
        )
        market = getattr(snapshot, "market", None)
        prices = list(getattr(market, "prices", []) or [])
        observed_prices = len(prices)
        data_age_seconds = max(0.0, float(getattr(snapshot, "data_age_seconds", 0.0) or 0.0))
        stale = bool(getattr(snapshot, "stale", False))

        exchange_rules_available: bool | None = None
        mark_price_available: bool | None = None
        if heavy_snapshot is not None:
            exchange_rules_available = getattr(heavy_snapshot, "exchange_rules", None) is not None
            mark_price_available = getattr(heavy_snapshot, "mark_price", None) is not None

        failures: list[str] = []
        if observed_prices < required_prices:
            failures.append("INSUFFICIENT_OBSERVED_PRICES")
        if stale:
            failures.append("STALE_DATA")
        if data_age_seconds > self.max_data_age_seconds:
            failures.append("DATA_TOO_OLD")
        if exchange_rules_available is False:
            failures.append("EXCHANGE_RULES_UNAVAILABLE")
        if mark_price_available is False:
            failures.append("MARK_PRICE_UNAVAILABLE")

        complete = not failures
        is_new_entry = normalized_action in OPEN_ENTRY_ACTIONS
        protective = self.mode != "OBSERVE"
        blocks_new_entry = bool(protective and not complete and is_new_entry and not position_open)
        reason = "WARMUP_COMPLETE" if complete else "SYMBOL_WARMUP_INCOMPLETE"

        return SymbolWarmupResult(
            symbol=normalized_symbol,
            action=normalized_action,
            complete=complete,
            blocks_new_entry=blocks_new_entry,
            reason=reason,
            observed_prices=observed_prices,
            min_observed_prices=required_prices,
            data_age_seconds=data_age_seconds,
            max_data_age_seconds=self.max_data_age_seconds,
            stale=stale,
            position_open=bool(position_open),
            mode=self.mode,
            exchange_rules_available=exchange_rules_available,
            mark_price_available=mark_price_available,
            diagnostics={
                "failures": failures,
                "is_new_entry": is_new_entry,
                "protective": protective,
            },
        )
