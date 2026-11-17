from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from backtesting.domain.config import BacktestSymbolConfig
from backtesting.domain.realism import (
    BacktestFillSettings,
    EquityPoint,
    FundingRatePoint,
)
from backtesting.engine.broker import SimulatedBroker, SimulatedPosition
from backtesting.infrastructure.candle_loader import Candle
from domain.barriers import (
    LIQUIDATION,
    STOP_LOSS,
    AmbiguousRejected,
    BarrierHit,
    Barriers,
    hit_after_limit_fill,
    hit_at_open,
    hit_within_candle,
)
from domain.models import FuturesPosition, PositionDirection, TradeAction
from domain.symbol_rules import SymbolRules
from domain.trade_result import TradeResult
from futures.performance.cost_model import CostModel
from futures.planned_exit import GLOBAL_FALLBACK_EXIT_SOURCE
from futures.risk_manager import FuturesRiskManager


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    reason: str
    position: SimulatedPosition | None = None
    trade: TradeResult | None = None


class BacktestLiquidityError(ValueError):
    pass


class ExecutionSimulator:
    def __init__(
        self,
        *,
        symbols: list[BacktestSymbolConfig],
        cost_model: CostModel,
        same_candle_exit_policy: str = "CONSERVATIVE_STOP_FIRST",
        end_of_data_policy: str = "CLOSE_AT_LAST_PRICE",
        initial_balance: Decimal = Decimal("1000"),
        fill_settings: BacktestFillSettings | None = None,
        funding_rates_by_symbol: dict[str, tuple[FundingRatePoint, ...]] | None = None,
    ):
        self.symbol_configs = {item.symbol: item for item in symbols}
        self.same_candle_exit_policy = (
            str(same_candle_exit_policy or "CONSERVATIVE_STOP_FIRST").upper().strip()
        )
        self.end_of_data_policy = str(end_of_data_policy or "CLOSE_AT_LAST_PRICE").upper().strip()
        self.cost_model = cost_model
        self.initial_balance = Decimal(str(initial_balance))
        self.fill_settings = fill_settings or BacktestFillSettings()
        funding_rates_by_symbol = funding_rates_by_symbol or {}
        self.brokers = {
            item.symbol: SimulatedBroker(
                symbol=item.symbol,
                risk_manager=FuturesRiskManager(
                    position_notional_usdt=float(item.position_notional_usdt),
                    stop_loss_pct=float(item.stop_loss_pct),
                    take_profit_pct=float(item.take_profit_pct),
                ),
                symbol_rules=item.rules,
                cost_model=cost_model,
                leverage=item.leverage,
                fill_settings=self.fill_settings,
                funding_rate_points=funding_rates_by_symbol.get(item.symbol, ()),
            )
            for item in symbols
        }
        self.trades: list[TradeResult] = []
        self.realized_pnl = Decimal("0")
        self.consecutive_losses = 0
        self.ambiguous_stop_take_count = 0
        self.ambiguous_stop_take_by_symbol: dict[str, int] = {}
        self.ambiguous_outcome_counts: dict[str, int] = {}
        self.end_of_data_closed_count = 0
        self.end_of_data_open_positions_count = 0
        self.end_of_data_unrealized_pnl = Decimal("0")
        self.end_of_data_open_exposure = Decimal("0")
        self.dynamic_exit_used_count = 0
        self.global_fallback_count = 0
        self.invalid_strategy_exit_count = 0
        self.gap_stop_count = 0
        self.liquidation_count = 0
        self.partial_fill_count = 0
        self.liquidity_rejection_count = 0
        self.high_participation_fill_count = 0
        self.exit_liquidity_breach_count = 0
        self.max_exit_market_impact_bps = Decimal("0")
        self.funding_expected_periods = 0
        self.funding_missing_periods = 0
        self.max_observed_volume_participation = Decimal("0")
        self.limit_entry_intrabar_ambiguity_count = 0
        self.equity_curve: list[EquityPoint] = []
        self.last_mark_prices: dict[str, Decimal] = {}

    def current_position(self, symbol: str) -> FuturesPosition:
        return self.brokers[symbol].current_position()

    def open_position(
        self,
        *,
        symbol: str,
        action: TradeAction,
        timestamp: str,
        price: Decimal,
        reason: str,
        quantity: Decimal | None = None,
        stop_price: Decimal | None = None,
        take_profit_price: Decimal | None = None,
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
        candle_volume: Decimal | None = None,
    ) -> ExecutionResult:
        requested_quantity = (
            Decimal(str(quantity))
            if quantity is not None
            else self.symbol_configs[symbol].position_notional_usdt / Decimal(str(price))
        )
        executable_requested_quantity = self.brokers[
            symbol
        ].risk_manager.normalize_planned_quantity(
            planned_quantity=requested_quantity,
            price=Decimal(str(price)),
            rules=self.symbol_configs[symbol].rules,
        )
        filled_quantity, participation = self._entry_fill_quantity(
            symbol=symbol,
            price=Decimal(str(price)),
            requested_quantity=executable_requested_quantity,
            candle_volume=candle_volume,
        )
        if filled_quantity < executable_requested_quantity:
            self.partial_fill_count += 1
        self._record_participation(participation)
        entry_maker = bool(
            self.fill_settings.assume_limit_maker
            and str(entry_order_type).upper().strip() == "LIMIT"
            and str(limit_fill_status or "").upper().strip() == "LIMIT_FILLED"
        )
        market_impact_bps = Decimal("0") if entry_maker else self._market_impact_bps(participation)
        position = self.brokers[symbol].open_position(
            action=action,
            timestamp=timestamp,
            price=price,
            reason=reason,
            quantity=filled_quantity,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            dynamic_exit_used=dynamic_exit_used,
            exit_source=exit_source,
            recommended_stop_price=recommended_stop_price,
            recommended_take_profit_price=recommended_take_profit_price,
            reward_risk_liquido=reward_risk_liquido,
            raw_reward_risk=raw_reward_risk,
            cost_to_reward_ratio=cost_to_reward_ratio,
            risk_pct=risk_pct,
            reward_pct=reward_pct,
            planned_exit_rejection_reason=planned_exit_rejection_reason,
            planned_exit_diagnostics=planned_exit_diagnostics,
            smart_sizing_mode=smart_sizing_mode,
            smart_sizing_used=smart_sizing_used,
            smart_sizing_affected_result=smart_sizing_affected_result,
            base_notional_usdt=base_notional_usdt,
            final_notional_usdt=final_notional_usdt,
            sizing_multiplier=sizing_multiplier,
            sizing_reason=sizing_reason,
            sizing_diagnostics=sizing_diagnostics,
            smart_execution_mode=smart_execution_mode,
            smart_execution_used=smart_execution_used,
            smart_execution_affected_result=smart_execution_affected_result,
            entry_order_type=entry_order_type,
            limit_price=limit_price,
            limit_fill_status=limit_fill_status,
            limit_wait_candles=limit_wait_candles,
            fallback_to_market_used=fallback_to_market_used,
            smart_execution_reason=smart_execution_reason,
            smart_execution_diagnostics=smart_execution_diagnostics,
            entry_maker=entry_maker,
            requested_quantity=requested_quantity,
            entry_volume_participation=participation,
            market_impact_bps=market_impact_bps,
        )
        if dynamic_exit_used:
            self.dynamic_exit_used_count += 1
        else:
            self.global_fallback_count += 1
            if planned_exit_rejection_reason:
                self.invalid_strategy_exit_count += 1
        return ExecutionResult(
            status="EXECUTED",
            reason="POSITION_OPENED",
            position=position,
        )

    def close_position(
        self,
        *,
        symbol: str,
        timestamp: str,
        price: Decimal,
        exit_reason: str,
        strategy_reason_exit: str,
        candle_volume: Decimal | None = None,
        exit_maker: bool = False,
        liquidation: bool = False,
    ) -> ExecutionResult:
        position = self.brokers[symbol].position
        liquidity_volume = Decimal(str(candle_volume or 0))
        if exit_maker:
            liquidity_volume *= self.fill_settings.intrabar_limit_volume_fraction
        participation = _volume_participation(
            position.quantity if position is not None else Decimal("0"),
            liquidity_volume,
        )
        self._record_participation(participation)
        if participation > self.fill_settings.max_volume_participation:
            self.exit_liquidity_breach_count += 1
        extra_slippage_bps = (
            Decimal("0")
            if exit_maker
            else self._market_impact_bps(participation, stress_above_cap=True)
        )
        self.max_exit_market_impact_bps = max(
            self.max_exit_market_impact_bps,
            extra_slippage_bps,
        )
        liquidation_fee = Decimal("0")
        if liquidation and position is not None:
            liquidation_fee = (
                Decimal(str(price)) * position.quantity * self.fill_settings.liquidation_fee_pct
            )
        trade = self.brokers[symbol].close_position(
            timestamp=timestamp,
            price=price,
            exit_reason=exit_reason,
            strategy_reason_exit=strategy_reason_exit,
            exit_maker=exit_maker,
            exit_market_impact=not exit_maker,
            exit_volume_participation=participation,
            extra_slippage_bps=extra_slippage_bps,
            liquidation_fee=liquidation_fee,
        )
        self._record_trade(trade)
        return ExecutionResult(status="EXECUTED", reason=exit_reason, trade=trade)

    def maybe_close_at_open(
        self,
        *,
        symbol: str,
        candle: Candle,
    ) -> ExecutionResult | None:
        position = self.brokers[symbol].position
        if position is None or position.direction == PositionDirection.NONE:
            return None
        hit = hit_at_open(self._barriers(symbol, position), Decimal(str(candle.open)))
        if hit is None:
            return None
        if hit.kind == LIQUIDATION:
            return self._close_liquidation(symbol, candle, hit.price, hit.exit_reason)
        if hit.kind == STOP_LOSS:
            self.gap_stop_count += 1
        return self.close_position(
            symbol=symbol,
            timestamp=candle.timestamp,
            price=hit.price,
            exit_reason=hit.exit_reason,
            strategy_reason_exit=_exit_explanation(hit, position.direction),
            candle_volume=candle.volume,
            exit_maker=hit.maker,
        )

    def maybe_close_by_stop_or_take(
        self,
        *,
        symbol: str,
        candle: Candle,
        include_open_gap: bool = True,
    ) -> ExecutionResult | None:
        broker = self.brokers[symbol]
        position = broker.position
        if position is None:
            return None
        if include_open_gap:
            open_result = self.maybe_close_at_open(symbol=symbol, candle=candle)
            if open_result is not None:
                return open_result
            position = broker.position
            if position is None:
                return open_result
        if position.direction == PositionDirection.NONE:
            return None

        hit = hit_within_candle(
            self._barriers(symbol, position),
            candle,
            ambiguity_policy=self.same_candle_exit_policy,
        )
        if hit is None:
            return None
        if hit.ambiguous:
            self._record_ambiguous_stop_take(symbol)
            self._record_ambiguous_outcome(hit.ambiguity_resolution)
        if isinstance(hit, AmbiguousRejected):
            return None
        if hit.kind == LIQUIDATION:
            return self._close_liquidation(symbol, candle, hit.price, hit.exit_reason)
        return self.close_position(
            symbol=symbol,
            timestamp=_candle_close_timestamp(candle),
            price=hit.price,
            exit_reason=hit.exit_reason,
            strategy_reason_exit=_exit_explanation(hit, position.direction),
            candle_volume=candle.volume,
            exit_maker=hit.maker,
        )

    def _barriers(self, symbol: str, position: SimulatedPosition) -> Barriers:
        """The exits of ``position`` as the shared barrier rules see them."""

        tick = self.symbol_configs[symbol].rules.price_tick
        return Barriers(
            direction=position.direction.value,
            stop=position.stop_price,
            take=position.take_profit_price,
            liquidation=position.liquidation_price,
            take_trigger_offset=(
                tick if self.fill_settings.require_limit_trade_through else Decimal("0")
            ),
        )

    def maybe_close_after_limit_fill(
        self,
        *,
        symbol: str,
        candle: Candle,
    ) -> ExecutionResult | None:
        """Conservative OHLC path after an intrabar limit entry, delegated to ``domain.barriers``."""

        position = self.brokers[symbol].position
        if position is None or position.direction == PositionDirection.NONE:
            return None
        hit = hit_after_limit_fill(self._barriers(symbol, position), candle)
        if hit is None:
            return None
        if isinstance(hit, AmbiguousRejected):
            self.limit_entry_intrabar_ambiguity_count += 1
            return None
        explanation = (
            "Stop conservador apos fill limit intrabar"
            if hit.kind == STOP_LOSS
            else "Take confirmado pelo fechamento apos fill limit"
        )
        return self.close_position(
            symbol=symbol,
            timestamp=_candle_close_timestamp(candle),
            price=hit.price,
            exit_reason=hit.exit_reason,
            strategy_reason_exit=explanation,
            candle_volume=candle.volume,
            exit_maker=hit.maker,
        )

    def close_open_positions_at_end(
        self,
        last_event_by_symbol: Mapping[str, object],
    ) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        open_positions = [
            (symbol, broker.position)
            for symbol, broker in self.brokers.items()
            if broker.position is not None
        ]
        self.end_of_data_open_positions_count = len(open_positions)
        for symbol, position in open_positions:
            if position is None:
                continue
            event = last_event_by_symbol.get(symbol)
            candle = getattr(event, "candle", None)
            if candle is None:
                continue
            mark_price = Decimal(str(candle.close))
            self.end_of_data_open_exposure += position.notional
            self.end_of_data_unrealized_pnl += _unrealized_pnl(position, mark_price)
            if self.end_of_data_policy == "MARK_TO_MARKET":
                continue
            if self.end_of_data_policy == "FAIL_IF_OPEN":
                raise RuntimeError(
                    "END_OF_DATA_OPEN_POSITION: "
                    + ",".join(sorted(symbol for symbol, _ in open_positions))
                )
            result = self.close_position(
                symbol=symbol,
                timestamp=_candle_close_timestamp(candle),
                price=mark_price,
                exit_reason="END_OF_DATA",
                strategy_reason_exit="Backtest finalizado com posição aberta",
                candle_volume=Decimal(str(candle.volume)),
            )
            self.end_of_data_closed_count += 1
            results.append(result)
        return results

    def reserved_margin(self) -> Decimal:
        total = Decimal("0")
        for symbol, broker in self.brokers.items():
            position = broker.position
            if position is None:
                continue
            leverage = Decimal(str(max(1, int(self.symbol_configs[symbol].leverage))))
            total += position.notional / leverage
        return total

    def available_balance(
        self,
        prices: Mapping[str, Decimal] | None = None,
    ) -> Decimal:
        marks = dict(self.last_mark_prices)
        marks.update({symbol: Decimal(str(price)) for symbol, price in dict(prices or {}).items()})
        unrealized = Decimal("0")
        for symbol, broker in self.brokers.items():
            position = broker.position
            if position is None:
                continue
            unrealized += _unrealized_pnl(
                position,
                marks.get(symbol, position.entry_price),
            )
        return self.initial_balance + self.realized_pnl + unrealized - self.reserved_margin()

    def current_drawdown_pct(self) -> Decimal:
        if not self.equity_curve:
            return Decimal("0")
        peak = max(
            self.initial_balance,
            *(Decimal(str(point.equity)) for point in self.equity_curve),
        )
        current = Decimal(str(self.equity_curve[-1].equity))
        return (peak - current) / peak if peak > 0 else Decimal("0")

    def execution_diagnostics(self) -> dict[str, object]:
        return {
            "same_candle_exit_policy": self.same_candle_exit_policy,
            "end_of_data_policy": self.end_of_data_policy,
            "ambiguous_stop_take_count": self.ambiguous_stop_take_count,
            "ambiguous_stop_take_by_symbol": dict(self.ambiguous_stop_take_by_symbol),
            "ambiguous_policy": self.same_candle_exit_policy,
            "ambiguous_outcome_counts": dict(self.ambiguous_outcome_counts),
            "end_of_data_closed_count": self.end_of_data_closed_count,
            "open_positions_at_end": self.end_of_data_open_positions_count,
            "unrealized_pnl_at_end": str(self.end_of_data_unrealized_pnl),
            "open_exposure_at_end": str(self.end_of_data_open_exposure),
            "dynamic_exit_used_count": self.dynamic_exit_used_count,
            "global_fallback_count": self.global_fallback_count,
            "invalid_strategy_exit_count": self.invalid_strategy_exit_count,
            "gap_stop_count": self.gap_stop_count,
            "liquidation_count": self.liquidation_count,
            "partial_fill_count": self.partial_fill_count,
            "liquidity_rejection_count": self.liquidity_rejection_count,
            "high_participation_fill_count": self.high_participation_fill_count,
            "exit_liquidity_breach_count": self.exit_liquidity_breach_count,
            "max_exit_market_impact_bps": str(self.max_exit_market_impact_bps),
            "max_observed_volume_participation": str(self.max_observed_volume_participation),
            "limit_entry_intrabar_ambiguity_count": (self.limit_entry_intrabar_ambiguity_count),
            "market_impact_model_enabled": (
                self.fill_settings.market_impact_bps_at_full_participation > 0
            ),
            "funding_model_enabled": self.cost_model.funding_enabled,
            "funding_default_rate": str(self.cost_model.default_funding_rate),
            "funding_expected_periods": self.funding_expected_periods,
            "funding_missing_periods": self.funding_missing_periods,
            "funding_coverage_passed": self.funding_missing_periods == 0,
            "equity_points": len(self.equity_curve),
        }

    def mark_to_market(
        self,
        *,
        timestamp: str,
        prices: Mapping[str, Decimal],
    ) -> EquityPoint:
        self.last_mark_prices.update(
            {symbol: Decimal(str(price)) for symbol, price in prices.items()}
        )
        unrealized = Decimal("0")
        estimated_open_costs = Decimal("0")
        gross_exposure = Decimal("0")
        open_count = 0
        for symbol, broker in self.brokers.items():
            position = broker.position
            if position is None:
                continue
            mark = self.last_mark_prices.get(symbol, position.entry_price)
            open_count += 1
            gross_exposure += abs(mark * position.quantity)
            unrealized += _unrealized_pnl(position, mark)
            estimated_open_costs += broker.cost_model.calculate_total_cost(
                entry_notional=position.notional,
                exit_notional=mark * position.quantity,
                entry_maker=position.entry_maker,
                exit_maker=False,
                entry_market_impact=not position.entry_maker,
                exit_market_impact=True,
                direction=position.direction,
                entry_time=position.entry_time,
                exit_time=timestamp,
                funding_rate_points=broker.funding_rate_points,
                entry_extra_slippage_bps=position.market_impact_bps,
            ).total_cost
        point = EquityPoint(
            timestamp=timestamp,
            balance=self.initial_balance + self.realized_pnl,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            estimated_open_costs=estimated_open_costs,
            equity=self.initial_balance + self.realized_pnl + unrealized - estimated_open_costs,
            gross_exposure=gross_exposure,
            reserved_margin=self.reserved_margin(),
            open_positions=open_count,
        )
        self.equity_curve.append(point)
        return point

    def open_positions_snapshot(self) -> list[dict[str, str]]:
        positions = []
        for symbol, broker in self.brokers.items():
            position = broker.position
            if position is None:
                continue
            positions.append(
                {
                    "symbol": symbol,
                    "direction": position.direction.value,
                    "notional": str(position.notional),
                    "quantity": str(position.quantity),
                    "entry_price": str(position.entry_price),
                }
            )
        return positions

    def _record_trade(self, trade: TradeResult) -> None:
        self.trades.append(trade)
        self.realized_pnl += trade.net_pnl
        self._record_funding_coverage(trade)
        if trade.net_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def _record_funding_coverage(self, trade: TradeResult) -> None:
        if not self.cost_model.funding_enabled:
            return
        boundaries = _funding_boundaries_between(trade.entry_time, trade.exit_time)
        self.funding_expected_periods += len(boundaries)
        if not boundaries or self.cost_model.default_funding_rate != 0:
            return
        points = self.brokers[trade.symbol].funding_rate_points
        available = {
            timestamp
            for point in points
            if (timestamp := _timestamp_seconds(point.timestamp)) is not None
        }
        self.funding_missing_periods += sum(
            1 for boundary in boundaries if boundary not in available
        )

    def _record_ambiguous_stop_take(self, symbol: str) -> None:
        self.ambiguous_stop_take_count += 1
        self.ambiguous_stop_take_by_symbol[symbol] = (
            self.ambiguous_stop_take_by_symbol.get(symbol, 0) + 1
        )

    def _record_ambiguous_outcome(self, outcome: str) -> None:
        self.ambiguous_outcome_counts[outcome] = self.ambiguous_outcome_counts.get(outcome, 0) + 1

    def _entry_fill_quantity(
        self,
        *,
        symbol: str,
        price: Decimal,
        requested_quantity: Decimal,
        candle_volume: Decimal | None,
    ) -> tuple[Decimal, Decimal]:
        volume = Decimal(str(candle_volume or 0))
        if volume <= 0:
            self.liquidity_rejection_count += 1
            raise BacktestLiquidityError(f"BACKTEST_ZERO_OR_MISSING_VOLUME:{symbol}")
        max_quantity = volume * self.fill_settings.max_volume_participation
        rules = self.symbol_configs[symbol].rules
        if rules.round_quantity(requested_quantity) <= 0 and requested_quantity <= max_quantity:
            return requested_quantity, _volume_participation(requested_quantity, volume)
        if requested_quantity > max_quantity:
            if not self.fill_settings.allow_partial_fills:
                self.liquidity_rejection_count += 1
                raise BacktestLiquidityError(f"BACKTEST_LIQUIDITY_CAP_EXCEEDED:{symbol}")
            requested_fill = max_quantity
        else:
            requested_fill = requested_quantity
        filled = rules.round_quantity(requested_fill)
        try:
            rules.validate_quantity(filled, price)
        except ValueError as error:
            self.liquidity_rejection_count += 1
            raise BacktestLiquidityError(
                f"BACKTEST_PARTIAL_FILL_NOT_EXECUTABLE:{symbol}:{error}"
            ) from error
        return filled, _volume_participation(filled, volume)

    def _market_impact_bps(
        self,
        participation: Decimal,
        *,
        stress_above_cap: bool = False,
    ) -> Decimal:
        if participation <= 0:
            return Decimal("0")
        impact = self.fill_settings.market_impact_bps_at_full_participation * participation.sqrt()
        cap = self.fill_settings.max_volume_participation
        if stress_above_cap and cap > 0 and participation > cap:
            impact *= participation / cap
        return impact

    def _record_participation(self, participation: Decimal) -> None:
        self.max_observed_volume_participation = max(
            self.max_observed_volume_participation,
            participation,
        )
        if participation > self.fill_settings.reliable_volume_participation:
            self.high_participation_fill_count += 1

    def _close_liquidation(
        self,
        symbol: str,
        candle: Candle,
        price: Decimal,
        reason: str,
    ) -> ExecutionResult:
        self.liquidation_count += 1
        return self.close_position(
            symbol=symbol,
            timestamp=(
                candle.timestamp
                if str(reason).upper().endswith("GAP")
                else _candle_close_timestamp(candle)
            ),
            price=Decimal(str(price)),
            exit_reason=reason,
            strategy_reason_exit="Margem de manutencao atingida",
            candle_volume=candle.volume,
            liquidation=True,
        )


