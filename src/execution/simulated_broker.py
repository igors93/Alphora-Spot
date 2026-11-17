from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from domain.models import FuturesPosition, PositionDirection, TradeAction
from domain.symbol_rules import SymbolRules
from domain.trade_result import TradeResult
from futures.exit_models import FixedPctExitModel
from futures.performance.cost_model import CostModel
from futures.planned_exit import GLOBAL_FALLBACK_EXIT_SOURCE
from futures.protection import normalize_planned_exit_prices
from futures.risk_manager import FuturesRiskManager


@dataclass(frozen=True)
class SimulatedPosition:
    symbol: str
    direction: PositionDirection
    entry_time: str
    entry_price: Decimal
    quantity: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    strategy_reason_entry: str
    trade_plan_id: str | None = None
    adaptive_adjustments_applied: int = 0
    last_adaptive_adjustment_plan_id: str | None = None
    dynamic_exit_used: bool = False
    exit_source: str = GLOBAL_FALLBACK_EXIT_SOURCE
    recommended_stop_price: Decimal | None = None
    recommended_take_profit_price: Decimal | None = None
    actual_stop_price_used: Decimal | None = None
    actual_take_profit_price_used: Decimal | None = None
    reward_risk_liquido: Decimal | None = None
    raw_reward_risk: Decimal | None = None
    cost_to_reward_ratio: Decimal | None = None
    risk_pct: Decimal | None = None
    reward_pct: Decimal | None = None
    planned_exit_rejection_reason: str | None = None
    planned_exit_diagnostics: dict | None = None
    smart_sizing_mode: str = "OFF"
    smart_sizing_used: bool = False
    smart_sizing_affected_result: bool = False
    base_notional_usdt: Decimal | None = None
    final_notional_usdt: Decimal | None = None
    sizing_multiplier: Decimal | None = None
    sizing_reason: str | None = None
    sizing_diagnostics: dict | None = None
    smart_execution_mode: str = "OFF"
    smart_execution_used: bool = False
    smart_execution_affected_result: bool = False
    entry_order_type: str = "MARKET"
    limit_price: Decimal | None = None
    limit_fill_status: str | None = None
    limit_wait_candles: int = 0
    fallback_to_market_used: bool = False
    smart_execution_reason: str | None = None
    smart_execution_diagnostics: dict | None = None

    @property
    def notional(self) -> Decimal:
        return self.entry_price * self.quantity

    def to_futures_position(self) -> FuturesPosition:
        # opened_at=0 would make holding_seconds ~ the Unix epoch age, so any
        # max-hold guard would force-close the paper position right after entry.
        opened = _parse_timestamp(str(self.entry_time or ""))
        return FuturesPosition(
            symbol=self.symbol,
            direction=self.direction,
            quantity=self.quantity,
            entry_price=self.entry_price,
            unrealized_pnl=Decimal("0"),
            opened_at=opened.timestamp() if opened is not None else None,
        )


