from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading.learning import OUTCOME_EVENT_TYPE
from trading.learning_execution_shadow import (
    LEARNING_EXECUTION_STATUS_ALLOW,
    LEARNING_EXECUTION_STATUS_NO_GUARD,
    LEARNING_EXECUTION_STATUS_NO_PLAN,
    LEARNING_EXECUTION_STATUS_WOULD_BLOCK,
    LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE,
)
from utils.jsonl import parse_json_object_line

LEARNING_SHADOW_IMPACT_VERSION = "1.0.0"
LEARNING_EXECUTION_SHADOW_EVENT_TYPE = "learning_execution_shadow_evaluated"

IMPACT_NOT_MATCHED = "NOT_MATCHED"
IMPACT_NO_CHANGE = "NO_CHANGE"
IMPACT_BLOCK_WOULD_HELP = "BLOCK_WOULD_HELP"
IMPACT_BLOCK_WOULD_HURT = "BLOCK_WOULD_HURT"
IMPACT_BLOCK_NEUTRAL = "BLOCK_NEUTRAL"
IMPACT_REDUCE_SIZE_WOULD_HELP = "REDUCE_SIZE_WOULD_HELP"
IMPACT_REDUCE_SIZE_WOULD_HURT = "REDUCE_SIZE_WOULD_HURT"
IMPACT_REDUCE_SIZE_NEUTRAL = "REDUCE_SIZE_NEUTRAL"


@dataclass(frozen=True)
class LearningShadowImpactTrade:
    symbol: str
    plan_id: str
    shadow_status: str
    suggested_action: str
    outcome_status: str
    net_pnl: Decimal
    estimated_shadow_net_pnl: Decimal
    estimated_pnl_delta: Decimal
    impact_status: str
    would_block_trade: bool = False
    would_reduce_size: bool = False
    original_quantity: Decimal | None = None
    shadow_quantity: Decimal | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LearningShadowImpactSummary:
    total_shadow_decisions: int
    matched_outcomes: int
    unmatched_shadow_decisions: int
    allow_count: int
    would_block_count: int
    would_reduce_size_count: int
    block_would_help_count: int
    block_would_hurt_count: int
    block_neutral_count: int
    reduce_size_would_help_count: int
    reduce_size_would_hurt_count: int
    reduce_size_neutral_count: int
    estimated_loss_avoided_by_blocks: Decimal
    estimated_profit_missed_by_blocks: Decimal
    estimated_block_net_impact: Decimal
    estimated_reduce_size_net_impact: Decimal
    estimated_total_shadow_impact: Decimal
    matched_trades: list[LearningShadowImpactTrade] = field(default_factory=list)
    version: str = LEARNING_SHADOW_IMPACT_VERSION
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LearningShadowImpactAnalyzer:
    def analyze_journal(self, path: str | Path) -> LearningShadowImpactSummary:
        events = load_jsonl_events(path)
        return self.analyze_events(events)

    def analyze_events(self, events: list[dict[str, Any]]) -> LearningShadowImpactSummary:
        shadows = [
            payload for event in events if (payload := _extract_shadow_payload(event)) is not None
        ]
        outcomes_by_plan_id = _outcomes_by_plan_id(events)

        matched_trades: list[LearningShadowImpactTrade] = []
        unmatched = 0

        for shadow in shadows:
            plan_id = str(shadow.get("plan_id") or "")
            if not plan_id:
                unmatched += 1
                continue

            outcome = outcomes_by_plan_id.get(plan_id)
            if outcome is None:
                unmatched += 1
                continue

            matched_trades.append(_build_impact_trade(shadow=shadow, outcome=outcome))

        return _build_summary(
            total_shadow_decisions=len(shadows),
            unmatched_shadow_decisions=unmatched,
            matched_trades=matched_trades,
        )


def load_jsonl_events(path: str | Path) -> list[dict[str, Any]]:
    journal_path = Path(path)
    if not journal_path.exists():
        return []

    events: list[dict[str, Any]] = []
    with journal_path.open("r", encoding="utf-8") as file:
        for line in file:
            event = parse_json_object_line(line.strip())
            if event is not None:
                events.append(event)

    return events


