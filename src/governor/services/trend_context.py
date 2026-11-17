"""Observational trend context attached to the market snapshot of each evaluation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from domain.models import MarketSnapshot
from futures.trend import TrendContext, TrendContextEngine
from governor.evaluation.outcomes import EventJournal
from governor.events import TREND_CONTEXT_EVALUATED
from governor.safe_journal import safe_append

TracePayload = Callable[[], Mapping[str, str]]


def _no_trace() -> Mapping[str, str]:
    return {}


def attach_trend_context(market: MarketSnapshot, trend_context: TrendContext) -> MarketSnapshot:
    """Return a copy of the market snapshot with the trend indicator set."""

    indicators = dict(getattr(market, "indicators", {}) or {})
    indicators["trend_context"] = trend_context
    return replace(market, indicators=indicators)


class TrendContextService:
    """Evaluate and record the trend context; it never blocks, sizes or sends orders.

    ``mode`` (``OBSERVE`` by default) is also the regime policy mode the guards read. The trend
    is computed on the closed candles of ``timeframe_for(symbol)``: the strategy's primary
    timeframe unless ``ALPHORA_TREND_TIMEFRAME`` names another one, the same series the
    backtest uses.
    """

    def __init__(
        self,
        *,
        config: Any,
        engine: TrendContextEngine,
        journal: EventJournal,
        timeframe_for: Callable[[str], str],
        logger: Any = None,
        trace_payload: TracePayload = _no_trace,
    ) -> None:
        self.config = config
        self.engine = engine
        self.journal = journal
        self.timeframe_for = timeframe_for
        self.logger = logger
        self.trace_payload = trace_payload

    def enabled(self) -> bool:
        return bool(getattr(self.config, "trend_context_enabled", True))

    def mode(self) -> str:
        return str(getattr(self.config, "trend_context_mode", "OBSERVE") or "OBSERVE").upper()

    def for_market(self, *, symbol: str, market: Any, timestamp: float) -> TrendContext | None:
        if not self.enabled():
            return None
        candles = (getattr(market, "candles", None) or {}).get(self.timeframe_for(symbol), ())
        return self.engine.evaluate_candles(symbol=symbol, candles=candles, timestamp=timestamp)

    def record_evaluated(self, trend_context: TrendContext) -> None:
        payload = trend_context.to_dict()
        diagnostics = dict(payload.pop("diagnostics", {}) or {})
        mode = self.mode()
        safe_append(
            self.journal,
            TREND_CONTEXT_EVALUATED,
            logger=self.logger,
            **self.trace_payload(),
            symbol=payload["symbol"],
            mode=mode,
            direction=payload["direction"],
            strength=payload["strength"],
            regime=payload["regime"],
            confidence=payload["confidence"],
            data_points=payload["data_points"],
            price_above_fast_ma=payload["price_above_fast_ma"],
            price_above_slow_ma=payload["price_above_slow_ma"],
            fast_ma_slope=payload["fast_ma_slope"],
            slow_ma_slope=payload["slow_ma_slope"],
            volatility_pct=payload["volatility_pct"],
            chop_score=payload["chop_score"],
            execution_changed=False,
            order_mutation=False,
            diagnostics={
                **diagnostics,
                "observe_only": mode == "OBSERVE",
                "safety_note": (
                    "TrendContext e somente contexto observacional. "
                    "Nao bloqueia, nao altera sizing e nao envia ordens."
                ),
            },
        )