class SimulatedBroker:
    def __init__(
        self,
        *,
        symbol: str,
        risk_manager: FuturesRiskManager,
        symbol_rules: SymbolRules,
        cost_model: CostModel,
        exit_model=None,
    ):
        self.symbol = symbol
        self.risk_manager = risk_manager
        self.symbol_rules = symbol_rules
        self.cost_model = cost_model
        self.exit_model = exit_model or FixedPctExitModel()
        self.position: SimulatedPosition | None = None

    def current_position(self) -> FuturesPosition:
        if self.position is None:
            return FuturesPosition(
                symbol=self.symbol,
                direction=PositionDirection.NONE,
                quantity=Decimal("0"),
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                opened_at=None,
            )

        return self.position.to_futures_position()

    def open_position(
        self,
        *,
        action: TradeAction,
        timestamp: str,
        price: Decimal,
        reason: str,
        quantity: Decimal | None = None,
        stop_price: Decimal | None = None,
        take_profit_price: Decimal | None = None,
        trade_plan_id: str | None = None,
        dynamic_exit_used: bool = False,
        exit_source: str = GLOBAL_FALLBACK_EXIT_SOURCE,
        recommended_stop_price: Decimal | None = None,
        recommended_take_profit_price: Decimal | None = None,
        reward_risk_liquido: Decimal | None = None,
        raw_reward_risk: Decimal | None = None,
        cost_to_reward_ratio: Decimal | None = None,
        risk_pct: Decimal | None = None,
        reward_pct: Decimal | None = None,
        planned_exit_rejection_reason: str | None = None,
        planned_exit_diagnostics: dict | None = None,
        smart_sizing_mode: str = "OFF",
        smart_sizing_used: bool = False,
        smart_sizing_affected_result: bool = False,
        base_notional_usdt: Decimal | None = None,
        final_notional_usdt: Decimal | None = None,
        sizing_multiplier: Decimal | None = None,
        sizing_reason: str | None = None,
        sizing_diagnostics: dict | None = None,
        smart_execution_mode: str = "OFF",
        smart_execution_used: bool = False,
        smart_execution_affected_result: bool = False,
        entry_order_type: str = "MARKET",
        limit_price: Decimal | None = None,
        limit_fill_status: str | None = None,
        limit_wait_candles: int = 0,
        fallback_to_market_used: bool = False,
        smart_execution_reason: str | None = None,
        smart_execution_diagnostics: dict | None = None,
    ) -> SimulatedPosition:
        if self.position is not None:
            raise RuntimeError("Ja existe posicao simulada aberta")

        if action == TradeAction.OPEN_LONG:
            direction = PositionDirection.LONG
        elif action == TradeAction.OPEN_SHORT:
            direction = PositionDirection.SHORT
        else:
            raise ValueError(f"Acao invalida para abertura: {action}")

        if quantity is None:
            quantity = self.risk_manager.calculate_quantity(price, self.symbol_rules)
        else:
            quantity = self.risk_manager.normalize_planned_quantity(
                planned_quantity=quantity,
                price=price,
                rules=self.symbol_rules,
            )

        if stop_price is not None or take_profit_price is not None:
            if stop_price is None or take_profit_price is None:
                raise ValueError("stop_price e take_profit_price planejados devem vir juntos")

            final_stop, final_take = normalize_planned_exit_prices(
                action=action,
                entry_price=price,
                rules=self.symbol_rules,
                planned_stop_price=stop_price,
                planned_take_profit_price=take_profit_price,
            )
        else:
            exit_prices = self.exit_model.calculate(
                action=action,
                entry_price=price,
                rules=self.symbol_rules,
                stop_loss_pct=self.risk_manager.stop_loss_pct,
                take_profit_pct=self.risk_manager.take_profit_pct,
            )
            final_stop = exit_prices.stop_price
            final_take = exit_prices.take_profit_price

        recommended_stop = recommended_stop_price
        if recommended_stop is None and dynamic_exit_used:
            recommended_stop = stop_price
        recommended_take = recommended_take_profit_price
        if recommended_take is None and dynamic_exit_used:
            recommended_take = take_profit_price

        self.position = SimulatedPosition(
            symbol=self.symbol,
            direction=direction,
            entry_time=timestamp,
            entry_price=price,
            quantity=quantity,
            stop_price=final_stop,
            take_profit_price=final_take,
            strategy_reason_entry=reason,
            trade_plan_id=trade_plan_id,
            dynamic_exit_used=bool(dynamic_exit_used),
            exit_source=str(exit_source or GLOBAL_FALLBACK_EXIT_SOURCE),
            recommended_stop_price=recommended_stop,
            recommended_take_profit_price=recommended_take,
            actual_stop_price_used=final_stop,
            actual_take_profit_price_used=final_take,
            reward_risk_liquido=reward_risk_liquido,
            raw_reward_risk=raw_reward_risk,
            cost_to_reward_ratio=cost_to_reward_ratio,
            risk_pct=risk_pct,
            reward_pct=reward_pct,
            planned_exit_rejection_reason=planned_exit_rejection_reason,
            planned_exit_diagnostics=(
                dict(planned_exit_diagnostics)
                if isinstance(planned_exit_diagnostics, dict)
                else planned_exit_diagnostics
            ),
            smart_sizing_mode=str(smart_sizing_mode or "OFF"),
            smart_sizing_used=bool(smart_sizing_used),
            smart_sizing_affected_result=bool(smart_sizing_affected_result),
            base_notional_usdt=base_notional_usdt,
            final_notional_usdt=final_notional_usdt,
            sizing_multiplier=sizing_multiplier,
            sizing_reason=sizing_reason,
            sizing_diagnostics=(
                dict(sizing_diagnostics)
                if isinstance(sizing_diagnostics, dict)
                else sizing_diagnostics
            ),
            smart_execution_mode=str(smart_execution_mode or "OFF"),
            smart_execution_used=bool(smart_execution_used),
            smart_execution_affected_result=bool(smart_execution_affected_result),
            entry_order_type=str(entry_order_type or "MARKET"),
            limit_price=limit_price,
            limit_fill_status=limit_fill_status,
            limit_wait_candles=max(0, int(limit_wait_candles or 0)),
            fallback_to_market_used=bool(fallback_to_market_used),
            smart_execution_reason=smart_execution_reason,
            smart_execution_diagnostics=(
                dict(smart_execution_diagnostics)
                if isinstance(smart_execution_diagnostics, dict)
                else smart_execution_diagnostics
            ),
        )
        return self.position

    def adjust_protection(
        self,
        *,
        stop_price: Decimal | None = None,
        take_profit_price: Decimal | None = None,
        current_price: Decimal | None = None,
        adjustment_plan_id: str | None = None,
        reason: str,
    ) -> SimulatedPosition:
        if self.position is None:
            raise RuntimeError("Nao existe posicao simulada aberta")

        position = self.position
        if current_price is None:
            current_price = position.entry_price

        final_stop = stop_price if stop_price is not None else position.stop_price
        final_take = (
            take_profit_price if take_profit_price is not None else position.take_profit_price
        )

        final_stop, final_take = _normalize_adjusted_protection_prices(
            direction=position.direction,
            entry_price=position.entry_price,
            current_price=Decimal(str(current_price)),
            rules=self.symbol_rules,
            stop_price=final_stop,
            take_profit_price=final_take,
        )

        self.position = replace(
            position,
            stop_price=final_stop,
            take_profit_price=final_take,
            adaptive_adjustments_applied=position.adaptive_adjustments_applied + 1,
            last_adaptive_adjustment_plan_id=adjustment_plan_id,
        )
        return self.position

    def close_position(
        self,
        *,
        timestamp: str,
        price: Decimal,
        exit_reason: str,
        strategy_reason_exit: str,
    ) -> TradeResult:
        if self.position is None:
            raise RuntimeError("Nao existe posicao simulada aberta")

        position = self.position
        self.position = None

        if position.direction == PositionDirection.LONG:
            gross_pnl = (price - position.entry_price) * position.quantity
        elif position.direction == PositionDirection.SHORT:
            gross_pnl = (position.entry_price - price) * position.quantity
        else:
            raise ValueError(f"Direcao invalida: {position.direction}")

        exit_notional = price * position.quantity
        holding_hours = _estimate_holding_hours(position.entry_time, timestamp)
        net_pnl, costs = self.cost_model.calculate_net_pnl(
            gross_pnl=gross_pnl,
            entry_notional=position.notional,
            exit_notional=exit_notional,
            holding_hours=holding_hours,
        )
        return_pct = net_pnl / position.notional if position.notional > 0 else Decimal("0")

        return TradeResult(
            symbol=position.symbol,
            direction=position.direction,
            entry_time=position.entry_time,
            exit_time=timestamp,
            entry_price=position.entry_price,
            exit_price=price,
            quantity=position.quantity,
            notional=position.notional,
            gross_pnl=gross_pnl,
            fees=costs.fees,
            slippage_cost=costs.slippage_cost,
            spread_cost=costs.spread_cost,
            funding_cost=costs.funding_cost,
            net_pnl=net_pnl,
            return_pct=return_pct,
            exit_reason=exit_reason,
            strategy_reason_entry=position.strategy_reason_entry,
            strategy_reason_exit=strategy_reason_exit,
            dynamic_exit_used=position.dynamic_exit_used,
            exit_source=position.exit_source,
            recommended_stop_price=position.recommended_stop_price,
            recommended_take_profit_price=position.recommended_take_profit_price,
            actual_stop_price_used=position.actual_stop_price_used,
            actual_take_profit_price_used=position.actual_take_profit_price_used,
            reward_risk_liquido=position.reward_risk_liquido,
            raw_reward_risk=position.raw_reward_risk,
            cost_to_reward_ratio=position.cost_to_reward_ratio,
            risk_pct=position.risk_pct,
            reward_pct=position.reward_pct,
            planned_exit_rejection_reason=position.planned_exit_rejection_reason,
            planned_exit_diagnostics=position.planned_exit_diagnostics,
            smart_sizing_mode=position.smart_sizing_mode,
            smart_sizing_used=position.smart_sizing_used,
            smart_sizing_affected_result=position.smart_sizing_affected_result,
            base_notional_usdt=position.base_notional_usdt,
            final_notional_usdt=position.final_notional_usdt,
            sizing_multiplier=position.sizing_multiplier,
            sizing_reason=position.sizing_reason,
            sizing_diagnostics=position.sizing_diagnostics,
            smart_execution_mode=position.smart_execution_mode,
            smart_execution_used=position.smart_execution_used,
            smart_execution_affected_result=position.smart_execution_affected_result,
            entry_order_type=position.entry_order_type,
            limit_price=position.limit_price,
            limit_fill_status=position.limit_fill_status,
            limit_wait_candles=position.limit_wait_candles,
            fallback_to_market_used=position.fallback_to_market_used,
            smart_execution_reason=position.smart_execution_reason,
            smart_execution_diagnostics=position.smart_execution_diagnostics,
        )


