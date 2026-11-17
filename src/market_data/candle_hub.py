from __future__ import annotations

import time
from typing import Any

from domain.candle import Candle
from domain.strategy_requirements import StrategyDataRequirements
from domain.timeframe import timeframe_duration_ms
from market_data.candle_provider import CandleDataProvider, CandleProviderError
from market_data.candle_store import CandleDataStore
from utils.events import EventChannel, observe_event
from utils.logger import get_logger

CANDLE_HUB_MODE_OFF = "OFF"
CANDLE_HUB_MODE_SHADOW = "SHADOW"
CANDLE_HUB_MODE_ACTIVE_PAPER = "ACTIVE_PAPER"
CANDLE_HUB_MODE_ACTIVE_TESTNET = "ACTIVE_TESTNET"
CANDLE_HUB_MODE_ACTIVE_REAL = "ACTIVE_REAL"

_ACTIVE_MODES = {
    CANDLE_HUB_MODE_ACTIVE_PAPER,
    CANDLE_HUB_MODE_ACTIVE_TESTNET,
    CANDLE_HUB_MODE_ACTIVE_REAL,
}

_STALE_THRESHOLD_SECONDS = 120.0

# Extra candles kept per series beyond the strategy requirement, absorbing
# boundary conditions (duplicates, in-progress candle) without re-fetching.
_SERIES_LIMIT_HEADROOM = 16