def _exit_explanation(hit: BarrierHit, direction: PositionDirection) -> str:
    long = direction == PositionDirection.LONG
    if hit.at_open:
        if hit.kind == STOP_LOSS:
            side = "abaixo" if long else "acima"
            return f"Gap {side} do stop; preenchimento conservador na abertura"
        return "Abertura alem do take; limit preenchida no alvo"
    if hit.ambiguous:
        if hit.ambiguity_resolution == "TAKE_FIRST":
            return "Stop e take atingidos no mesmo candle; politica otimista aplicou take primeiro"
        if hit.ambiguity_resolution == "NEAREST_TAKE":
            return "Stop e take atingidos no mesmo candle; take estava mais proximo da abertura"
        return "Stop e take atingidos no mesmo candle; politica conservadora aplicou stop primeiro"
    if hit.kind == STOP_LOSS:
        return f"Stop atingido pelo {'low' if long else 'high'} do candle"
    return f"Take profit atingido pelo {'high' if long else 'low'} do candle"


def default_symbol_rules(symbol: str) -> SymbolRules:
    return SymbolRules(
        symbol=symbol,
        quantity_step=Decimal("0.0001"),
        min_quantity=Decimal("0.0001"),
        min_notional=Decimal("50"),
        price_tick=Decimal("0.10"),
    )


