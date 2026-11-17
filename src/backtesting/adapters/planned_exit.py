"""Historical output adapter for the shared strategy exit validation rule."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from backtesting.domain.config import BacktestSymbolConfig, CandleEvent
from domain.models import TradeAction
from futures.planned_exit import (
    GLOBAL_FALLBACK_EXIT_SOURCE,
    INVALID_STRATEGY_PLANNED_EXIT,
    STRATEGY_METADATA_EXIT_SOURCE,
    StrategyPlannedExitCandidate,
    resolve_strategy_planned_exit,
)
from scanner.signal_score import SignalScore
from utils.events import EventChannel, observe_event


class HistoricalPlannedExitPort:
    def __init__(
        self,
        symbol_configs: dict[str, BacktestSymbolConfig],
        *,
        price_for_event: Any,
    ) -> None:
        self.symbol_configs = symbol_configs
        self.price_for_event = price_for_event

    def resolve(
        self, *, score: SignalScore, action: TradeAction, event: CandleEvent
    ) -> dict[str, Any]:
        metadata = score.metadata if isinstance(score.metadata, dict) else {}
        present, extracted, validated = resolve_strategy_planned_exit(
            metadata,
            action=action,
            entry_price=Decimal(str(self.price_for_event(event))),
            price_tick=self.symbol_configs[event.symbol].rules.price_tick,
        )
        if present:
            observe_event(
                "strategy_planned_exit_detected",
                component=__name__,
                category="backtesting",
                channel=EventChannel.AUDIT,
                symbol=event.symbol,
                action=action.value,
                stop_price=extracted.stop_price,
                take_profit_price=extracted.take_profit_price,
                valid=validated.valid,
                rejection_reason=validated.rejection_reason,
            )
        if present and validated.valid:
            observe_event(
                "dynamic_exit_used",
                component=__name__,
                category="backtesting",
                channel=EventChannel.AUDIT,
                symbol=event.symbol,
                action=action.value,
                stop_price=validated.stop_price,
                take_profit_price=validated.take_profit_price,
                reward_risk_liquido=validated.reward_risk_liquido,
            )
            return _payload(
                validated,
                extracted,
                dynamic_exit_used=True,
                exit_source=STRATEGY_METADATA_EXIT_SOURCE,
                rejection_reason=None,
            )
        rejection_reason = validated.rejection_reason if present else None
        if present:
            observe_event(
                "dynamic_exit_fallback_used",
                component=__name__,
                category="backtesting",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                symbol=event.symbol,
                action=action.value,
                reason=rejection_reason or INVALID_STRATEGY_PLANNED_EXIT,
            )
        return _payload(
            extracted,
            extracted,
            dynamic_exit_used=False,
            exit_source=GLOBAL_FALLBACK_EXIT_SOURCE,
            rejection_reason=rejection_reason,
        )


def _payload(
    candidate: StrategyPlannedExitCandidate,
    recommended: StrategyPlannedExitCandidate,
    *,
    dynamic_exit_used: bool,
    exit_source: str,
    rejection_reason: str | None,
) -> dict[str, Any]:
    return {
        "dynamic_exit_used": dynamic_exit_used,
        "exit_source": exit_source,
        "stop_price": candidate.stop_price,
        "take_profit_price": candidate.take_profit_price,
        "risk_abs": candidate.risk_abs,
        "reward_abs": candidate.reward_abs,
        "recommended_stop_price": recommended.stop_price,
        "recommended_take_profit_price": recommended.take_profit_price,
        "reward_risk_liquido": candidate.reward_risk_liquido,
        "raw_reward_risk": candidate.raw_reward_risk,
        "cost_to_reward_ratio": candidate.cost_to_reward_ratio,
        "risk_pct": candidate.risk_pct,
        "reward_pct": candidate.reward_pct,
        "planned_exit_rejection_reason": rejection_reason,
        "diagnostics": dict(candidate.diagnostics),
    }
