from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from domain.symbol_rules import SymbolRules
from futures.costs.model import CostEstimate
from futures.planning._math import decimal_or_none, decimal_or_none_from_any
from futures.planning.economic_exit_math import (
    BPS,
    EconomicExitProjection,
    calculate_economic_exit,
)
from indicators.atr import average_true_range
from trading.context import TradeContext
from trading.enums import TradeAction
from trading.intent import TradeIntent

if TYPE_CHECKING:
    from config import AppConfig

ECONOMIC_TAKE_REQUIRED_TOO_FAR = "ECONOMIC_TAKE_REQUIRED_TOO_FAR"
ECONOMIC_EXIT_NOT_REALISTIC = "ECONOMIC_EXIT_NOT_REALISTIC"
ECONOMIC_EXIT_BLOCK_REASONS = {
    ECONOMIC_TAKE_REQUIRED_TOO_FAR,
    ECONOMIC_EXIT_NOT_REALISTIC,
}


@dataclass(frozen=True)
class ExitPlan:
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    expected_net_gain: Decimal | None
    expected_net_loss: Decimal | None
    reward_risk: Decimal | None
    block_reason: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _BaseExitPrices:
    stop_price: Decimal
    take_profit_price: Decimal
    exit_model_requested: str
    exit_model_effective: str
    atr_value: Decimal | None
    atr_available: bool
    atr_fallback_reason: str | None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class ExitPlanner:
    def __init__(
        self,
        *,
        exit_model: str = "FIXED_PCT",
        stop_loss_pct: Decimal = Decimal("0.01"),
        take_profit_pct: Decimal = Decimal("0.02"),
        atr_period: int = 14,
        atr_stop_multiplier: Decimal = Decimal("1.5"),
        atr_take_multiplier: Decimal = Decimal("2.0"),
        min_net_gain_bps: Decimal = Decimal("5"),
        min_net_reward_risk: Decimal = Decimal("1.5"),
        max_economic_take_bps: Decimal = Decimal("120"),
        max_economic_take_atr_multiplier: Decimal = Decimal("3.0"),
        economic_exit_adjustment_enabled: bool = True,
        economic_exit_diagnostics_enabled: bool = True,
    ):
        self.exit_model = _normalize_exit_model(exit_model)
        self.stop_loss_pct = Decimal(str(stop_loss_pct))
        self.take_profit_pct = Decimal(str(take_profit_pct))
        self.atr_period = int(atr_period)
        self.atr_stop_multiplier = Decimal(str(atr_stop_multiplier))
        self.atr_take_multiplier = Decimal(str(atr_take_multiplier))
        self.min_net_gain_bps = Decimal(str(min_net_gain_bps))
        self.min_net_reward_risk = Decimal(str(min_net_reward_risk))
        self.max_economic_take_bps = Decimal(str(max_economic_take_bps))
        self.max_economic_take_atr_multiplier = Decimal(str(max_economic_take_atr_multiplier))
        self.economic_exit_adjustment_enabled = bool(economic_exit_adjustment_enabled)
        self.economic_exit_diagnostics_enabled = bool(economic_exit_diagnostics_enabled)

    @classmethod
    def from_config(cls, config: "AppConfig") -> "ExitPlanner":
        return cls(
            exit_model=_config_value(config, "exit_model", "FIXED_PCT"),
            stop_loss_pct=_config_decimal(config, "stop_loss_pct", "0.01"),
            take_profit_pct=_config_decimal(config, "take_profit_pct", "0.02"),
            atr_period=int(getattr(config, "atr_period", 14)),
            atr_stop_multiplier=_config_decimal(
                config,
                "atr_stop_multiplier",
                "1.5",
            ),
            atr_take_multiplier=_config_decimal(
                config,
                "atr_take_multiplier",
                "2.0",
            ),
            min_net_gain_bps=_config_decimal(config, "min_net_gain_bps", "5"),
            min_net_reward_risk=_config_decimal(
                config,
                "min_net_reward_risk",
                "1.5",
            ),
            max_economic_take_bps=_config_decimal(
                config,
                "max_economic_take_bps",
                "120",
            ),
            max_economic_take_atr_multiplier=_config_decimal(
                config,
                "max_economic_take_atr_multiplier",
                "3.0",
            ),
            economic_exit_adjustment_enabled=bool(
                getattr(config, "economic_exit_adjustment_enabled", True)
            ),
            economic_exit_diagnostics_enabled=bool(
                getattr(config, "economic_exit_diagnostics_enabled", True)
            ),
        )

    def plan(
        self,
        *,
        intent: TradeIntent,
        context: TradeContext,
        quantity: Decimal,
        cost: CostEstimate,
    ) -> ExitPlan:
        if intent.action not in {TradeAction.OPEN_LONG.value, TradeAction.OPEN_SHORT.value}:
            return ExitPlan(
                stop_price=None,
                take_profit_price=None,
                expected_net_gain=None,
                expected_net_loss=None,
                reward_risk=None,
                diagnostics={"reason": "exit_plan_not_required_for_action"},
            )

        entry = Decimal(str(context.market_price))
        qty = abs(Decimal(str(quantity)))
        if entry <= 0 or qty <= 0:
            return ExitPlan(
                stop_price=None,
                take_profit_price=None,
                expected_net_gain=None,
                expected_net_loss=None,
                reward_risk=None,
                block_reason="EXIT_PLAN_INVALID_INPUT",
                diagnostics={
                    "model": "economic_aware",
                    "block_reason": "EXIT_PLAN_INVALID_INPUT",
                    "entry_reference_price": str(entry),
                    "quantity": str(qty),
                },
            )

        base = self._base_exit_prices(
            action=intent.action,
            context=context,
            entry=entry,
        )
        max_allowed_distance, max_allowed_diagnostics = self._max_allowed_take_distance(
            entry=entry,
            atr_value=base.atr_value,
        )
        projection = calculate_economic_exit(
            action=intent.action,
            entry_price=entry,
            quantity=qty,
            gross_stop_distance=abs(entry - base.stop_price),
            gross_take_distance_configured=abs(base.take_profit_price - entry),
            round_trip_cost=cost.round_trip_cost,
            min_net_gain_bps=self.min_net_gain_bps,
            min_net_reward_risk=self.min_net_reward_risk,
            max_allowed_take_distance=max_allowed_distance,
            adjustment_enabled=self.economic_exit_adjustment_enabled,
        )

        if projection.blocked:
            diagnostics = self._diagnostics(
                base=base,
                projection=projection,
                final_projection=projection,
                cost=cost,
                max_allowed_diagnostics=max_allowed_diagnostics,
                final_take_price=None,
                adjusted=False,
                block_reason=projection.block_reason,
            )
            return ExitPlan(
                stop_price=base.stop_price,
                take_profit_price=None,
                expected_net_gain=projection.expected_net_gain,
                expected_net_loss=projection.expected_net_loss,
                reward_risk=projection.reward_risk,
                block_reason=projection.block_reason,
                diagnostics=diagnostics,
            )

        final_take = _round_take_price(
            rules=context.exchange_rules,
            action=intent.action,
            price=projection.required_take_price,
        )
        final_projection = calculate_economic_exit(
            action=intent.action,
            entry_price=entry,
            quantity=qty,
            gross_stop_distance=abs(entry - base.stop_price),
            gross_take_distance_configured=abs(final_take - entry),
            round_trip_cost=cost.round_trip_cost,
            min_net_gain_bps=self.min_net_gain_bps,
            min_net_reward_risk=self.min_net_reward_risk,
            adjustment_enabled=False,
        )
        adjusted = self.economic_exit_adjustment_enabled and final_take != base.take_profit_price
        diagnostics = self._diagnostics(
            base=base,
            projection=projection,
            final_projection=final_projection,
            cost=cost,
            max_allowed_diagnostics=max_allowed_diagnostics,
            final_take_price=final_take,
            adjusted=adjusted,
            block_reason=None,
        )

        return ExitPlan(
            stop_price=base.stop_price,
            take_profit_price=final_take,
            expected_net_gain=final_projection.expected_net_gain,
            expected_net_loss=final_projection.expected_net_loss,
            reward_risk=final_projection.reward_risk,
            diagnostics=diagnostics,
        )

    def _base_exit_prices(
        self,
        *,
        action: str,
        context: TradeContext,
        entry: Decimal,
    ) -> _BaseExitPrices:
        requested = _normalize_exit_model(
            getattr(context.symbol_config, "exit_model", self.exit_model)
        )
        stop_pct = Decimal(str(getattr(context.symbol_config, "stop_loss_pct", self.stop_loss_pct)))
        take_pct = Decimal(
            str(getattr(context.symbol_config, "take_profit_pct", self.take_profit_pct))
        )
        atr_period = int(getattr(context.symbol_config, "atr_period", self.atr_period))
        atr_stop_multiplier = Decimal(
            str(
                getattr(
                    context.symbol_config,
                    "atr_stop_multiplier",
                    self.atr_stop_multiplier,
                )
            )
        )
        atr_take_multiplier = Decimal(
            str(
                getattr(
                    context.symbol_config,
                    "atr_take_multiplier",
                    self.atr_take_multiplier,
                )
            )
        )

        if requested == "ATR":
            atr_value, atr_diagnostics = _resolve_atr(context, atr_period)
            if atr_value is not None:
                stop, take = _atr_prices(
                    action=action,
                    entry=entry,
                    rules=context.exchange_rules,
                    atr=atr_value,
                    stop_multiplier=atr_stop_multiplier,
                    take_multiplier=atr_take_multiplier,
                )
                return _BaseExitPrices(
                    stop_price=stop,
                    take_profit_price=take,
                    exit_model_requested="ATR",
                    exit_model_effective="ATR",
                    atr_value=atr_value,
                    atr_available=True,
                    atr_fallback_reason=None,
                    diagnostics={
                        **atr_diagnostics,
                        "atr_period": atr_period,
                        "atr_stop_multiplier": str(atr_stop_multiplier),
                        "atr_take_multiplier": str(atr_take_multiplier),
                        "stop_loss_pct": str(stop_pct),
                        "take_profit_pct": str(take_pct),
                    },
                )

            stop, take = _fixed_prices(
                action=action,
                entry=entry,
                rules=context.exchange_rules,
                stop_pct=stop_pct,
                take_pct=take_pct,
            )
            fallback_reason = atr_diagnostics.get(
                "atr_fallback_reason",
                "ATR_NOT_AVAILABLE",
            )
            return _BaseExitPrices(
                stop_price=stop,
                take_profit_price=take,
                exit_model_requested="ATR",
                exit_model_effective="FIXED_PCT_FALLBACK",
                atr_value=None,
                atr_available=False,
                atr_fallback_reason=fallback_reason,
                diagnostics={
                    **atr_diagnostics,
                    "atr_period": atr_period,
                    "atr_fallback_reason": fallback_reason,
                    "exit_model_effective": "FIXED_PCT_FALLBACK",
                    "stop_loss_pct": str(stop_pct),
                    "take_profit_pct": str(take_pct),
                },
            )

        stop, take = _fixed_prices(
            action=action,
            entry=entry,
            rules=context.exchange_rules,
            stop_pct=stop_pct,
            take_pct=take_pct,
        )
        return _BaseExitPrices(
            stop_price=stop,
            take_profit_price=take,
            exit_model_requested="FIXED_PCT",
            exit_model_effective="FIXED_PCT",
            atr_value=None,
            atr_available=False,
            atr_fallback_reason=None,
            diagnostics={
                "stop_loss_pct": str(stop_pct),
                "take_profit_pct": str(take_pct),
            },
        )

    def _max_allowed_take_distance(
        self,
        *,
        entry: Decimal,
        atr_value: Decimal | None,
    ) -> tuple[Decimal | None, dict[str, Any]]:
        max_by_bps = (
            entry * self.max_economic_take_bps / BPS if self.max_economic_take_bps > 0 else None
        )
        max_by_atr = (
            atr_value * self.max_economic_take_atr_multiplier
            if atr_value is not None and atr_value > 0 and self.max_economic_take_atr_multiplier > 0
            else None
        )
        candidates = [item for item in (max_by_bps, max_by_atr) if item is not None and item > 0]
        max_allowed = min(candidates) if candidates else None
        return max_allowed, {
            "max_economic_take_bps": str(self.max_economic_take_bps),
            "max_economic_take_atr_multiplier": str(self.max_economic_take_atr_multiplier),
            "max_allowed_take_distance": decimal_or_none(max_allowed),
            "max_allowed_take_bps": decimal_or_none(
                _distance_bps(max_allowed, entry) if max_allowed is not None else None
            ),
            "max_take_limited_by": _max_take_limited_by(max_by_bps, max_by_atr),
            "max_take_distance_by_bps": decimal_or_none(max_by_bps),
            "max_take_distance_by_atr": decimal_or_none(max_by_atr),
        }

    def _diagnostics(
        self,
        *,
        base: _BaseExitPrices,
        projection: EconomicExitProjection,
        final_projection: EconomicExitProjection,
        cost: CostEstimate,
        max_allowed_diagnostics: dict[str, Any],
        final_take_price: Decimal | None,
        adjusted: bool,
        block_reason: str | None,
    ) -> dict[str, Any]:
        adjustment_reason = _adjustment_reason(
            enabled=self.economic_exit_adjustment_enabled,
            adjusted=adjusted,
            block_reason=block_reason,
            configured_take_distance=projection.gross_take_distance_configured,
            economic_required_take_distance=(projection.economic_required_take_distance),
        )
        diagnostics = {
            "model": "economic_aware",
            "exit_model_requested": base.exit_model_requested,
            "exit_model_effective": base.exit_model_effective,
            "economic_exit_adjustment_enabled": (self.economic_exit_adjustment_enabled),
            "economic_exit_diagnostics_enabled": (self.economic_exit_diagnostics_enabled),
            "economic_exit_adjusted": adjusted,
            "economic_exit_adjustment_reason": adjustment_reason,
            "economic_exit_blocked_unrealistic_take": block_reason is not None,
            "block_reason": block_reason,
            "entry_reference_price": str(projection.entry_price),
            "quantity": str(projection.quantity),
            "notional": str(projection.notional),
            "configured_stop_price": str(base.stop_price),
            "configured_take_price": str(base.take_profit_price),
            "required_take_price": str(projection.required_take_price),
            "final_take_price": decimal_or_none(final_take_price),
            "configured_stop_bps": decimal_or_none(projection.stop_bps),
            "configured_take_bps": decimal_or_none(projection.configured_take_bps),
            "economic_required_take_bps": decimal_or_none(projection.economic_required_take_bps),
            "required_take_bps": decimal_or_none(projection.required_take_bps),
            "final_take_bps": decimal_or_none(final_projection.required_take_bps),
            "final_stop_bps": decimal_or_none(projection.stop_bps),
            "stop_bps": decimal_or_none(projection.stop_bps),
            "gross_stop_distance": str(projection.gross_stop_distance),
            "gross_take_distance_configured": str(projection.gross_take_distance_configured),
            "required_net_gain": str(projection.required_net_gain),
            "required_take_distance_by_min_net_gain": str(
                projection.required_take_distance_by_min_net_gain
            ),
            "required_take_distance_by_reward_risk": str(
                projection.required_take_distance_by_reward_risk
            ),
            "economic_required_take_distance": str(projection.economic_required_take_distance),
            "required_take_distance_final": str(projection.required_take_distance_final),
            "expected_net_gain": str(final_projection.expected_net_gain),
            "expected_net_gain_bps": decimal_or_none(final_projection.expected_net_gain_bps),
            "expected_net_loss": str(final_projection.expected_net_loss),
            "reward_risk": decimal_or_none(final_projection.reward_risk),
            "min_net_gain_bps": str(self.min_net_gain_bps),
            "min_net_reward_risk": str(self.min_net_reward_risk),
            "round_trip_cost": str(cost.round_trip_cost),
            "round_trip_cost_bps": str(cost.round_trip_cost_bps),
            "minimum_profitable_move": str(cost.minimum_profitable_move),
            "cost_components": {
                "entry_fee": str(cost.entry_fee),
                "exit_fee": str(cost.exit_fee),
                "spread_cost": str(cost.spread_cost),
                "slippage_cost": str(cost.slippage_cost),
                "funding_cost": str(cost.funding_cost),
            },
            "cost_sources": cost.diagnostics.get("sources", {}),
            "atr_available": base.atr_available,
            "atr_value": decimal_or_none(base.atr_value),
            "atr_fallback_reason": base.atr_fallback_reason,
            **base.diagnostics,
            **max_allowed_diagnostics,
        }
        if block_reason is not None:
            diagnostics["economic_exit_not_realistic"] = True
        return diagnostics