def _estimate_holding_hours(entry_time: str, exit_time: str) -> Decimal:
    entry = _parse_timestamp(entry_time)
    exit_ = _parse_timestamp(exit_time)

    if entry is None or exit_ is None or exit_ <= entry:
        return Decimal("0")

    seconds = Decimal(str((exit_ - entry).total_seconds()))
    return seconds / Decimal("3600")


def _parse_timestamp(value: str) -> datetime | None:
    value = value.strip()

    try:
        numeric = Decimal(value)
    except InvalidOperation:
        numeric = None

    if numeric is not None:
        if not numeric.is_finite():
            return None
        if numeric > Decimal("100000000000"):
            numeric = numeric / Decimal("1000")
        try:
            return datetime.fromtimestamp(float(numeric), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _normalize_adjusted_protection_prices(
    *,
    direction: PositionDirection,
    entry_price: Decimal,
    current_price: Decimal,
    rules: SymbolRules,
    stop_price: Decimal,
    take_profit_price: Decimal,
) -> tuple[Decimal, Decimal]:
    stop = Decimal(str(stop_price))
    take = Decimal(str(take_profit_price))

    if entry_price <= 0:
        raise ValueError("entry_price precisa ser maior que zero")

    if current_price <= 0:
        raise ValueError("current_price precisa ser maior que zero")

    if direction == PositionDirection.LONG:
        stop = rules.round_price_down(stop)
        take = rules.round_price_up(take)

        if stop >= current_price:
            raise ValueError(
                "adjusted stop invalido para LONG: precisa ficar abaixo do preco atual"
            )

        if take <= entry_price:
            raise ValueError("adjusted take invalido para LONG: precisa ficar acima da entrada")

        return stop, take

    if direction == PositionDirection.SHORT:
        stop = rules.round_price_up(stop)
        take = rules.round_price_down(take)

        if stop <= current_price:
            raise ValueError(
                "adjusted stop invalido para SHORT: precisa ficar acima do preco atual"
            )

        if take >= entry_price:
            raise ValueError("adjusted take invalido para SHORT: precisa ficar abaixo da entrada")

        return stop, take

    raise ValueError(f"Direcao invalida para ajuste: {direction}")
