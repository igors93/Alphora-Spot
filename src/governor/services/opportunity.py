"""Opportunity ranking: which scheduled symbols a cycle evaluates, and in what order."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

from governor.evaluation.outcomes import EventJournal
from governor.governor_helpers import _dirty_symbol_can_wake, _priority_rank
from governor.opportunity_ranker import OpportunityRanker
from governor.portfolio_risk_engine import resolve_correlation_group
from scanner.priority_scheduler import ScheduledSymbol

RankKey = tuple[int, int, int, int, int, int, float, str]


class SymbolScheduler(Protocol):
    def due_symbols(self, now: float) -> Iterable[str]: ...

    def all_symbols(self) -> Iterable[ScheduledSymbol]: ...


def _no_trace() -> Mapping[str, str]:
    return {}


class OpportunityService:
    """Rank the due, open, dirty and fast-lane symbols of a cycle.

    The base order (open positions, fast lane, priority, score…) always applies;
    the opportunity ranker reorders only when ``governor_opportunity_ranking_enabled``
    is on, and is otherwise recorded as a shadow decision.
    """

    def __init__(
        self,
        *,
        config: Any,
        scheduler: SymbolScheduler,
        ranker: OpportunityRanker,
        quarantine: Callable[[], Any] = lambda: None,
        portfolio_settings: Any = None,
        journal: EventJournal | None = None,
        trace_payload: Callable[[], Mapping[str, str]] = _no_trace,
        logger: Any = None,
    ) -> None:
        self.config = config
        self.scheduler = scheduler
        self.ranker = ranker
        self.quarantine = quarantine
        self.portfolio_settings = portfolio_settings
        self.journal = journal
        self.trace_payload = trace_payload
        self.logger = logger

    def ranking_enabled(self) -> bool:
        return bool(getattr(self.config, "governor_opportunity_ranking_enabled", False))

    def rank_due_symbols(
        self,
        now: float,
        *,
        open_symbols: set[str] | None = None,
        dirty_symbols: list[str] | set[str] | None = None,
        fast_lane_symbols: list[str] | set[str] | None = None,
    ) -> list[str]:
        open_symbols = open_symbols or set()
        dirty = {symbol.upper().strip() for symbol in (dirty_symbols or []) if symbol}
        fast_lane = {symbol.upper().strip() for symbol in (fast_lane_symbols or []) if symbol}
        due = set(self.scheduler.due_symbols(now))
        scheduled = [
            item
            for item in self.scheduler.all_symbols()
            if (
                item.symbol in due
                or item.symbol in open_symbols
                or item.symbol in fast_lane
                or (
                    item.symbol in dirty
                    and _dirty_symbol_can_wake(
                        item.priority,
                        now=now,
                        last_checked_at=item.last_checked_at,
                        loop_interval_seconds=self.config.loop_interval_seconds,
                    )
                )
            )
        ]

        def base_key(item: ScheduledSymbol) -> RankKey:
            return _rank_key(
                item, open_symbols=open_symbols, fast_lane=fast_lane, due=due, dirty=dirty
            )

        base_ranked = sorted(scheduled, key=base_key, reverse=True)
        ranked = base_ranked
        opportunity_scores = self._opportunity_scores(
            scheduled, now=now, open_symbols=open_symbols, fast_lane=fast_lane
        )
        if self.ranking_enabled():
            scores_by_symbol = {item.symbol: item for item in opportunity_scores}
            ranked = sorted(
                scheduled,
                key=lambda item: (scores_by_symbol[item.symbol].opportunity_score, *base_key(item)),
                reverse=True,
            )
        self._record_shadow(
            scheduled=scheduled,
            base_ranked=base_ranked,
            ranked=ranked,
            opportunity_scores=opportunity_scores,
        )
        if len(ranked) > 1:
            self.logger.info(
                "Governador avaliando simbolos candidatos por score/prioridade: %s",
                ", ".join(f"{item.symbol}:{item.score}/{item.priority.value}" for item in ranked),
            )
        return [item.symbol for item in ranked]

    def _opportunity_scores(
        self,
        scheduled: list[ScheduledSymbol],
        *,
        now: float,
        open_symbols: set[str],
        fast_lane: set[str],
    ) -> list[Any]:
        symbols = [item.symbol for item in scheduled]
        scores: list[Any] = self.ranker.rank(
            scheduled,
            fast_lane_symbols=fast_lane,
            open_symbols=open_symbols,
            quarantine_levels=self._quarantine_levels(symbols, now=now),
            symbols_with_group_exposure=self._symbols_with_group_exposure(
                symbols, open_symbols=open_symbols
            ),
        )
        return scores

    def _quarantine_levels(self, symbols: list[str], *, now: float) -> dict[str, str]:
        manager = self.quarantine()
        if manager is None:
            return {}
        return {symbol: manager.state_for(symbol, now=now).level.value for symbol in symbols}

    def _symbols_with_group_exposure(
        self,
        symbols: list[str],
        *,
        open_symbols: set[str],
    ) -> set[str]:
        if not open_symbols:
            return set()
        settings = self.portfolio_settings
        open_groups = {resolve_correlation_group(symbol, settings)[0] for symbol in open_symbols}
        exposed: set[str] = set()
        for symbol in symbols:
            group = resolve_correlation_group(symbol, settings)[0]
            if group in open_groups:
                exposed.add(symbol)
        return exposed

    def _record_shadow(
        self,
        *,
        scheduled: list[ScheduledSymbol],
        base_ranked: list[ScheduledSymbol],
        ranked: list[ScheduledSymbol],
        opportunity_scores: list[Any],
    ) -> None:
        if self.journal is None:
            return
        enabled = self.ranking_enabled()
        self.journal.append(
            "governor_opportunity_ranking_shadow",
            **self.trace_payload(),
            status="APPLIED" if enabled else "SHADOW",
            enabled=enabled,
            original_order=[item.symbol for item in scheduled],
            base_ranked_order=[item.symbol for item in base_ranked],
            ranked_order=[item.symbol for item in ranked],
            opportunity_scores=[item.to_dict() for item in opportunity_scores],
            execution_changed=enabled,
            order_mutation=False,
            diagnostics={
                "safety_note": (
                    "Ranking de oportunidade nao bloqueia simbolos; "
                    "em shadow ele nao altera a ordem de execucao."
                ),
            },
        )


def _rank_key(
    item: ScheduledSymbol,
    *,
    open_symbols: set[str],
    fast_lane: set[str],
    due: set[str],
    dirty: set[str],
) -> RankKey:
    return (
        1 if item.symbol in open_symbols else 0,
        1 if item.symbol in fast_lane else 0,
        _priority_rank(item.priority),
        item.score,
        1 if item.symbol in due else 0,
        1 if item.symbol in dirty else 0,
        -item.next_check_at,
        item.symbol,
    )