def _unrealized_pnl(position: SimulatedPosition, mark_price: Decimal) -> Decimal:
    if position.direction == PositionDirection.LONG:
        return (mark_price - position.entry_price) * position.quantity
    if position.direction == PositionDirection.SHORT:
        return (position.entry_price - mark_price) * position.quantity
    return Decimal("0")


def _volume_participation(
    quantity: Decimal,
    candle_volume: Decimal | None,
) -> Decimal:
    volume = Decimal(str(candle_volume or 0))
    if volume <= 0:
        return Decimal("1") if quantity > 0 else Decimal("0")
    return abs(Decimal(str(quantity))) / volume


def _funding_boundaries_between(entry_time: str, exit_time: str) -> list[int]:
    entry = _timestamp_seconds(entry_time)
    exit_ = _timestamp_seconds(exit_time)
    if entry is None or exit_ is None or exit_ <= entry:
        return []
    period_seconds = 8 * 60 * 60
    first = ((entry // period_seconds) + 1) * period_seconds
    if first > exit_:
        return []
    return list(range(first, exit_ + 1, period_seconds))


def _timestamp_seconds(value: str) -> int | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def _candle_close_timestamp(candle: Candle) -> str:
    return datetime.fromtimestamp(
        candle.close_time_ms / 1000,
        tz=timezone.utc,
    ).isoformat()