def format_learning_shadow_impact_summary(summary: LearningShadowImpactSummary) -> str:
    lines = [
        "Alphora Learning Shadow Impact",
        "==============================",
        f"Total shadow decisions: {summary.total_shadow_decisions}",
        f"Matched outcomes: {summary.matched_outcomes}",
        f"Unmatched shadow decisions: {summary.unmatched_shadow_decisions}",
        "",
        "Ações simuladas:",
        f"- Allow/no change: {summary.allow_count}",
        f"- Would block: {summary.would_block_count}",
        f"- Would reduce size: {summary.would_reduce_size_count}",
        "",
        "Impacto estimado:",
        f"- Loss avoided by blocks: {summary.estimated_loss_avoided_by_blocks}",
        f"- Profit missed by blocks: {summary.estimated_profit_missed_by_blocks}",
        f"- Block net impact: {summary.estimated_block_net_impact}",
        f"- Reduce size net impact: {summary.estimated_reduce_size_net_impact}",
        f"- Total shadow impact: {summary.estimated_total_shadow_impact}",
        "",
        "Classificação:",
        f"- Block would help: {summary.block_would_help_count}",
        f"- Block would hurt: {summary.block_would_hurt_count}",
        f"- Reduce size would help: {summary.reduce_size_would_help_count}",
        f"- Reduce size would hurt: {summary.reduce_size_would_hurt_count}",
    ]
    return "\n".join(lines) + "\n"


def export_learning_shadow_impact_summary(
    *,
    source_journal_path: str | Path,
    output_path: str | Path,
) -> str:
    summary = LearningShadowImpactAnalyzer().analyze_journal(source_journal_path)
    formatted = format_learning_shadow_impact_summary(summary)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(formatted, encoding="utf-8")
    return formatted


def _extract_shadow_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("event_type") != LEARNING_EXECUTION_SHADOW_EVENT_TYPE:
        return None

    data = event.get("data")
    return data if isinstance(data, dict) else None


def _extract_outcome_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("event_type") != OUTCOME_EVENT_TYPE:
        return None

    data = event.get("data")
    if not isinstance(data, dict):
        return None

    outcome = data.get("outcome")
    if isinstance(outcome, dict):
        return outcome

    if data.get("trade_plan_id") is not None or data.get("outcome_status") is not None:
        return data

    return None


def _outcomes_by_plan_id(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}

    for event in events:
        outcome = _extract_outcome_payload(event)
        if outcome is None:
            continue

        trade_plan_id = outcome.get("trade_plan_id")
        if trade_plan_id:
            outcomes[str(trade_plan_id)] = outcome

    return outcomes


def _build_impact_trade(
    *,
    shadow: dict[str, Any],
    outcome: dict[str, Any],
) -> LearningShadowImpactTrade:
    net_pnl = _decimal_or_zero(outcome.get("net_pnl"))
    status = str(shadow.get("status") or "")
    original_quantity = _optional_decimal(shadow.get("original_quantity"))
    shadow_quantity = _optional_decimal(shadow.get("shadow_quantity"))
    multiplier = _shadow_multiplier(
        original_quantity=original_quantity,
        shadow_quantity=shadow_quantity,
        fallback=shadow.get("suggested_size_multiplier"),
    )

    if status == LEARNING_EXECUTION_STATUS_WOULD_BLOCK:
        estimated_shadow_net_pnl = Decimal("0")
        impact_status = _impact_status_for_block(net_pnl)
    elif status == LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE:
        estimated_shadow_net_pnl = net_pnl * multiplier
        impact_status = _impact_status_for_reduce_size(net_pnl, estimated_shadow_net_pnl)
    else:
        estimated_shadow_net_pnl = net_pnl
        impact_status = IMPACT_NO_CHANGE

    estimated_delta = estimated_shadow_net_pnl - net_pnl

    return LearningShadowImpactTrade(
        symbol=str(outcome.get("symbol") or shadow.get("symbol") or ""),
        plan_id=str(shadow.get("plan_id") or outcome.get("trade_plan_id") or ""),
        shadow_status=status,
        suggested_action=str(shadow.get("suggested_action") or ""),
        outcome_status=str(outcome.get("outcome_status") or ""),
        net_pnl=net_pnl,
        estimated_shadow_net_pnl=estimated_shadow_net_pnl,
        estimated_pnl_delta=estimated_delta,
        impact_status=impact_status,
        would_block_trade=bool(shadow.get("would_block_trade", False)),
        would_reduce_size=bool(shadow.get("would_reduce_size", False)),
        original_quantity=original_quantity,
        shadow_quantity=shadow_quantity,
        diagnostics={
            "source_shadow_decision_id": shadow.get("decision_id"),
            "source_outcome_id": outcome.get("outcome_id"),
            "shadow_multiplier": str(multiplier),
        },
    )


