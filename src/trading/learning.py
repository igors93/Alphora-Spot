from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading.outcome import (
    OUTCOME_BREAKEVEN,
    OUTCOME_LOSS,
    OUTCOME_UNKNOWN,
    OUTCOME_WIN,
)
from utils.jsonl import parse_json_object_line

TRADE_PLAN_LEARNING_VERSION = "1.0.0"
OUTCOME_EVENT_TYPE = "trade_plan_outcome_recorded"


@dataclass(frozen=True)
class OutcomeBucketStats:
    count: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    unknown: int = 0
    linked: int = 0
    total_net_pnl: Decimal = Decimal("0")
    average_net_pnl: Decimal = Decimal("0")
    win_rate: Decimal = Decimal("0")


@dataclass(frozen=True)
class TradePlanLearningSummary:
    total_outcomes: int
    linked_outcomes: int
    unlinked_outcomes: int
    wins: int
    losses: int
    breakeven: int
    unknown: int
    total_net_pnl: Decimal
    average_net_pnl: Decimal
    win_rate: Decimal
    adjusted: OutcomeBucketStats
    not_adjusted: OutcomeBucketStats
    by_symbol: dict[str, OutcomeBucketStats] = field(default_factory=dict)
    version: str = TRADE_PLAN_LEARNING_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class TradePlanLearningAnalyzer:
    def summarize_journal(self, path: str | Path) -> TradePlanLearningSummary:
        return self.summarize_outcomes(load_trade_plan_outcomes_from_journal(path))

    def summarize_outcomes(
        self,
        outcomes: list[dict[str, Any]],
    ) -> TradePlanLearningSummary:
        total = len(outcomes)
        linked = sum(1 for outcome in outcomes if outcome.get("trade_plan_id"))
        wins = _count_status(outcomes, OUTCOME_WIN)
        losses = _count_status(outcomes, OUTCOME_LOSS)
        breakeven = _count_status(outcomes, OUTCOME_BREAKEVEN)
        unknown = _count_status(outcomes, OUTCOME_UNKNOWN)
        total_net_pnl = _sum_net_pnl(outcomes)
        average_net_pnl = _average(total_net_pnl, total)
        win_rate = _ratio(wins, total)

        adjusted_outcomes = [outcome for outcome in outcomes if _adaptive_adjustments(outcome) > 0]
        not_adjusted_outcomes = [
            outcome for outcome in outcomes if _adaptive_adjustments(outcome) <= 0
        ]

        return TradePlanLearningSummary(
            total_outcomes=total,
            linked_outcomes=linked,
            unlinked_outcomes=total - linked,
            wins=wins,
            losses=losses,
            breakeven=breakeven,
            unknown=unknown,
            total_net_pnl=total_net_pnl,
            average_net_pnl=average_net_pnl,
            win_rate=win_rate,
            adjusted=_bucket_stats(adjusted_outcomes),
            not_adjusted=_bucket_stats(not_adjusted_outcomes),
            by_symbol=_group_by_symbol(outcomes),
            diagnostics={
                "has_outcomes": total > 0,
                "source_event_type": OUTCOME_EVENT_TYPE,
                "summary_version": TRADE_PLAN_LEARNING_VERSION,
            },
        )


def load_trade_plan_outcomes_from_journal(path: str | Path) -> list[dict[str, Any]]:
    journal_path = Path(path)
    if not journal_path.exists():
        return []

    outcomes: list[dict[str, Any]] = []
    with journal_path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue

            event = parse_json_object_line(line)
            if event is None:
                continue

            if event.get("event_type") != OUTCOME_EVENT_TYPE:
                continue

            outcome = _extract_outcome_payload(event)
            if outcome is not None:
                outcomes.append(outcome)

    return outcomes


def _extract_outcome_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None

    outcome = data.get("outcome")
    if isinstance(outcome, dict):
        return outcome

    if data.get("trade_plan_id") is not None or data.get("outcome_status") is not None:
        return {
            "symbol": data.get("symbol"),
            "trade_plan_id": data.get("trade_plan_id"),
            "outcome_status": data.get("outcome_status"),
            "net_pnl": data.get("net_pnl"),
            "adaptive_adjustments_applied": data.get(
                "adaptive_adjustments_applied",
                0,
            ),
        }

    return None


def _bucket_stats(outcomes: list[dict[str, Any]]) -> OutcomeBucketStats:
    count = len(outcomes)
    wins = _count_status(outcomes, OUTCOME_WIN)
    losses = _count_status(outcomes, OUTCOME_LOSS)
    breakeven = _count_status(outcomes, OUTCOME_BREAKEVEN)
    unknown = _count_status(outcomes, OUTCOME_UNKNOWN)
    linked = sum(1 for outcome in outcomes if outcome.get("trade_plan_id"))
    total_net_pnl = _sum_net_pnl(outcomes)

    return OutcomeBucketStats(
        count=count,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        unknown=unknown,
        linked=linked,
        total_net_pnl=total_net_pnl,
        average_net_pnl=_average(total_net_pnl, count),
        win_rate=_ratio(wins, count),
    )


def _group_by_symbol(outcomes: list[dict[str, Any]]) -> dict[str, OutcomeBucketStats]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        symbol = str(outcome.get("symbol") or "UNKNOWN")
        grouped.setdefault(symbol, []).append(outcome)

    return {symbol: _bucket_stats(symbol_outcomes) for symbol, symbol_outcomes in grouped.items()}


def _count_status(outcomes: list[dict[str, Any]], status: str) -> int:
    return sum(1 for outcome in outcomes if outcome.get("outcome_status") == status)


def _sum_net_pnl(outcomes: list[dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for outcome in outcomes:
        value = _optional_decimal(outcome.get("net_pnl"))
        if value is not None:
            total += value

    return total


def _adaptive_adjustments(outcome: dict[str, Any]) -> int:
    raw = outcome.get("adaptive_adjustments_applied", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _average(total: Decimal, count: int) -> Decimal:
    if count <= 0:
        return Decimal("0")

    return total / Decimal(str(count))


def _ratio(part: int, total: int) -> Decimal:
    if total <= 0:
        return Decimal("0")

    return Decimal(str(part)) / Decimal(str(total))


def _optional_decimal(value) -> Decimal | None:
    if value is None:
        return None

    try:
        return Decimal(str(value))
    except Exception:
        return None