def _fixed_prices(
    *,
    action: str,
    entry: Decimal,
    rules: SymbolRules,
    stop_pct: Decimal,
    take_pct: Decimal,
) -> tuple[Decimal, Decimal]:
    if action == TradeAction.OPEN_LONG.value:
        return (
            rules.round_price_down(entry * (Decimal("1") - stop_pct)),
            rules.round_price_up(entry * (Decimal("1") + take_pct)),
        )
    return (
        rules.round_price_up(entry * (Decimal("1") + stop_pct)),
        rules.round_price_down(entry * (Decimal("1") - take_pct)),
    )


def _atr_prices(
    *,
    action: str,
    entry: Decimal,
    rules: SymbolRules,
    atr: Decimal,
    stop_multiplier: Decimal,
    take_multiplier: Decimal,
) -> tuple[Decimal, Decimal]:
    stop_distance = atr * stop_multiplier
    take_distance = atr * take_multiplier
    if action == TradeAction.OPEN_LONG.value:
        return (
            rules.round_price_down(entry - stop_distance),
            rules.round_price_up(entry + take_distance),
        )
    return (
        rules.round_price_up(entry + stop_distance),
        rules.round_price_down(entry - take_distance),
    )


def _round_take_price(
    *,
    rules: SymbolRules,
    action: str,
    price: Decimal,
) -> Decimal:
    if action == TradeAction.OPEN_LONG.value:
        return rules.round_price_up(price)
    return rules.round_price_down(price)


