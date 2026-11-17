from dataclasses import dataclass

from scanner.signal_score import SignalPriority, SignalScore, default_next_check_seconds

PRIORITY_RANK = {
    SignalPriority.COLD: 0,
    SignalPriority.WARM: 1,
    SignalPriority.HOT: 2,
    SignalPriority.READY: 3,
}


@dataclass(frozen=True)
class ScheduledSymbol:
    symbol: str
    score: int
    priority: SignalPriority
    next_check_at: float
    last_checked_at: float
    data_age_seconds: float
    stale: bool
    reason: str = ""


class PriorityScheduler:
    def __init__(self, *, max_interval_seconds: float = 300.0, max_data_age_seconds: float = 120.0):
        if max_interval_seconds <= 0:
            raise ValueError("max_interval_seconds precisa ser maior que zero")
        if max_data_age_seconds <= 0:
            raise ValueError("max_data_age_seconds precisa ser maior que zero")

        self.max_interval_seconds = float(max_interval_seconds)
        self.max_data_age_seconds = float(max_data_age_seconds)
        self._symbols: dict[str, ScheduledSymbol] = {}

    def update(self, score: SignalScore, now: float) -> ScheduledSymbol:
        raw_interval = score.suggested_next_check_seconds
        interval = min(
            self.max_interval_seconds,
            float(raw_interval) if raw_interval is not None else self.max_interval_seconds,
        )
        priority = score.priority
        scheduled = ScheduledSymbol(
            symbol=score.symbol,
            score=int(score.score),
            priority=priority if priority is not None else SignalPriority.COLD,
            next_check_at=now + interval,
            last_checked_at=now,
            data_age_seconds=float(score.data_age_seconds),
            stale=float(score.data_age_seconds) > self.max_data_age_seconds,
            reason=score.reason,
        )
        self._symbols[score.symbol] = scheduled
        return scheduled

    def due_symbols(self, now: float) -> list[str]:
        return [item.symbol for item in self._symbols.values() if item.next_check_at <= now]

    def ensure_symbols(self, symbols: list[str], now: float) -> None:
        for symbol in symbols:
            normalized = symbol.upper().strip()
            if normalized not in self._symbols:
                cold_interval = default_next_check_seconds(SignalPriority.COLD)
                self._symbols[normalized] = ScheduledSymbol(
                    symbol=normalized,
                    score=0,
                    priority=SignalPriority.COLD,
                    next_check_at=now,
                    last_checked_at=now - cold_interval,
                    data_age_seconds=0.0,
                    stale=False,
                    reason="Ainda nao avaliado",
                )

    def top_symbols(self, limit: int) -> list[ScheduledSymbol]:
        if limit < 1:
            return []
        return sorted(
            self._symbols.values(),
            key=lambda item: (
                PRIORITY_RANK[item.priority],
                item.score,
                -item.next_check_at,
                item.symbol,
            ),
            reverse=True,
        )[:limit]

    def summary(self) -> dict[str, int]:
        summary = {priority.value: 0 for priority in SignalPriority}
        for item in self._symbols.values():
            summary[item.priority.value] += 1
        return summary

    def all_symbols(self) -> list[ScheduledSymbol]:
        return list(self._symbols.values())
