"""The per-symbol runtime status board shown to operators and the desktop."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import replace
from typing import Protocol

from governor.models import SymbolRuntimeSnapshot, SymbolRuntimeStatus
from scanner.signal_score import SignalPriority


class StrategyNameSource(Protocol):
    @property
    def global_strategy_name(self) -> str: ...


class SymbolRuntimeBoard:
    """Write access to the Governor ``snapshots`` mapping for evaluation stages.

    The board wraps the mapping owned by the Governor instead of copying it, so
    every write is immediately visible to the state writer and the desktop.
    """

    def __init__(
        self,
        snapshots: MutableMapping[str, SymbolRuntimeSnapshot],
        *,
        strategy: StrategyNameSource,
    ) -> None:
        self._snapshots = snapshots
        self._strategy = strategy

    def get(self, symbol: str) -> SymbolRuntimeSnapshot | None:
        return self._snapshots.get(symbol)

    def publish(self, snapshot: SymbolRuntimeSnapshot) -> None:
        self._snapshots[snapshot.symbol] = snapshot

    def mark_blocked(self, symbol: str, reason: str) -> None:
        previous = self._snapshots.get(symbol)
        self._snapshots[symbol] = SymbolRuntimeSnapshot(
            symbol=symbol,
            enabled=True,
            status=SymbolRuntimeStatus.BLOCKED,
            score=previous.score if previous else 0,
            priority=previous.priority if previous else SignalPriority.COLD,
            last_price=previous.last_price if previous else "0",
            last_checked_at=previous.last_checked_at if previous else 0.0,
            next_check_at=previous.next_check_at if previous else 0.0,
            data_age_seconds=previous.data_age_seconds if previous else 0.0,
            stale=previous.stale if previous else False,
            strategy=self._strategy.global_strategy_name,
            reason=reason,
        )

    def mark_error(self, symbol: str, message: str) -> None:
        self._snapshots[symbol] = SymbolRuntimeSnapshot(
            symbol=symbol,
            enabled=True,
            status=SymbolRuntimeStatus.ERROR,
            strategy=self._strategy.global_strategy_name,
            error=message,
        )

    def set_disabled(self, symbol: str, status: SymbolRuntimeStatus) -> None:
        """Show a paused or stopped symbol without evaluating it."""

        self._snapshots[symbol] = SymbolRuntimeSnapshot(
            symbol=symbol,
            enabled=False,
            status=status,
            strategy=self._strategy.global_strategy_name,
            reason=status.value,
        )

    def mark_no_action(self, symbol: str, reason: str) -> None:
        previous = self._snapshots.get(symbol)
        if previous is None:
            return
        self._snapshots[symbol] = replace(previous, reason=reason)
