"""Symbol health: warmup requirements, quarantine and per-symbol error records."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from config.symbol_context import SymbolTradingConfig
from domain.models import MarketSnapshot
from domain.timeframe import timeframe_duration_ms
from governor.evaluation.outcomes import EventJournal
from governor.evaluation.runtime_board import SymbolRuntimeBoard
from governor.symbol_quarantine import SymbolQuarantine, SymbolQuarantineSettings
from governor.symbol_warmup import SymbolWarmupEvaluator
from scanner.signal_score import SignalScore

TracePayload = Callable[[], Mapping[str, str]]


def _no_trace() -> Mapping[str, str]:
    return {}


def _setting(config: Any, name: str, legacy_name: str, default: float) -> Any:
    """Read ``governor_<name>`` and fall back to the legacy ``<name>`` setting."""

    return getattr(config, name, getattr(config, legacy_name, default))


def build_symbol_quarantine(config: Any) -> SymbolQuarantine:
    prefix = "governor_symbol_quarantine_"
    legacy = "symbol_quarantine_"
    return SymbolQuarantine(
        SymbolQuarantineSettings(
            watch_after_errors=int(
                _setting(config, f"{prefix}watch_after_errors", f"{legacy}watch_after_errors", 2)
                or 2
            ),
            cooldown_after_errors=int(
                _setting(
                    config, f"{prefix}cooldown_after_errors", f"{legacy}cooldown_after_errors", 3
                )
                or 3
            ),
            quarantine_after_errors=int(
                _setting(
                    config,
                    f"{prefix}quarantine_after_errors",
                    f"{legacy}quarantine_after_errors",
                    5,
                )
                or 5
            ),
            cooldown_seconds=float(
                _setting(config, f"{prefix}cooldown_seconds", f"{legacy}cooldown_seconds", 600.0)
                or 600.0
            ),
            quarantine_seconds=float(
                _setting(
                    config, f"{prefix}quarantine_seconds", f"{legacy}quarantine_seconds", 1800.0
                )
                or 1800.0
            ),
        )
    )


class SymbolHealthService:
    """Warmup requirements, quarantine transitions and the error record of a symbol."""

    def __init__(
        self,
        *,
        config: Any,
        strategy: Any,
        quarantine: SymbolQuarantine,
        board: SymbolRuntimeBoard,
        journal: EventJournal,
        logger: Any,
        trace_payload: TracePayload = _no_trace,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.quarantine = quarantine
        self.board = board
        self.journal = journal
        self.logger = logger
        self.trace_payload = trace_payload

    # --- warmup -------------------------------------------------------------------------

    def warmup_evaluator(self) -> SymbolWarmupEvaluator:
        config = self.config
        max_age = float(getattr(config, "governor_market_data_max_age_seconds", 30.0) or 30.0)
        return SymbolWarmupEvaluator(
            min_observed_prices=self.warmup_min_observed_prices(),
            max_data_age_seconds=max_age,
            mode=getattr(config, "governor_symbol_warmup_mode", "PROTECTIVE"),
        )

    def warmup_min_observed_prices(self) -> int:
        strategy = getattr(self.strategy, "_global_strategy", None)
        metadata = getattr(strategy, "metadata", None)
        required_history = int(getattr(metadata, "required_history", 0) or 0)
        if required_history > 0:
            return required_history

        config = self.config
        if strategy is None and not hasattr(config, "governor_symbol_warmup_min_observed_prices"):
            return 1
        value = int(getattr(config, "governor_symbol_warmup_min_observed_prices", 20) or 20)
        return max(1, value)

    # --- quarantine and errors ----------------------------------------------------------

    def quarantine_manager(self) -> SymbolQuarantine:
        return self.quarantine

    def record_quarantine_event(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        event: str,
        reason: str,
        now: float | None = None,
    ) -> None:
        event_time = time.time() if now is None else now
        previous, current = self.quarantine.record_event(
            symbol_config.symbol,
            event=event,
            reason=reason,
            now=event_time,
        )
        if previous.level is current.level:
            return
        self.journal.append(
            "symbol_quarantine_state_changed",
            **self.trace_payload(),
            symbol=symbol_config.symbol,
            previous_level=previous.level.value,
            new_level=current.level.value,
            reason=current.reason,
            event=event,
            state=current.to_dict(),
            execution_changed=False,
            order_mutation=False,
        )

    def record_symbol_error(self, symbol_config: SymbolTradingConfig, error: Exception) -> None:
        message = str(error)
        self.logger.error("Erro no simbolo %s: %s", symbol_config.symbol, message)
        self.record_quarantine_event(
            symbol_config=symbol_config,
            event="SYMBOL_EVALUATION_ERROR",
            reason=message,
        )
        self.board.mark_error(symbol_config.symbol, message)
        self.journal.append(
            "symbol_error",
            symbol=symbol_config.symbol,
            strategy=self.strategy.global_strategy_name,
            reason=message,
            execution_mode=self.config.execution_mode.value,
            environment=self.config.environment.value,
        )

    # --- strategy evaluation policy -----------------------------------------------------

    def strategy_skip_score(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        market: MarketSnapshot,
        reason: str,
        now: float,
    ) -> SignalScore:
        """The neutral score used when the strategy is not evaluated on this snapshot."""

        return SignalScore(
            symbol=symbol_config.symbol,
            score=0,
            reason=reason,
            action="NO_ACTION",
            confidence=0,
            suggested_next_check_seconds=self._next_check_seconds(
                symbol_config=symbol_config, market=market, now=now
            ),
            data_age_seconds=float(getattr(market, "data_age_seconds", 0.0) or 0.0),
            strategy=self.strategy.global_strategy_name,
            metadata={
                "source": "GOVERNOR_STRATEGY_EVALUATION_POLICY",
                "skip_reason": reason,
                "primary_timeframe": getattr(market, "primary_timeframe", None),
            },
        )

    def _next_check_seconds(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        market: MarketSnapshot,
        now: float,
    ) -> float:
        """Seconds until the next close of the primary candle (bounded to five minutes)."""

        default_delay = max(
            0.0, min(float(getattr(self.config, "loop_interval_seconds", 1.0)), 5.0)
        )
        try:
            requirement = self.strategy.requirements_for_symbol(
                symbol_config.symbol,
                symbol_config=symbol_config,
            )
            candles = getattr(market, "candles", None)
            if not candles:
                return default_delay
            series = candles.get(requirement.primary_timeframe)
            if not series:
                return default_delay
            last_candle = series[-1]
            next_close_ms = int(last_candle.close_time_ms)
            if bool(getattr(last_candle, "is_closed", False)):
                next_close_ms += timeframe_duration_ms(requirement.primary_timeframe)
            return max(0.0, min(300.0, (next_close_ms / 1000.0) - float(now)))
        except Exception:
            return default_delay