def _resolve_atr(
    context: TradeContext,
    period: int,
) -> tuple[Decimal | None, dict[str, Any]]:
    context_atr = decimal_or_none_from_any(getattr(context, "atr", None))
    if context_atr is not None and context_atr > 0:
        return context_atr, {
            "atr_available": True,
            "atr_source": "trade_context_atr",
        }

    candles = tuple(getattr(context, "candles", ()) or ())
    if candles:
        try:
            atr = average_true_range(candles, period)
        except Exception as error:
            return None, {
                "atr_available": False,
                "atr_source": "trade_context_candles",
                "atr_fallback_reason": f"ATR_CALCULATION_FAILED:{error.__class__.__name__}",
                "atr_candle_count": len(candles),
            }
        if atr is not None and atr > 0:
            return atr, {
                "atr_available": True,
                "atr_source": "trade_context_candles",
                "atr_candle_count": len(candles),
            }
        return None, {
            "atr_available": False,
            "atr_source": "trade_context_candles",
            "atr_fallback_reason": "ATR_HISTORY_INSUFFICIENT",
            "atr_candle_count": len(candles),
        }

    return None, {
        "atr_available": False,
        "atr_source": "none",
        "atr_fallback_reason": "ATR_NOT_AVAILABLE_IN_CONTEXT",
        "atr_candle_count": 0,
    }


