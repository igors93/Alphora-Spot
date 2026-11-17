from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from config.symbol_context import SymbolTradingConfig
from domain.models import FuturesPosition, TradeAction
from domain.symbol_rules import SymbolRules
from futures.risk_guard import FuturesRiskGuard
from futures.risk_manager import FuturesRiskManager, TradeNotExecutableError
from governor.market_data_router import HeavyMarketSnapshot
from scanner.signal_score import SignalPriority, SignalScore
from trading.intent import TradeIntent


class GuardOutcome(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    DEFER = "DEFER"
    FORCE_CLOSE = "FORCE_CLOSE"


@dataclass(frozen=True)
class OperationalDirective:
    action: TradeAction
    reason: str
    source: str
    parent_intent_id: str | None = None


@dataclass(frozen=True)
class GuardDecision:
    outcome: GuardOutcome
    reason: str
    checks: Mapping[str, Any] = field(default_factory=dict)
    directive: OperationalDirective | None = None


class HotSymbolWorker:
    """Deep validation for symbols that deserve a heavy check.

    This worker never mutates exchange state. It only confirms whether a scored
    symbol deserves portfolio risk evaluation and later execution.
    """

    def evaluate(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        score: SignalScore,
        intent: TradeIntent,
        heavy_snapshot: HeavyMarketSnapshot,
        paused: bool = False,
        risk_guard: FuturesRiskGuard | None = None,
    ) -> GuardDecision:
        checks: dict[str, Any] = {
            "intent_id": intent.intent_id,
            "score": score.score,
            "priority": score.priority.value if score.priority is not None else None,
            "data_age_seconds": heavy_snapshot.data_age_seconds,
        }
        if paused:
            return _decision(GuardOutcome.BLOCK, "SYMBOL_PAUSED", checks=checks)

        if heavy_snapshot.stale:
            return _decision(GuardOutcome.BLOCK, "STALE_DATA", checks=checks)

        rules = heavy_snapshot.exchange_rules
        if rules is None:
            return _decision(GuardOutcome.BLOCK, "SYMBOL_RULES_FAILED", checks=checks)

        position = heavy_snapshot.position
        if (
            position.is_open
            and risk_guard is not None
            and risk_guard.should_close_for_max_hold(holding_seconds=position.holding_seconds)
        ):
            checks["risk_guard_close"] = "max_position_hold_seconds"
            return _decision(
                GuardOutcome.FORCE_CLOSE,
                "RISK_GUARD_CLOSE_MAX_HOLD",
                checks=checks,
                directive=OperationalDirective(
                    action=TradeAction.CLOSE_POSITION,
                    reason="RISK_GUARD_CLOSE_MAX_HOLD",
                    source="HotSymbolWorker",
                    parent_intent_id=intent.intent_id,
                ),
            )

        action = intent.action
        checks["strategy_decision"] = action
        checks["strategy_reason"] = intent.reason

        if action == TradeAction.HOLD.value:
            return _decision(
                GuardOutcome.DEFER,
                "STRATEGY_HOLD",
                checks=checks,
            )

        if action in {TradeAction.OPEN_LONG.value, TradeAction.OPEN_SHORT.value}:
            if not _open_attention_confirmed(score, action):
                checks["open_attention_policy"] = "HOT_OR_READY_OR_SCORE_ACTION"
                return _decision(
                    GuardOutcome.DEFER,
                    "OPEN_ATTENTION_NOT_CONFIRMED",
                    checks=checks,
                )
            block = self._validate_open_intent(
                symbol_config=symbol_config,
                heavy_snapshot=heavy_snapshot,
                rules=rules,
                position=position,
                risk_guard=risk_guard,
                checks=checks,
            )
            if block is not None:
                return block

        elif action == TradeAction.CLOSE_POSITION.value:
            if not position.is_open:
                return _decision(GuardOutcome.BLOCK, "CLOSE_WITHOUT_POSITION", checks=checks)

        else:
            return _decision(GuardOutcome.BLOCK, "UNSUPPORTED_STRATEGY_ACTION", checks=checks)

        return _decision(GuardOutcome.ALLOW, "HOT_CHECK_PASSED", checks=checks)

    def _validate_open_intent(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        rules: SymbolRules,
        position: FuturesPosition,
        risk_guard: FuturesRiskGuard | None,
        checks: dict[str, Any],
    ) -> GuardDecision | None:
        if position.is_open:
            return _decision(GuardOutcome.BLOCK, "POSITION_ALREADY_OPEN", checks=checks)

        if heavy_snapshot.open_orders or heavy_snapshot.open_algo_orders:
            checks["open_orders_count"] = len(heavy_snapshot.open_orders)
            checks["open_algo_orders_count"] = len(heavy_snapshot.open_algo_orders)
            checks["open_order_conflict_policy"] = "BLOCK_ANY_OPEN_ORDER"
            return _decision(GuardOutcome.BLOCK, "OPEN_ORDER_CONFLICT", checks=checks)

        risk_manager = FuturesRiskManager(
            position_notional_usdt=symbol_config.position_notional_usdt,
            stop_loss_pct=symbol_config.stop_loss_pct,
            take_profit_pct=symbol_config.take_profit_pct,
        )
        try:
            quantity = risk_manager.calculate_quantity(
                heavy_snapshot.market.price,
                rules,
            )
        except TradeNotExecutableError as error:
            checks["quantity_error"] = str(error)
            return _decision(GuardOutcome.BLOCK, "MIN_NOTIONAL", checks=checks)

        notional = quantity * heavy_snapshot.market.price
        checks["quantity"] = str(quantity)
        checks["notional"] = str(notional)

        if risk_guard is not None:
            decision = risk_guard.can_open_position(notional=notional)
            checks["risk_guard"] = {
                "allowed": decision.allowed,
                "reason": decision.reason,
                "metrics": decision.metrics,
            }
            if not decision.allowed:
                return _decision(
                    GuardOutcome.BLOCK,
                    "SYMBOL_RISK_GUARD_BLOCKED",
                    checks=checks,
                )

        return None


def _open_attention_confirmed(score: SignalScore, action: str) -> bool:
    probable_action = str(score.action or "").upper()
    return score.priority in {SignalPriority.HOT, SignalPriority.READY} or probable_action == action


def _decision(
    outcome: GuardOutcome,
    reason: str,
    *,
    checks: dict[str, Any],
    directive: OperationalDirective | None = None,
) -> GuardDecision:
    return GuardDecision(
        outcome=outcome,
        reason=reason,
        checks=MappingProxyType(dict(checks)),
        directive=directive,
    )