def _build_summary(
    *,
    total_shadow_decisions: int,
    unmatched_shadow_decisions: int,
    matched_trades: list[LearningShadowImpactTrade],
) -> LearningShadowImpactSummary:
    allow_count = sum(
        1
        for trade in matched_trades
        if trade.shadow_status
        in {
            LEARNING_EXECUTION_STATUS_ALLOW,
            LEARNING_EXECUTION_STATUS_NO_GUARD,
            LEARNING_EXECUTION_STATUS_NO_PLAN,
        }
    )
    block_trades = [
        trade
        for trade in matched_trades
        if trade.shadow_status == LEARNING_EXECUTION_STATUS_WOULD_BLOCK
    ]
    reduce_trades = [
        trade
        for trade in matched_trades
        if trade.shadow_status == LEARNING_EXECUTION_STATUS_WOULD_REDUCE_SIZE
    ]

    loss_avoided = sum(
        (abs(trade.net_pnl) for trade in block_trades if trade.net_pnl < 0),
        Decimal("0"),
    )
    profit_missed = sum(
        (trade.net_pnl for trade in block_trades if trade.net_pnl > 0),
        Decimal("0"),
    )
    block_net_impact = sum(
        (trade.estimated_pnl_delta for trade in block_trades),
        Decimal("0"),
    )
    reduce_net_impact = sum(
        (trade.estimated_pnl_delta for trade in reduce_trades),
        Decimal("0"),
    )

    return LearningShadowImpactSummary(
        total_shadow_decisions=total_shadow_decisions,
        matched_outcomes=len(matched_trades),
        unmatched_shadow_decisions=unmatched_shadow_decisions,
        allow_count=allow_count,
        would_block_count=len(block_trades),
        would_reduce_size_count=len(reduce_trades),
        block_would_help_count=_count_impact(matched_trades, IMPACT_BLOCK_WOULD_HELP),
        block_would_hurt_count=_count_impact(matched_trades, IMPACT_BLOCK_WOULD_HURT),
        block_neutral_count=_count_impact(matched_trades, IMPACT_BLOCK_NEUTRAL),
        reduce_size_would_help_count=_count_impact(
            matched_trades,
            IMPACT_REDUCE_SIZE_WOULD_HELP,
        ),
        reduce_size_would_hurt_count=_count_impact(
            matched_trades,
            IMPACT_REDUCE_SIZE_WOULD_HURT,
        ),
        reduce_size_neutral_count=_count_impact(
            matched_trades,
            IMPACT_REDUCE_SIZE_NEUTRAL,
        ),
        estimated_loss_avoided_by_blocks=loss_avoided,
        estimated_profit_missed_by_blocks=profit_missed,
        estimated_block_net_impact=block_net_impact,
        estimated_reduce_size_net_impact=reduce_net_impact,
        estimated_total_shadow_impact=block_net_impact + reduce_net_impact,
        matched_trades=matched_trades,
        diagnostics={
            "shadow_event_type": LEARNING_EXECUTION_SHADOW_EVENT_TYPE,
            "outcome_event_type": OUTCOME_EVENT_TYPE,
            "summary_version": LEARNING_SHADOW_IMPACT_VERSION,
        },
    )


def _impact_status_for_block(net_pnl: Decimal) -> str:
    if net_pnl < 0:
        return IMPACT_BLOCK_WOULD_HELP

    if net_pnl > 0:
        return IMPACT_BLOCK_WOULD_HURT

    return IMPACT_BLOCK_NEUTRAL


def _impact_status_for_reduce_size(
    net_pnl: Decimal,
    estimated_shadow_net_pnl: Decimal,
) -> str:
    delta = estimated_shadow_net_pnl - net_pnl
    if delta > 0:
        return IMPACT_REDUCE_SIZE_WOULD_HELP

    if delta < 0:
        return IMPACT_REDUCE_SIZE_WOULD_HURT

    return IMPACT_REDUCE_SIZE_NEUTRAL


def _count_impact(trades: list[LearningShadowImpactTrade], impact_status: str) -> int:
    return sum(1 for trade in trades if trade.impact_status == impact_status)


def _shadow_multiplier(
    *,
    original_quantity: Decimal | None,
    shadow_quantity: Decimal | None,
    fallback,
) -> Decimal:
    if original_quantity is not None and shadow_quantity is not None:
        if original_quantity > 0:
            return shadow_quantity / original_quantity

    return _decimal_or_default(fallback, Decimal("1"))


def _optional_decimal(value) -> Decimal | None:
    if value is None:
        return None

    try:
        return Decimal(str(value))
    except Exception:
        return None


def _decimal_or_default(value, default: Decimal) -> Decimal:
    parsed = _optional_decimal(value)
    return parsed if parsed is not None else default


def _decimal_or_zero(value) -> Decimal:
    return _decimal_or_default(value, Decimal("0"))
