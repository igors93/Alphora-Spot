"""Market-shock wakeups: end the wait between two cycles early."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from governor.evaluation.outcomes import EventJournal
from trading_cadence import MIN_LOOP_INTERVAL_SECONDS

Clock = Callable[[], float]
Sleep = Callable[[float], None]


class MarketWakeup:
    """Wakeup signal set by the market data hub thread and consumed by the cycle loop.

    A wakeup only brings the next cycle forward; it never sends orders or changes a
    decision. A global cooldown suppresses bursts of shocks. Long-lived: the
    composition root creates one per Governor and hands ``on_market_shock`` to the hub.
    """

    def __init__(self, *, config: Any, clock: Clock = time.time, sleep: Sleep = time.sleep):
        self.config = config
        self.clock = clock
        self.sleep = sleep
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.last_wakeup_at = 0.0
        self.count = 0
        self.suppressed_count = 0
        self.last_symbol = ""
        self.last_reason = ""

    def enabled(self) -> bool:
        return bool(getattr(self.config, "governor_wakeup_enabled", True))

    def cooldown_seconds(self) -> float:
        value = float(getattr(self.config, "governor_wakeup_cooldown_seconds", 1.0) or 0.0)
        return max(0.0, value)

    def on_market_shock(self, event: dict[str, Any]) -> None:
        if not self.enabled():
            return

        now = self.clock()
        symbol = str(event.get("symbol", "")).upper().strip()
        reason = str(event.get("reason", "MARKET_SHOCK")).strip() or "MARKET_SHOCK"
        cooldown_seconds = self.cooldown_seconds()

        with self.lock:
            if (
                cooldown_seconds > 0
                and self.last_wakeup_at > 0
                and now - self.last_wakeup_at < cooldown_seconds
            ):
                self.suppressed_count += 1
                return

            self.last_wakeup_at = now
            self.count += 1
            self.last_symbol = symbol
            self.last_reason = reason
            self.event.set()

    def consume_stats(self) -> dict[str, Any]:
        with self.lock:
            stats = {
                "wakeup_count": self.count,
                "suppressed_count": self.suppressed_count,
                "last_symbol": self.last_symbol,
                "last_reason": self.last_reason,
                "last_wakeup_at": self.last_wakeup_at,
            }
            self.count = 0
            self.suppressed_count = 0
            return stats

    def clear(self) -> None:
        self.event.clear()

    def wait_for_next_cycle(
        self,
        *,
        journal: EventJournal,
        heartbeat: Callable[[], object],
    ) -> None:
        """Sleep one loop interval, or less when a market shock wakes the Governor."""

        # Never busy-loop: a zero/negative interval would hammer the exchange API.
        interval_seconds = max(
            MIN_LOOP_INTERVAL_SECONDS,
            float(self.config.loop_interval_seconds or 0.0),
        )

        if not self.enabled():
            self.sleep(interval_seconds)
            heartbeat()
            return

        woke_up = self.event.wait(timeout=interval_seconds)
        if not woke_up:
            heartbeat()
            return

        self.event.clear()
        stats = self.consume_stats()
        journal.append(
            "governor_wakeup_event_consumed",
            status="APPLIED",
            reason="MARKET_SHOCK_WAKEUP",
            loop_interval_seconds=interval_seconds,
            wakeup_count=stats["wakeup_count"],
            suppressed_count=stats["suppressed_count"],
            last_symbol=stats["last_symbol"],
            last_reason=stats["last_reason"],
            last_wakeup_at=stats["last_wakeup_at"],
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Fase 10D: Wakeup apenas antecipa o proximo ciclo do Governor. "
                    "Nao envia ordens e nao muda decisoes diretamente."
                ),
            },
        )
        heartbeat()