class CandleDataHub:
    """
    Orchestrates candle fetching and storage for all strategy symbols.

    Modes:
      OFF       — nothing runs; no candles delivered.
      SHADOW    — candles collected and logged, but not delivered to strategy.
      ACTIVE_*  — candles delivered; used as input by strategies.

    The Governor's fast safety checks (position, stop, take, reconcile) are
    never blocked by this hub. Only strategy evaluation uses the hub's output.
    """

    def __init__(
        self,
        provider: CandleDataProvider,
        *,
        requirements_by_symbol: dict[str, StrategyDataRequirements],
        mode: str = CANDLE_HUB_MODE_SHADOW,
    ):
        self._provider = provider
        self._requirements = {sym.upper(): req for sym, req in requirements_by_symbol.items()}
        self._mode = str(mode or CANDLE_HUB_MODE_SHADOW).upper().strip()
        # Cap each series at the strategy requirement instead of the global
        # default; with many symbols this is the dominant RAM saving.
        series_limits = {
            (symbol, interval): count + _SERIES_LIMIT_HEADROOM
            for symbol, req in self._requirements.items()
            for interval, count in req.required_candles.items()
        }
        self._store = CandleDataStore(series_limits=series_limits)
        self._last_fetched_ms: dict[tuple[str, str], int] = {}
        self._bootstrap_done: set[tuple[str, str]] = set()
        self._logger = get_logger(__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def active(self) -> bool:
        return self._mode in _ACTIVE_MODES

    def bootstrap(self) -> None:
        """Load historical candles for all registered symbol/timeframe pairs."""
        if self._mode == CANDLE_HUB_MODE_OFF:
            return

        for symbol, req in self._requirements.items():
            for interval, count in req.required_candles.items():
                key = (symbol, interval)
                if key in self._bootstrap_done:
                    continue
                observe_event(
                    "candle_bootstrap_started",
                    component=__name__,
                    category="candle_hub",
                    channel=EventChannel.AUDIT,
                    symbol=symbol,
                    interval=interval,
                    requested=count,
                    mode=self._mode,
                )
                try:
                    candles = self._provider.fetch_closed_candles(
                        symbol=symbol,
                        interval=interval,
                        limit=count,
                    )
                    self._store.upsert_many(candles)
                    self._bootstrap_done.add(key)
                    if candles:
                        self._last_fetched_ms[key] = candles[-1].open_time_ms
                    observe_event(
                        "candle_bootstrap_completed",
                        component=__name__,
                        category="candle_hub",
                        channel=EventChannel.AUDIT,
                        symbol=symbol,
                        interval=interval,
                        fetched=len(candles),
                        mode=self._mode,
                    )
                except CandleProviderError as error:
                    observe_event(
                        "candle_bootstrap_failed",
                        component=__name__,
                        category="candle_hub",
                        channel=EventChannel.AUDIT,
                        severity="WARNING",
                        symbol=symbol,
                        interval=interval,
                        reason=str(error),
                        mode=self._mode,
                    )

    def refresh(self, *, now_ms: int | None = None) -> None:
        """Fetch any new closed candles since the last bootstrap/refresh."""
        if self._mode == CANDLE_HUB_MODE_OFF:
            return

        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)

        for symbol, req in self._requirements.items():
            for interval in req.required_candles:
                key = (symbol, interval)
                if key not in self._bootstrap_done:
                    continue

                duration_ms = timeframe_duration_ms(interval)
                last_ms = self._last_fetched_ms.get(key, 0)
                elapsed_since_last_ms = current_ms - last_ms
                if elapsed_since_last_ms < duration_ms:
                    continue

                try:
                    new_candles = self._provider.fetch_since(
                        symbol=symbol,
                        interval=interval,
                        since_open_time_ms=last_ms,
                    )
                    if new_candles:
                        self._store.upsert_many(new_candles)
                        self._last_fetched_ms[key] = new_candles[-1].open_time_ms
                        observe_event(
                            "candle_series_updated",
                            component=__name__,
                            category="candle_hub",
                            channel=EventChannel.AUDIT,
                            symbol=symbol,
                            interval=interval,
                            new_candles=len(new_candles),
                            last_open_time_ms=new_candles[-1].open_time_ms,
                            mode=self._mode,
                        )
                except CandleProviderError as error:
                    observe_event(
                        "candle_series_stale",
                        component=__name__,
                        category="candle_hub",
                        channel=EventChannel.AUDIT,
                        severity="WARNING",
                        symbol=symbol,
                        interval=interval,
                        reason=str(error),
                        mode=self._mode,
                    )

    def candle_snapshot_for_symbol(
        self,
        symbol: str,
    ) -> dict[str, tuple[Candle, ...]]:
        """
        Return {interval: (candle, ...)} for use in MarketSnapshot.
        Returns empty dict when mode is OFF or SHADOW (shadow = collect, not deliver).
        """
        if not self.active:
            return {}

        normalized = symbol.upper()
        req = self._requirements.get(normalized)
        if req is None:
            return {}

        result: dict[str, tuple[Candle, ...]] = {}
        for interval, count in req.required_candles.items():
            result[interval] = self._store.snapshot(normalized, interval, count)
        return result

    def requirements_for_symbol(self, symbol: str) -> StrategyDataRequirements | None:
        """Return normalized requirements for a symbol, when registered."""

        return self._requirements.get(str(symbol or "").upper().strip())

    def diagnostics_for_symbol(
        self,
        symbol: str,
    ) -> dict[str, dict[str, Any]]:
        """Return diagnostics dict per interval (always, regardless of mode)."""
        normalized = symbol.upper()
        req = self._requirements.get(normalized)
        if req is None:
            return {}

        result: dict[str, dict[str, Any]] = {}
        for interval, count in req.required_candles.items():
            diag = self._store.diagnostics(
                normalized,
                interval,
                required=count,
                stale_threshold_seconds=_STALE_THRESHOLD_SECONDS,
            )
            result[interval] = diag.as_dict()
        return result

    def warmup_complete_for_symbol(self, symbol: str) -> bool:
        """True when all required series are ready."""
        normalized = symbol.upper()
        req = self._requirements.get(normalized)
        if req is None:
            return False
        for interval, count in req.required_candles.items():
            diag = self._store.diagnostics(normalized, interval, required=count)
            if not diag.warmup_complete:
                return False
        return True

    def latest_closed_candle(self, symbol: str, interval: str) -> Candle | None:
        return self._store.latest_closed(symbol.upper(), interval)
