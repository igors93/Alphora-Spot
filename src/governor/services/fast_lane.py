"""Fast lane: symbols hit by a market shock jump the evaluation queue of the cycle."""

from __future__ import annotations

from collections.abc import Collection, MutableMapping
from typing import Any

from governor.evaluation.outcomes import EventJournal


class FastLaneService:
    """Pick the shocked symbols that are evaluated first, with a per-symbol cooldown.

    The fast lane only reorders the cycle; it never blocks a trade or sends an order.
    ``last_selected_at`` belongs to the Governor and keeps the cooldown across cycles.
    """

    def __init__(
        self,
        *,
        config: Any,
        journal: EventJournal,
        symbols: Collection[str],
        stopped_symbols: Collection[str],
        paused_symbols: Collection[str],
        last_selected_at: MutableMapping[str, float],
    ) -> None:
        self.config = config
        self.journal = journal
        self.symbols = symbols
        self.stopped_symbols = stopped_symbols
        self.paused_symbols = paused_symbols
        self.last_selected_at = last_selected_at

    def enabled(self) -> bool:
        return bool(getattr(self.config, "governor_fast_lane_enabled", True))

    def max_symbols_per_cycle(self) -> int:
        value = int(getattr(self.config, "governor_fast_lane_max_symbols_per_cycle", 3) or 0)
        return max(0, value)

    def cooldown_seconds(self) -> float:
        value = float(getattr(self.config, "governor_fast_lane_cooldown_seconds", 2.0) or 0.0)
        return max(0.0, value)

    def symbols_from_shocks(
        self,
        *,
        shock_events: list[dict[str, Any]],
        now: float,
    ) -> list[str]:
        if not self.enabled():
            return []

        limit = self.max_symbols_per_cycle()
        if limit <= 0:
            return []

        valid_symbols = set(self.symbols)
        cooldown_seconds = self.cooldown_seconds()
        selected: list[str] = []
        seen: set[str] = set()

        for event in shock_events:
            symbol = str(event.get("symbol", "")).upper().strip()
            if not symbol or symbol in seen:
                continue

            if valid_symbols and symbol not in valid_symbols:
                continue

            if symbol in self.stopped_symbols or symbol in self.paused_symbols:
                continue

            last_at = float(self.last_selected_at.get(symbol, 0.0) or 0.0)
            if cooldown_seconds > 0 and last_at > 0 and now - last_at < cooldown_seconds:
                continue

            selected.append(symbol)
            seen.add(symbol)
            self.last_selected_at[symbol] = now

            if len(selected) >= limit:
                break

        return selected

    def record_prioritization(
        self,
        *,
        current_time: float,
        fast_lane_symbols: list[str],
        selected_symbols: list[str],
        shock_events: list[dict[str, Any]],
    ) -> None:
        if not fast_lane_symbols:
            return

        self.journal.append(
            "governor_fast_lane_prioritized",
            status="APPLIED",
            reason="MARKET_SHOCK_PRIORITY",
            current_time=current_time,
            fast_lane_symbols=fast_lane_symbols,
            selected_symbols=selected_symbols,
            shock_events_count=len(shock_events),
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10C: Fast Lane apenas prioriza avaliacao no ciclo. "
                    "Nao bloqueia trades e nao envia ordens."
                ),
            },
        )
