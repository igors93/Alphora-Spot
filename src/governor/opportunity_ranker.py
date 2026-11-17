from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from scanner.signal_score import SignalPriority, normalize_priority

PRIORITY_BONUS = {
    SignalPriority.READY: 20,
    SignalPriority.HOT: 10,
    SignalPriority.WARM: 3,
    SignalPriority.COLD: 0,
}

QUARANTINE_PENALTY = {
    "WATCH": 5,
    "COOLDOWN": 15,
    "QUARANTINED": 30,
}


@dataclass(frozen=True)
class OpportunityScore:
    symbol: str
    base_score: int
    priority: str
    opportunity_score: int
    reasons: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    execution_changed: bool = False
    order_mutation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base_score": self.base_score,
            "priority": self.priority,
            "opportunity_score": self.opportunity_score,
            "reasons": list(self.reasons),
            "diagnostics": dict(self.diagnostics),
            "execution_changed": self.execution_changed,
            "order_mutation": self.order_mutation,
        }


class OpportunityRanker:
    def score(
        self,
        *,
        symbol: str,
        base_score: int | float | Decimal,
        priority: SignalPriority | str,
        fast_lane: bool = False,
        position_open: bool = False,
        quarantine_level: str = "NONE",
        group_has_exposure: bool = False,
    ) -> OpportunityScore:
        normalized_symbol = str(symbol or "").upper().strip()
        normalized_priority = normalize_priority(priority)
        normalized_quarantine = str(quarantine_level or "NONE").upper().strip()
        score = max(0, int(base_score or 0))
        reasons: list[str] = ["BASE_SCORE"]
        diagnostics: dict[str, Any] = {
            "priority_bonus": 0,
            "fast_lane_bonus": 0,
            "position_open_bonus": 0,
            "quarantine_penalty": 0,
            "group_exposure_penalty": 0,
            "quarantine_level": normalized_quarantine,
            "group_has_exposure": bool(group_has_exposure),
        }

        priority_bonus = PRIORITY_BONUS[normalized_priority]
        score += priority_bonus
        diagnostics["priority_bonus"] = priority_bonus
        if priority_bonus:
            reasons.append(f"PRIORITY_{normalized_priority.value}")

        if fast_lane:
            score += 8
            diagnostics["fast_lane_bonus"] = 8
            reasons.append("FAST_LANE")

        if position_open:
            score += 40
            diagnostics["position_open_bonus"] = 40
            reasons.append("OPEN_POSITION_OPERATIONAL_ATTENTION")

        quarantine_penalty = QUARANTINE_PENALTY.get(normalized_quarantine, 0)
        if quarantine_penalty:
            score -= quarantine_penalty
            diagnostics["quarantine_penalty"] = quarantine_penalty
            reasons.append(f"QUARANTINE_{normalized_quarantine}")

        if group_has_exposure:
            score -= 5
            diagnostics["group_exposure_penalty"] = 5
            reasons.append("CORRELATION_GROUP_HAS_EXPOSURE")

        return OpportunityScore(
            symbol=normalized_symbol,
            base_score=max(0, int(base_score or 0)),
            priority=normalized_priority.value,
            opportunity_score=max(0, int(score)),
            reasons=reasons,
            diagnostics=diagnostics,
        )

    def rank(
        self,
        items,
        *,
        fast_lane_symbols: set[str] | None = None,
        open_symbols: set[str] | None = None,
        quarantine_levels: dict[str, str] | None = None,
        symbols_with_group_exposure: set[str] | None = None,
    ) -> list[OpportunityScore]:
        fast_lane_symbols = fast_lane_symbols or set()
        open_symbols = open_symbols or set()
        quarantine_levels = quarantine_levels or {}
        symbols_with_group_exposure = symbols_with_group_exposure or set()
        scores = [
            self.score(
                symbol=item.symbol,
                base_score=item.score,
                priority=item.priority,
                fast_lane=item.symbol in fast_lane_symbols,
                position_open=item.symbol in open_symbols,
                quarantine_level=quarantine_levels.get(item.symbol, "NONE"),
                group_has_exposure=item.symbol in symbols_with_group_exposure,
            )
            for item in items
        ]
        return sorted(
            scores,
            key=lambda item: (item.opportunity_score, item.base_score, item.symbol),
            reverse=True,
        )
