from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from futures.planning._math import BPS, decimal_or_none, decimal_or_none_from_any
from futures.planning.exit_planner import ECONOMIC_EXIT_BLOCK_REASONS
from trading.enums import OPEN_ACTIONS, TradeAction
from trading.plan import TradePlan

EXECUTABLE_TRADE_PLAN_STATUSES = {
    "ALLOW_MIN_SIZE",
    "ALLOW_REDUCED",
    "ALLOW_NORMAL",
    "ALLOW_AGGRESSIVE",
}


@dataclass(frozen=True)
class EconomicGateDecision:
    allowed: bool
    reason: str
    plan_id: str | None
    symbol: str | None
    action: str
    expected_net_gain: Decimal | None
    expected_net_gain_bps: Decimal | None
    expected_net_loss: Decimal | None
    reward_risk: Decimal | None
    round_trip_cost: Decimal | None
    round_trip_cost_bps: Decimal | None
    minimum_profitable_move: Decimal | None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class EconomicTradeGate:
    """Mandatory economic viability gate for new entries."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        min_net_gain_bps: Decimal = Decimal("5"),
        min_net_reward_risk: Decimal = Decimal("1.5"),
        max_round_trip_cost_bps: Decimal = Decimal("20"),
        require_profitable_trade_plan: bool = True,
    ):
        self.enabled = bool(enabled)
        self.min_net_gain_bps = Decimal(str(min_net_gain_bps))
        self.min_net_reward_risk = Decimal(str(min_net_reward_risk))
        self.max_round_trip_cost_bps = Decimal(str(max_round_trip_cost_bps))
        self.require_profitable_trade_plan = bool(require_profitable_trade_plan)

    @classmethod
    def from_config(cls, config) -> "EconomicTradeGate":
        return cls(
            enabled=getattr(config, "economic_gate_enabled", True),
            min_net_gain_bps=Decimal(str(getattr(config, "min_net_gain_bps", "5"))),
            min_net_reward_risk=Decimal(str(getattr(config, "min_net_reward_risk", "1.5"))),
            max_round_trip_cost_bps=Decimal(str(getattr(config, "max_round_trip_cost_bps", "20"))),
            require_profitable_trade_plan=getattr(
                config,
                "require_profitable_trade_plan",
                True,
            ),
        )

    def evaluate(
        self,
        plan: TradePlan | None,
        action: str,
    ) -> EconomicGateDecision:
        normalized_action = str(action or "").upper().strip()

        if normalized_action == TradeAction.CLOSE_POSITION.value:
            # Closing reduces operational risk; it must never be blocked by the economic gate.
            return self._decision(
                allowed=True,
                reason="CLOSE_POSITION_BYPASSES_ECONOMIC_GATE",
                plan=plan,
                action=normalized_action,
                diagnostics={"close_position_bypass": True},
            )

        if normalized_action not in OPEN_ACTIONS:
            return self._decision(
                allowed=True,
                reason="ACTION_NOT_OPEN",
                plan=plan,
                action=normalized_action,
            )

        if not self.enabled:
            return self._decision(
                allowed=True,
                reason="ECONOMIC_GATE_DISABLED",
                plan=plan,
                action=normalized_action,
            )

        if plan is None:
            allowed = not self.require_profitable_trade_plan
            return self._decision(
                allowed=allowed,
                reason=(
                    "TRADE_PLAN_REQUIRED"
                    if not allowed
                    else "TRADE_PLAN_ABSENT_REQUIREMENT_DISABLED"
                ),
                plan=None,
                action=normalized_action,
                diagnostics=self._base_diagnostics({"plan_present": False}),
            )

        diagnostics = self._plan_diagnostics(plan)
        failures = self._failures(plan, normalized_action, diagnostics)
        allowed = not failures
        reason = "ECONOMIC_GATE_APPROVED" if allowed else failures[0]
        diagnostics["failures"] = failures

        return self._decision(
            allowed=allowed,
            reason=reason,
            plan=plan,
            action=normalized_action,
            diagnostics=diagnostics,
        )

    def _failures(
        self,
        plan: TradePlan,
        action: str,
        diagnostics: dict[str, Any],
    ) -> list[str]:
        failures: list[str] = []
        entry = plan.entry_reference_price
        quantity = abs(plan.quantity)
        expected_net_gain_bps = _value_bps(plan.expected_net_gain, plan.notional)
        round_trip_cost_bps = _round_trip_cost_bps(plan)
        gross_take = _gross_take(plan)

        if plan.action != action:
            failures.append("PLAN_ACTION_MISMATCH")
        if plan.status not in EXECUTABLE_TRADE_PLAN_STATUSES:
            if plan.block_reason in ECONOMIC_EXIT_BLOCK_REASONS:
                failures.append(str(plan.block_reason))
            else:
                failures.append("PLAN_STATUS_NOT_EXECUTABLE")
        if plan.expected_net_gain is None:
            failures.append("EXPECTED_NET_GAIN_MISSING")
        elif plan.expected_net_gain <= 0:
            failures.append("EXPECTED_NET_GAIN_NOT_POSITIVE")

        if expected_net_gain_bps is None:
            failures.append("EXPECTED_NET_GAIN_BPS_MISSING")
        elif expected_net_gain_bps < self.min_net_gain_bps:
            failures.append("EXPECTED_NET_GAIN_BPS_BELOW_MINIMUM")

        if plan.expected_net_loss is None or plan.expected_net_loss <= 0:
            failures.append("EXPECTED_NET_LOSS_INVALID")

        if plan.reward_risk is None:
            failures.append("REWARD_RISK_MISSING")
        elif plan.reward_risk < self.min_net_reward_risk:
            failures.append("REWARD_RISK_BELOW_MINIMUM")

        if round_trip_cost_bps is None:
            failures.append("ROUND_TRIP_COST_BPS_MISSING")
        elif round_trip_cost_bps > self.max_round_trip_cost_bps:
            failures.append("ROUND_TRIP_COST_BPS_ABOVE_MAXIMUM")

        if plan.stop_price is None:
            failures.append("STOP_PRICE_MISSING")
        if plan.take_profit_price is None:
            failures.append("TAKE_PROFIT_PRICE_MISSING")

        if entry <= 0:
            failures.append("ENTRY_REFERENCE_PRICE_INVALID")
        if quantity <= 0:
            failures.append("QUANTITY_INVALID")

        if plan.stop_price is not None and entry > 0:
            if action == TradeAction.OPEN_LONG.value and plan.stop_price >= entry:
                failures.append("STOP_PRICE_WRONG_SIDE")
            if action == TradeAction.OPEN_SHORT.value and plan.stop_price <= entry:
                failures.append("STOP_PRICE_WRONG_SIDE")

        if plan.take_profit_price is not None and entry > 0:
            if action == TradeAction.OPEN_LONG.value and plan.take_profit_price <= entry:
                failures.append("TAKE_PROFIT_PRICE_WRONG_SIDE")
            if action == TradeAction.OPEN_SHORT.value and plan.take_profit_price >= entry:
                failures.append("TAKE_PROFIT_PRICE_WRONG_SIDE")

        if gross_take is None or gross_take <= 0:
            failures.append("GROSS_TAKE_INVALID")
        elif plan.estimated_round_trip_cost >= gross_take:
            failures.append("GROSS_TAKE_DOES_NOT_COVER_COSTS")

        if plan.expected_net_gain is not None and gross_take is not None:
            expected_from_components = gross_take - plan.estimated_round_trip_cost
            if expected_from_components <= 0:
                failures.append("NET_TAKE_AFTER_COSTS_NOT_PROFITABLE")

        return _unique(failures)

    def _plan_diagnostics(self, plan: TradePlan) -> dict[str, Any]:
        expected_net_gain_bps = _value_bps(plan.expected_net_gain, plan.notional)
        round_trip_cost_bps = _round_trip_cost_bps(plan)
        gross_take = _gross_take(plan)
        gross_loss = _gross_loss(plan)
        minimum_profitable_move = _minimum_profitable_move(plan)
        cost_diagnostics = (
            plan.diagnostics.get("cost", {}) if isinstance(plan.diagnostics, dict) else {}
        )
        cost_summary = (
            plan.diagnostics.get("cost_summary", {}) if isinstance(plan.diagnostics, dict) else {}
        )
        exit_diagnostics = (
            plan.diagnostics.get("exit", {}) if isinstance(plan.diagnostics, dict) else {}
        )

        return self._base_diagnostics(
            {
                "plan_present": True,
                "plan_status": plan.status,
                "plan_block_reason": plan.block_reason,
                "thresholds": {
                    "min_net_gain_bps": str(self.min_net_gain_bps),
                    "min_net_reward_risk": str(self.min_net_reward_risk),
                    "max_round_trip_cost_bps": str(self.max_round_trip_cost_bps),
                    "require_profitable_trade_plan": self.require_profitable_trade_plan,
                },
                "metrics": {
                    "entry_reference_price": str(plan.entry_reference_price),
                    "quantity": str(plan.quantity),
                    "notional": str(plan.notional),
                    "gross_take": decimal_or_none(gross_take),
                    "gross_loss": decimal_or_none(gross_loss),
                    "expected_net_gain_bps": decimal_or_none(expected_net_gain_bps),
                    "round_trip_cost_bps": decimal_or_none(round_trip_cost_bps),
                    "minimum_profitable_move": decimal_or_none(minimum_profitable_move),
                },
                "cost_components": {
                    "entry_fee": str(plan.estimated_entry_fee),
                    "exit_fee": str(plan.estimated_exit_fee),
                    "spread_cost": str(plan.estimated_spread_cost),
                    "slippage_cost": str(plan.estimated_slippage_cost),
                    "funding_cost": str(plan.estimated_funding_cost),
                    "round_trip_cost": str(plan.estimated_round_trip_cost),
                },
                "cost_diagnostics": cost_diagnostics,
                "cost_summary": cost_summary,
                "exit_diagnostics": exit_diagnostics,
                # Shadow (OPP-008): the take-first probability above which the plan has a
                # positive expected value; recorded, not yet used to approve or block.
                "outcome_costs": (
                    plan.diagnostics.get("outcome_costs", {})
                    if isinstance(plan.diagnostics, dict)
                    else {}
                ),
            }
        )

    def _base_diagnostics(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        diagnostics = {
            "economic_gate_enabled": self.enabled,
            "gate_version": "1.0.0",
        }
        diagnostics.update(extra or {})
        return diagnostics

    def _decision(
        self,
        *,
        allowed: bool,
        reason: str,
        plan: TradePlan | None,
        action: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> EconomicGateDecision:
        return EconomicGateDecision(
            allowed=allowed,
            reason=reason,
            plan_id=plan.plan_id if plan is not None else None,
            symbol=plan.symbol if plan is not None else None,
            action=action,
            expected_net_gain=plan.expected_net_gain if plan is not None else None,
            expected_net_gain_bps=(
                _value_bps(plan.expected_net_gain, plan.notional) if plan is not None else None
            ),
            expected_net_loss=plan.expected_net_loss if plan is not None else None,
            reward_risk=plan.reward_risk if plan is not None else None,
            round_trip_cost=(plan.estimated_round_trip_cost if plan is not None else None),
            round_trip_cost_bps=(_round_trip_cost_bps(plan) if plan is not None else None),
            minimum_profitable_move=(_minimum_profitable_move(plan) if plan is not None else None),
            diagnostics=diagnostics or self._base_diagnostics(),
        )


def _gross_take(plan: TradePlan) -> Decimal | None:
    if plan.take_profit_price is None:
        return None
    return abs(plan.take_profit_price - plan.entry_reference_price) * abs(plan.quantity)


def _gross_loss(plan: TradePlan) -> Decimal | None:
    if plan.stop_price is None:
        return None
    return abs(plan.entry_reference_price - plan.stop_price) * abs(plan.quantity)


def _round_trip_cost_bps(plan: TradePlan) -> Decimal | None:
    diagnostics = plan.diagnostics if isinstance(plan.diagnostics, dict) else {}
    for path in (
        ("cost_summary", "round_trip_cost_bps"),
        ("cost", "round_trip_cost_bps"),
    ):
        current: Any = diagnostics
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        parsed = decimal_or_none_from_any(current)
        if parsed is not None:
            return parsed
    return _value_bps(plan.estimated_round_trip_cost, plan.notional)


def _minimum_profitable_move(plan: TradePlan) -> Decimal | None:
    diagnostics = plan.diagnostics if isinstance(plan.diagnostics, dict) else {}
    for path in (
        ("cost_summary", "minimum_profitable_move"),
        ("cost", "minimum_profitable_move"),
    ):
        current: Any = diagnostics
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        parsed = decimal_or_none_from_any(current)
        if parsed is not None:
            return parsed
    if plan.quantity <= 0:
        return None
    return plan.estimated_round_trip_cost / abs(plan.quantity)


def _value_bps(value: Decimal | None, notional: Decimal) -> Decimal | None:
    if value is None or notional <= 0:
        return None
    return value / abs(notional) * BPS


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