def _adjustment_reason(
    *,
    enabled: bool,
    adjusted: bool,
    block_reason: str | None,
    configured_take_distance: Decimal,
    economic_required_take_distance: Decimal,
) -> str:
    if block_reason is not None:
        return ECONOMIC_EXIT_NOT_REALISTIC
    if not enabled:
        return "ECONOMIC_EXIT_ADJUSTMENT_DISABLED"
    if adjusted:
        return "TAKE_PROFIT_RAISED_TO_ECONOMIC_MINIMUM"
    if configured_take_distance >= economic_required_take_distance:
        return "CONFIGURED_TAKE_ALREADY_ECONOMIC"
    return "TAKE_PROFIT_ALREADY_AT_ECONOMIC_MINIMUM_AFTER_ROUNDING"


def _max_take_limited_by(
    max_by_bps: Decimal | None,
    max_by_atr: Decimal | None,
) -> str | None:
    if max_by_bps is None and max_by_atr is None:
        return None
    if max_by_bps is None:
        return "ATR"
    if max_by_atr is None:
        return "BPS"
    return "BPS" if max_by_bps <= max_by_atr else "ATR"


def _config_value(config: "AppConfig", name: str, default: str) -> Any:
    return getattr(config, name, default)


def _config_decimal(config: "AppConfig", name: str, default: str) -> Decimal:
    return Decimal(str(getattr(config, name, default)))


def _normalize_exit_model(value: Any) -> str:
    raw = getattr(value, "value", value)
    normalized = str(raw or "FIXED_PCT").upper().strip()
    return "ATR" if normalized == "ATR" else "FIXED_PCT"


def _distance_bps(distance: Decimal | None, entry: Decimal) -> Decimal | None:
    if distance is None or entry <= 0:
        return None
    return distance / entry * BPS
