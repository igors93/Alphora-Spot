from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RequestBudgetDecision:
    allowed: bool
    reason: str
    symbol: str
    priority: str
    action: str
    position_open: bool
    fast_lane: bool
    used_heavy_checks: int
    max_heavy_checks: int
    override: bool = False
    category: str = ""
    category_used_heavy_checks: int = 0
    category_max_heavy_checks: int | None = None
    borrowed_budget: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "symbol": self.symbol,
            "priority": self.priority,
            "action": self.action,
            "position_open": self.position_open,
            "fast_lane": self.fast_lane,
            "used_heavy_checks": self.used_heavy_checks,
            "max_heavy_checks": self.max_heavy_checks,
            "override": self.override,
            "category": self.category,
            "category_used_heavy_checks": self.category_used_heavy_checks,
            "category_max_heavy_checks": self.category_max_heavy_checks,
            "borrowed_budget": self.borrowed_budget,
        }


class RequestBudget:
    def __init__(
        self,
        *,
        max_heavy_checks_per_cycle: int = 3,
        open_position_override: bool = True,
        max_heavy_checks_by_category: dict[str, int] | None = None,
        allow_budget_borrowing: bool = True,
    ):
        self.max_heavy_checks_per_cycle = max(0, int(max_heavy_checks_per_cycle))
        self.open_position_override = bool(open_position_override)
        self.max_heavy_checks_by_category = _normalize_category_budget(max_heavy_checks_by_category)
        self.allow_budget_borrowing = bool(allow_budget_borrowing)
        self.used_heavy_checks = 0
        self.used_heavy_checks_by_category: dict[str, int] = {}
        self.overridden_heavy_checks = 0
        self.denied_heavy_checks = 0

    def evaluate_heavy_check(
        self,
        *,
        symbol: str,
        priority: str,
        action: str,
        position_open: bool,
        fast_lane: bool,
        category: str | None = None,
    ) -> RequestBudgetDecision:
        normalized_symbol = str(symbol or "").upper().strip()
        normalized_priority = str(priority or "").upper().strip()
        normalized_action = str(action or "").upper().strip()
        normalized_category = _resolve_category(
            category=category,
            priority=normalized_priority,
            position_open=position_open,
            fast_lane=fast_lane,
        )
        category_used = self.used_heavy_checks_by_category.get(normalized_category, 0)
        category_max = self.max_heavy_checks_by_category.get(normalized_category)

        if position_open and self.open_position_override:
            self.overridden_heavy_checks += 1
            return RequestBudgetDecision(
                allowed=True,
                reason="OPEN_POSITION_OVERRIDE",
                symbol=normalized_symbol,
                priority=normalized_priority,
                action=normalized_action,
                position_open=True,
                fast_lane=fast_lane,
                used_heavy_checks=self.used_heavy_checks,
                max_heavy_checks=self.max_heavy_checks_per_cycle,
                override=True,
                category=normalized_category,
                category_used_heavy_checks=category_used,
                category_max_heavy_checks=category_max,
            )

        if self.max_heavy_checks_per_cycle <= 0:
            self.denied_heavy_checks += 1
            return RequestBudgetDecision(
                allowed=False,
                reason="HEAVY_CHECK_BUDGET_DISABLED_OR_ZERO",
                symbol=normalized_symbol,
                priority=normalized_priority,
                action=normalized_action,
                position_open=position_open,
                fast_lane=fast_lane,
                used_heavy_checks=self.used_heavy_checks,
                max_heavy_checks=self.max_heavy_checks_per_cycle,
                category=normalized_category,
                category_used_heavy_checks=category_used,
                category_max_heavy_checks=category_max,
            )

        if self.used_heavy_checks >= self.max_heavy_checks_per_cycle:
            self.denied_heavy_checks += 1
            return RequestBudgetDecision(
                allowed=False,
                reason="HEAVY_CHECK_BUDGET_EXHAUSTED",
                symbol=normalized_symbol,
                priority=normalized_priority,
                action=normalized_action,
                position_open=position_open,
                fast_lane=fast_lane,
                used_heavy_checks=self.used_heavy_checks,
                max_heavy_checks=self.max_heavy_checks_per_cycle,
                category=normalized_category,
                category_used_heavy_checks=category_used,
                category_max_heavy_checks=category_max,
            )

        if category_max is not None and category_used >= category_max:
            if self.allow_budget_borrowing:
                self.used_heavy_checks += 1
                self.used_heavy_checks_by_category[normalized_category] = category_used + 1
                return RequestBudgetDecision(
                    allowed=True,
                    reason="HEAVY_CHECK_BUDGET_BORROWED",
                    symbol=normalized_symbol,
                    priority=normalized_priority,
                    action=normalized_action,
                    position_open=position_open,
                    fast_lane=fast_lane,
                    used_heavy_checks=self.used_heavy_checks,
                    max_heavy_checks=self.max_heavy_checks_per_cycle,
                    category=normalized_category,
                    category_used_heavy_checks=category_used + 1,
                    category_max_heavy_checks=category_max,
                    borrowed_budget=True,
                )
            self.denied_heavy_checks += 1
            return RequestBudgetDecision(
                allowed=False,
                reason="HEAVY_CHECK_CATEGORY_BUDGET_EXHAUSTED",
                symbol=normalized_symbol,
                priority=normalized_priority,
                action=normalized_action,
                position_open=position_open,
                fast_lane=fast_lane,
                used_heavy_checks=self.used_heavy_checks,
                max_heavy_checks=self.max_heavy_checks_per_cycle,
                category=normalized_category,
                category_used_heavy_checks=category_used,
                category_max_heavy_checks=category_max,
            )

        self.used_heavy_checks += 1
        self.used_heavy_checks_by_category[normalized_category] = category_used + 1
        return RequestBudgetDecision(
            allowed=True,
            reason="HEAVY_CHECK_BUDGET_GRANTED",
            symbol=normalized_symbol,
            priority=normalized_priority,
            action=normalized_action,
            position_open=position_open,
            fast_lane=fast_lane,
            used_heavy_checks=self.used_heavy_checks,
            max_heavy_checks=self.max_heavy_checks_per_cycle,
            category=normalized_category,
            category_used_heavy_checks=category_used + 1,
            category_max_heavy_checks=category_max,
        )

    def deny(
        self,
        *,
        reason: str,
        symbol: str,
        priority: str,
        action: str,
        position_open: bool,
        fast_lane: bool,
        category: str,
    ) -> RequestBudgetDecision:
        """Deny a heavy check for a reason outside the cycle budget (e.g. REST weight)."""

        normalized_category = str(category or "").upper().strip()
        self.denied_heavy_checks += 1
        return RequestBudgetDecision(
            allowed=False,
            reason=reason,
            symbol=str(symbol or "").upper().strip(),
            priority=str(priority or "").upper().strip(),
            action=str(action or "").upper().strip(),
            position_open=position_open,
            fast_lane=fast_lane,
            used_heavy_checks=self.used_heavy_checks,
            max_heavy_checks=self.max_heavy_checks_per_cycle,
            category=normalized_category,
            category_used_heavy_checks=self.used_heavy_checks_by_category.get(
                normalized_category, 0
            ),
            category_max_heavy_checks=self.max_heavy_checks_by_category.get(normalized_category),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "max_heavy_checks_per_cycle": self.max_heavy_checks_per_cycle,
            "used_heavy_checks": self.used_heavy_checks,
            "used_heavy_checks_by_category": dict(self.used_heavy_checks_by_category),
            "overridden_heavy_checks": self.overridden_heavy_checks,
            "denied_heavy_checks": self.denied_heavy_checks,
            "open_position_override": self.open_position_override,
            "max_heavy_checks_by_category": dict(self.max_heavy_checks_by_category),
            "allow_budget_borrowing": self.allow_budget_borrowing,
        }


def _normalize_category_budget(
    value: dict[str, int] | None,
) -> dict[str, int]:
    if not value:
        return {}
    normalized: dict[str, int] = {}
    for raw_key, raw_limit in value.items():
        key = str(raw_key or "").upper().strip()
        if not key:
            continue
        normalized[key] = max(0, int(raw_limit))
    return normalized


def _resolve_category(
    *,
    category: str | None,
    priority: str,
    position_open: bool,
    fast_lane: bool,
) -> str:
    normalized = str(category or "").upper().strip()
    if normalized:
        return normalized
    if position_open:
        return "OPEN_POSITION"
    if fast_lane:
        return "FAST_LANE"
    if priority in {"READY", "HOT", "WARM", "COLD"}:
        return priority
    return "COLD"
