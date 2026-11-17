"""Single versioned trading policy shared by Governor, backtest, data recording and reconciliation.

The central rule of the opportunity forecast plan requires every forecast and every training
sample to be tied to an opportunity evaluated and resolved by the same operational policy:

- entry fill rule (market at ``NEXT_OPEN`` or ``SIGNAL_CLOSE``, or limit with timeout and
  optional market fallback);
- exit barriers (strategy ``PlannedExit`` when valid, ``ExitPlanner``, or static profile);
- protection buffer and working type;
- maximum holding horizon in primary candles;
- per-outcome costs (maker take, taker stop with stop slippage, signed funding);
- intrabar ambiguity resolution and lower-timeframe refinement;
- anti-HFT cadence and exchange symbol rules.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from domain.barriers import (
    AMBIGUITY_STOP_FIRST,
    LIQUIDATION,
    LONG,
    NOT_FILLED,
    OPEN,
    SHORT,
    SL_FIRST,
    STOP_LOSS,
    TAKE_PROFIT,
    TIMEOUT,
    TP_FIRST,
    AmbiguousRejected,
    BarrierHit,
    BarrierResolution,
    Barriers,
    SubCandles,
    first_barrier,
    hit_after_limit_fill,
    limit_entry_touched,
)
from domain.models import FuturesPosition, PositionDirection, TradeAction
from domain.opportunity import (
    DataWatermark,
    ExecutabilityCheck,
    ExecutabilityCheckItem,
    NotExecutable,
    OpportunityOutcome,
    OpportunityPlan,
    OutcomeDistribution,
    PathDependentContext,
    TradeOpportunity,
    build_opportunity_id,
    format_knowledge_time,
    map_barrier_kind_to_outcome,
    summarize_outcomes,
)
from domain.symbol_rules import SymbolRules
from domain.timeframe import (
    is_signal_timeframe_allowed_for_real,
    is_supported_timeframe,
    timeframe_duration_ms,
)
from futures.costs.model import CostModel
from futures.costs.outcome import BPS, OutcomeCosts, estimate_outcome_costs
from futures.planned_exit import (
    GLOBAL_FALLBACK_EXIT_SOURCE,
    STRATEGY_METADATA_EXIT_SOURCE,
    has_strategy_planned_exit_metadata,
    resolve_strategy_planned_exit,
)
from futures.planning.exit_planner import ExitPlanner
from futures.planning.trade_plan_builder import TradePlanBuilder
from futures.protection import normalize_planned_exit_prices
from trading.context import TradeContext
from trading.intent import TradeIntent
from trading.plan import TradePlan
from trading_cadence import (
    DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES,
    MAX_DAILY_TRADES_CEILING,
    effective_max_daily_trades,
    effective_min_seconds_between_entries,
    entry_cadence_block,
)

TRADING_POLICY_CODE_VERSION = "1.0.0"
DEFAULT_MAX_HOLDING_BARS = 16


def _norm_dec(value: Any) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _enum_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    raw = getattr(value, "value", value)
    return str(raw or default).strip()


def _direction_for_action(action: str) -> str:
    normalized = str(action or "").upper().strip()
    if normalized in {"OPEN_LONG", LONG}:
        return LONG
    if normalized in {"OPEN_SHORT", SHORT}:
        return SHORT
    return ""


@dataclass(frozen=True)
class TradingPolicy:
    """Versioned operational trading policy shared across live, backtest and research."""

    code_version: str = TRADING_POLICY_CODE_VERSION
    variant: str = "default"
    signal_timing_policy: str = "ON_CLOSE"
    market_entry_policy: str = "NEXT_OPEN"
    entry_order_type: str = "MARKET"
    limit_activation_policy: str = "NEXT_CANDLE"
    limit_entry_timeout_candles: int = 1
    limit_fallback_to_market: bool = False
    require_limit_trade_through: bool = True
    assume_limit_maker: bool = True
    limit_offset_bps: Decimal = Decimal("0")
    exit_planner: ExitPlanner = field(default_factory=ExitPlanner)
    cost_model: CostModel = field(default_factory=CostModel)
    dynamic_exit_enabled: bool = False
    dynamic_sizing_enabled: bool = False
    max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS
    ambiguity_policy: str = AMBIGUITY_STOP_FIRST
    protection_working_type: str = "MARK_PRICE"
    protection_trigger_buffer_bps: Decimal = Decimal("3")
    stop_slippage_bps: Decimal | None = None
    default_funding_events: int = 1
    min_seconds_between_entries: float = DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES
    max_daily_trades: int = MAX_DAILY_TRADES_CEILING
    primary_timeframe: str = "15m"
    execution_mode: str = "PAPER"

    def parameters_dict(self) -> dict[str, Any]:
        """Normalized rule parameters hashed into ``version`` (mode-independent)."""

        planner = self.exit_planner
        costs = self.cost_model
        stop_slippage = (
            self.stop_slippage_bps
            if self.stop_slippage_bps is not None
            else costs.estimated_slippage_bps
        )
        return {
            "code_version": str(self.code_version),
            "variant": str(self.variant or "default"),
            "primary_timeframe": str(self.primary_timeframe or "15m").lower().strip(),
            "signal_timing_policy": str(self.signal_timing_policy or "ON_CLOSE").upper().strip(),
            "market_entry_policy": str(self.market_entry_policy or "NEXT_OPEN").upper().strip(),
            "entry_order_type": str(self.entry_order_type or "MARKET").upper().strip(),
            "limit_activation_policy": (
                str(self.limit_activation_policy or "NEXT_CANDLE").upper().strip()
            ),
            "limit_entry_timeout_candles": max(0, int(self.limit_entry_timeout_candles)),
            "limit_fallback_to_market": bool(self.limit_fallback_to_market),
            "require_limit_trade_through": bool(self.require_limit_trade_through),
            "assume_limit_maker": bool(self.assume_limit_maker),
            "exit_model": str(planner.exit_model).upper().strip(),
            "stop_loss_pct": _norm_dec(planner.stop_loss_pct),
            "take_profit_pct": _norm_dec(planner.take_profit_pct),
            "atr_period": int(planner.atr_period),
            "atr_stop_multiplier": _norm_dec(planner.atr_stop_multiplier),
            "atr_take_multiplier": _norm_dec(planner.atr_take_multiplier),
            "min_net_gain_bps": _norm_dec(planner.min_net_gain_bps),
            "min_net_reward_risk": _norm_dec(planner.min_net_reward_risk),
            "max_economic_take_bps": _norm_dec(planner.max_economic_take_bps),
            "max_economic_take_atr_multiplier": _norm_dec(planner.max_economic_take_atr_multiplier),
            "economic_exit_adjustment_enabled": bool(planner.economic_exit_adjustment_enabled),
            "dynamic_exit_enabled": bool(self.dynamic_exit_enabled),
            "dynamic_sizing_enabled": bool(self.dynamic_sizing_enabled),
            "max_holding_bars": max(1, int(self.max_holding_bars)),
            "ambiguity_policy": str(self.ambiguity_policy or AMBIGUITY_STOP_FIRST).upper().strip(),
            "protection_working_type": (
                str(self.protection_working_type or "MARK_PRICE").upper().strip()
            ),
            "protection_trigger_buffer_bps": _norm_dec(self.protection_trigger_buffer_bps),
            "maker_fee_pct": _norm_dec(costs.maker_fee_pct),
            "taker_fee_pct": _norm_dec(costs.taker_fee_pct),
            "estimated_spread_bps": _norm_dec(costs.estimated_spread_bps),
            "estimated_slippage_bps": _norm_dec(costs.estimated_slippage_bps),
            "estimated_funding_rate": _norm_dec(costs.estimated_funding_rate),
            "stop_slippage_bps": _norm_dec(stop_slippage),
            "default_funding_events": max(0, int(self.default_funding_events)),
            "min_seconds_between_entries": _norm_dec(
                effective_min_seconds_between_entries(self.min_seconds_between_entries)
            ),
            "max_daily_trades": effective_max_daily_trades(self.max_daily_trades),
        }

    @property
    def version(self) -> str:
        canonical = json.dumps(self.parameters_dict(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        return f"trading-policy@{digest}"

    @property
    def plan_builder(self) -> TradePlanBuilder:
        return TradePlanBuilder(
            cost_model=self.cost_model,
            exit_planner=self.exit_planner,
            policy_version=self.version,
        )

    @classmethod
    def from_config(
        cls,
        config: Any = None,
        *,
        symbol_config: Any = None,
        variant: str = "default",
        **overrides: Any,
    ) -> TradingPolicy:
        """Build the operational policy from ``AppConfig`` and optional symbol overrides."""

        if config is None:
            base = cls(variant=variant)
            return base.with_variant(variant, **overrides) if overrides else base

        source = symbol_config if symbol_config is not None else config
        planner = ExitPlanner(
            exit_model=_enum_str(
                getattr(source, "exit_model", getattr(config, "exit_model", "FIXED_PCT")),
                "FIXED_PCT",
            ),
            stop_loss_pct=Decimal(
                str(getattr(source, "stop_loss_pct", getattr(config, "stop_loss_pct", "0.01")))
            ),
            take_profit_pct=Decimal(
                str(getattr(source, "take_profit_pct", getattr(config, "take_profit_pct", "0.02")))
            ),
            atr_period=int(getattr(source, "atr_period", getattr(config, "atr_period", 14))),
            atr_stop_multiplier=Decimal(
                str(
                    getattr(
                        source,
                        "atr_stop_multiplier",
                        getattr(config, "atr_stop_multiplier", "1.5"),
                    )
                )
            ),
            atr_take_multiplier=Decimal(
                str(
                    getattr(
                        source,
                        "atr_take_multiplier",
                        getattr(config, "atr_take_multiplier", "2.0"),
                    )
                )
            ),
            min_net_gain_bps=Decimal(str(getattr(config, "min_net_gain_bps", "5"))),
            min_net_reward_risk=Decimal(str(getattr(config, "min_net_reward_risk", "1.5"))),
            max_economic_take_bps=Decimal(str(getattr(config, "max_economic_take_bps", "120"))),
            max_economic_take_atr_multiplier=Decimal(
                str(getattr(config, "max_economic_take_atr_multiplier", "3.0"))
            ),
            economic_exit_adjustment_enabled=bool(
                getattr(config, "economic_exit_adjustment_enabled", True)
            ),
            economic_exit_diagnostics_enabled=bool(
                getattr(config, "economic_exit_diagnostics_enabled", True)
            ),
        )
        cost_model = CostModel.from_config(config)
        timeframe = str(
            getattr(source, "primary_timeframe", None)
            or getattr(config, "primary_timeframe", None)
            or "15m"
        )
        mode = _enum_str(getattr(config, "execution_mode", "PAPER"), "PAPER").upper()
        working_raw = getattr(
            source,
            "protection_working_type",
            getattr(source, "working_type", getattr(config, "working_type", "MARK_PRICE")),
        )
        horizon = int(getattr(config, "opportunity_max_holding_bars", DEFAULT_MAX_HOLDING_BARS))
        policy = cls(
            variant=variant,
            exit_planner=planner,
            cost_model=cost_model,
            dynamic_exit_enabled=bool(getattr(config, "dynamic_exit_enabled", False)),
            dynamic_sizing_enabled=bool(getattr(config, "dynamic_sizing_enabled", False)),
            max_holding_bars=max(1, horizon),
            protection_working_type=_enum_str(working_raw, "MARK_PRICE").upper(),
            min_seconds_between_entries=effective_min_seconds_between_entries(
                getattr(
                    source,
                    "min_seconds_between_entries",
                    getattr(
                        config, "min_seconds_between_entries", DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES
                    ),
                )
            ),
            max_daily_trades=effective_max_daily_trades(
                getattr(
                    source,
                    "max_daily_trades",
                    getattr(config, "max_daily_trades", MAX_DAILY_TRADES_CEILING),
                )
            ),
            primary_timeframe=timeframe,
            execution_mode=mode,
        )
        return policy.with_variant(variant, **overrides) if overrides else policy

    def for_symbol(self, symbol_config: Any) -> TradingPolicy:
        """Return a policy specialized with ``symbol_config`` exit and cadence overrides."""

        if symbol_config is None:
            return self
        overrides: dict[str, Any] = {}
        if getattr(symbol_config, "exit_model", None) is not None:
            overrides["exit_model"] = _enum_str(symbol_config.exit_model)
        for key in (
            "stop_loss_pct",
            "take_profit_pct",
            "atr_period",
            "atr_stop_multiplier",
            "atr_take_multiplier",
        ):
            val = getattr(symbol_config, key, None)
            if val is not None:
                overrides[key] = val
        working = getattr(
            symbol_config,
            "protection_working_type",
            getattr(symbol_config, "working_type", None),
        )
        if working is not None:
            overrides["protection_working_type"] = _enum_str(working, self.protection_working_type)
        max_daily = getattr(symbol_config, "max_daily_trades", None)
        if max_daily is not None and int(max_daily or 0) > 0:
            overrides["max_daily_trades"] = effective_max_daily_trades(max_daily)
        if getattr(symbol_config, "min_seconds_between_entries", None) is not None:
            overrides["min_seconds_between_entries"] = effective_min_seconds_between_entries(
                symbol_config.min_seconds_between_entries
            )
        return self.with_variant(self.variant, **overrides) if overrides else self

    def with_variant(self, variant: str, **overrides: Any) -> TradingPolicy:
        """Create a deployable policy variant with updated exit, cost or entry rules."""

        exit_fields = {
            "exit_model",
            "stop_loss_pct",
            "take_profit_pct",
            "atr_period",
            "atr_stop_multiplier",
            "atr_take_multiplier",
            "min_net_gain_bps",
            "min_net_reward_risk",
            "max_economic_take_bps",
            "max_economic_take_atr_multiplier",
            "economic_exit_adjustment_enabled",
        }
        cost_fields = {
            "maker_fee_pct",
            "taker_fee_pct",
            "estimated_spread_bps",
            "estimated_slippage_bps",
            "estimated_funding_rate",
        }
        planner_kwargs = {
            key: getattr(self.exit_planner, key)
            for key in (
                "exit_model",
                "stop_loss_pct",
                "take_profit_pct",
                "atr_period",
                "atr_stop_multiplier",
                "atr_take_multiplier",
                "min_net_gain_bps",
                "min_net_reward_risk",
                "max_economic_take_bps",
                "max_economic_take_atr_multiplier",
                "economic_exit_adjustment_enabled",
                "economic_exit_diagnostics_enabled",
            )
        }
        for key in exit_fields & overrides.keys():
            planner_kwargs[key] = overrides.pop(key)
        planner = ExitPlanner(**planner_kwargs)

        cost_kwargs = {
            "maker_fee_pct": self.cost_model.maker_fee_pct,
            "taker_fee_pct": self.cost_model.taker_fee_pct,
            "estimated_spread_bps": self.cost_model.estimated_spread_bps,
            "estimated_slippage_bps": self.cost_model.estimated_slippage_bps,
            "estimated_funding_rate": self.cost_model.estimated_funding_rate,
        }
        for key in cost_fields & overrides.keys():
            cost_kwargs[key] = Decimal(str(overrides.pop(key)))
        costs = CostModel(**cost_kwargs)

        return replace(
            self,
            variant=variant,
            exit_planner=planner,
            cost_model=costs,
            **overrides,
        )

    def build_plan(self, *, intent: TradeIntent, context: TradeContext) -> TradePlan:
        """Build a ``TradePlan`` using this policy's cost model and exit planner."""

        return self.plan_builder.build(intent=intent, context=context)

    def expected_funding_events(self, timeframe: str | None = None) -> int:
        tf = str(timeframe or self.primary_timeframe or "15m").lower().strip()
        if not is_supported_timeframe(tf):
            return max(0, int(self.default_funding_events))
        holding_ms = timeframe_duration_ms(tf) * max(1, int(self.max_holding_bars))
        eight_hours_ms = 8 * 3_600_000
        return max(int(self.default_funding_events), holding_ms // eight_hours_ms)

    def outcome_costs(
        self,
        *,
        direction: str,
        entry_price: Decimal,
        stop_price: Decimal,
        take_price: Decimal,
        context: TradeContext | None = None,
        market_entry: bool | None = None,
        funding_events: int | None = None,
    ) -> OutcomeCosts | None:
        spread_bps = (
            abs(context.spread_pct) * BPS
            if context is not None and context.spread_pct is not None
            else None
        )
        funding_rate = context.funding_rate if context is not None else None
        is_market = (
            (str(self.entry_order_type).upper().strip() == "MARKET")
            if market_entry is None
            else bool(market_entry)
        )
        events = (
            self.expected_funding_events()
            if funding_events is None
            else max(0, int(funding_events))
        )
        return estimate_outcome_costs(
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            take_price=take_price,
            cost_model=self.cost_model,
            spread_bps=spread_bps,
            funding_rate=funding_rate,
            funding_events=events,
            market_entry=is_market,
            take_exit_maker=True,
            stop_slippage_bps=self.stop_slippage_bps,
        )

    def _resolve_entry_prices(
        self,
        *,
        direction: str,
        context: TradeContext,
        order_type: str,
        limit_price: Decimal | None,
    ) -> tuple[Decimal | None, Decimal]:
        resolved_limit = Decimal(str(limit_price)) if limit_price is not None else None
        if (
            order_type == "LIMIT"
            and resolved_limit is None
            and Decimal(str(self.limit_offset_bps)) > 0
            and context.market_price > 0
        ):
            offset = Decimal(str(self.limit_offset_bps)) / BPS
            if direction == LONG:
                resolved_limit = context.exchange_rules.round_price_down(
                    context.market_price * (Decimal("1") - offset)
                )
            elif direction == SHORT:
                resolved_limit = context.exchange_rules.round_price_up(
                    context.market_price * (Decimal("1") + offset)
                )
        effective_entry = (
            resolved_limit
            if order_type == "LIMIT" and resolved_limit is not None and resolved_limit > 0
            else Decimal(str(context.market_price))
        )
        return resolved_limit, effective_entry

    def _resolve_opportunity_trade_plan(
        self,
        *,
        intent: TradeIntent,
        context: TradeContext,
        plan: TradePlan | None,
    ) -> TradePlan:
        if (
            plan is not None
            and not getattr(context.position, "is_open", False)
            and plan.quantity > 0
        ):
            return plan
        sizing_context = (
            replace(
                context,
                position=FuturesPosition(
                    context.symbol,
                    PositionDirection.NONE,
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                ),
            )
            if getattr(context.position, "is_open", False)
            else context
        )
        return self.build_plan(intent=intent, context=sizing_context)

    def opportunity_at(
        self,
        *,
        intent: TradeIntent,
        context: TradeContext,
        score: Any = None,
        plan: TradePlan | None = None,
        knowledge_time_ms: int | None = None,
        timeframe: str | None = None,
        limit_price: Decimal | None = None,
        entry_order_type: str | None = None,
        opened_trades_today: int = 0,
        seconds_since_last_entry: float | None = None,
    ) -> TradeOpportunity | NotExecutable:
        """Construct and check an operable ``TradeOpportunity`` at the decision instant."""

        trade_plan = self._resolve_opportunity_trade_plan(intent=intent, context=context, plan=plan)
        direction = _direction_for_action(intent.action)
        tf = str(
            timeframe
            or _timeframe_from_score(score)
            or getattr(context.symbol_config, "primary_timeframe", None)
            or self.primary_timeframe
            or "15m"
        ).strip()
        order_type = str(entry_order_type or self.entry_order_type or "MARKET").upper().strip()
        resolved_limit, effective_entry = self._resolve_entry_prices(
            direction=direction,
            context=context,
            order_type=order_type,
            limit_price=limit_price,
        )
        stop_price, take_price, exit_source, dynamic_used = self._resolve_barriers(
            intent=intent,
            context=context,
            trade_plan=trade_plan,
            score=score,
            entry_price=effective_entry,
        )
        costs = (
            self.outcome_costs(
                direction=direction,
                entry_price=effective_entry,
                stop_price=stop_price,
                take_price=take_price,
                context=context,
                market_entry=(order_type == "MARKET"),
            )
            if direction and stop_price > 0 and take_price > 0
            else None
        )
        watermark, watermark_ms, resolved_knowledge_ms = _resolve_watermark_and_knowledge_time(
            context=context,
            timeframe=tf,
            signal_timing_policy=self.signal_timing_policy,
            explicit_knowledge_time_ms=knowledge_time_ms,
            fallback_created_at=intent.created_at,
        )
        opp_plan = OpportunityPlan(
            entry_order_type=order_type,
            market_entry_policy=self.market_entry_policy,
            entry_reference_price=Decimal(str(context.market_price)),
            limit_price=resolved_limit,
            limit_timeout_candles=max(0, int(self.limit_entry_timeout_candles)),
            limit_fallback_to_market=bool(self.limit_fallback_to_market),
            stop_price=stop_price,
            take_profit_price=take_price,
            exit_source=exit_source,
            dynamic_exit_used=dynamic_used,
            max_holding_bars=max(1, int(self.max_holding_bars)),
            timeframe=tf,
            quantity=trade_plan.quantity,
            notional=trade_plan.notional,
            reward_risk=trade_plan.reward_risk,
            breakeven_probability=costs.breakeven_probability if costs is not None else None,
            r_win=costs.r_win if costs is not None else None,
            r_loss=costs.r_loss if costs is not None else None,
            cost_take_bps=costs.cost_take_bps if costs is not None else None,
            cost_stop_bps=costs.cost_stop_bps if costs is not None else None,
        )
        executability = self._check_executability(
            direction=direction,
            context=context,
            plan=opp_plan,
            watermark=watermark,
            knowledge_time_ms=resolved_knowledge_ms,
            opened_trades_today=opened_trades_today,
            seconds_since_last_entry=seconds_since_last_entry,
        )
        path_context = _build_path_context(context)
        origin = str(intent.strategy_name or getattr(score, "strategy", "") or "strategy")
        opp_id = build_opportunity_id(
            symbol=context.symbol,
            direction=direction or intent.action,
            watermark_time_ms=watermark_ms,
            origin=origin,
            policy_version=self.version,
            policy_variant=self.variant,
        )
        cls_to_use = TradeOpportunity if executability.executable else NotExecutable
        return cls_to_use(
            opportunity_id=opp_id,
            symbol=context.symbol,
            direction=direction,
            action=intent.action,
            origin=origin,
            policy_variant=self.variant,
            policy_version=self.version,
            knowledge_time=format_knowledge_time(resolved_knowledge_ms),
            knowledge_time_ms=resolved_knowledge_ms,
            data_watermark=watermark,
            plan=opp_plan,
            executability=executability,
            path_context=path_context,
            trade_plan_id=(plan.plan_id if plan is not None else trade_plan.plan_id),
        )

    def _resolve_barriers(
        self,
        *,
        intent: TradeIntent,
        context: TradeContext,
        trade_plan: TradePlan,
        score: Any,
        entry_price: Decimal,
    ) -> tuple[Decimal, Decimal, str, bool]:
        metadata = getattr(score, "metadata", None) if score is not None else None
        if has_strategy_planned_exit_metadata(metadata):
            present, _, validated = resolve_strategy_planned_exit(
                metadata,
                action=intent.action,
                entry_price=entry_price,
                price_tick=context.exchange_rules.price_tick,
            )
            if (
                present
                and validated.valid
                and validated.stop_price is not None
                and validated.take_profit_price is not None
            ):
                return (
                    validated.stop_price,
                    validated.take_profit_price,
                    STRATEGY_METADATA_EXIT_SOURCE,
                    True,
                )
        if trade_plan.stop_price is not None and trade_plan.take_profit_price is not None:
            return (
                trade_plan.stop_price,
                trade_plan.take_profit_price,
                GLOBAL_FALLBACK_EXIT_SOURCE,
                bool(self.dynamic_exit_enabled),
            )
        stop, take = _static_exit_prices(
            action=intent.action,
            entry=entry_price,
            rules=context.exchange_rules,
            stop_pct=self.exit_planner.stop_loss_pct,
            take_pct=self.exit_planner.take_profit_pct,
        )
        return stop, take, GLOBAL_FALLBACK_EXIT_SOURCE, False

    def _check_executability(
        self,
        *,
        direction: str,
        context: TradeContext,
        plan: OpportunityPlan,
        watermark: DataWatermark,
        knowledge_time_ms: int,
        opened_trades_today: int,
        seconds_since_last_entry: float | None,
    ) -> ExecutabilityCheck:
        checks = (
            _check_causal_watermark(context, watermark, knowledge_time_ms),
            _check_timeframe_allowed(plan.timeframe, self.execution_mode),
            _check_entry_rule(plan, context.exchange_rules),
            _check_exchange_rules(plan, context.exchange_rules),
            _check_protection_viable(
                direction=direction,
                plan=plan,
                context=context,
                working_type=self.protection_working_type,
                buffer_bps=self.protection_trigger_buffer_bps,
            ),
            _check_cadence(
                opened_trades_today=opened_trades_today,
                seconds_since_last_entry=seconds_since_last_entry,
                max_daily_trades=self.max_daily_trades,
                min_seconds_between_entries=self.min_seconds_between_entries,
            ),
        )
        failed = next((item for item in checks if not item.passed), None)
        return ExecutabilityCheck(
            executable=failed is None,
            rejection_reason=failed.reason if failed is not None else None,
            checks=checks,
        )

    def outcome(
        self,
        opportunity: TradeOpportunity,
        candles: Sequence[Any],
        *,
        sub_candles: SubCandles | None = None,
        price_tick: Decimal = Decimal("0"),
        liquidation_price: Decimal | None = None,
        funding_rate: Decimal | None = None,
        spread_bps: Decimal | None = None,
        reanchor_exit_on_fill: bool = False,
    ) -> OpportunityOutcome:
        """Resolve how ``opportunity`` ends on ``candles`` under this policy's fill & exit rules."""

        plan = opportunity.plan
        tick = max(Decimal("0"), Decimal(str(price_tick)))
        trade_through = tick if self.require_limit_trade_through else Decimal("0")
        if plan.entry_order_type == "LIMIT":
            return self._resolve_limit_outcome(
                opportunity=opportunity,
                candles=candles,
                sub_candles=sub_candles,
                trade_through=trade_through,
                price_tick=tick,
                liquidation_price=liquidation_price,
                funding_rate=funding_rate,
                spread_bps=spread_bps,
                reanchor_exit_on_fill=reanchor_exit_on_fill,
            )
        return self._resolve_market_outcome(
            opportunity=opportunity,
            candles=candles,
            sub_candles=sub_candles,
            trade_through=trade_through,
            price_tick=tick,
            liquidation_price=liquidation_price,
            funding_rate=funding_rate,
            spread_bps=spread_bps,
            reanchor_exit_on_fill=reanchor_exit_on_fill,
        )

    def _resolve_market_outcome(
        self,
        *,
        opportunity: TradeOpportunity,
        candles: Sequence[Any],
        sub_candles: SubCandles | None,
        trade_through: Decimal,
        price_tick: Decimal,
        liquidation_price: Decimal | None,
        funding_rate: Decimal | None,
        spread_bps: Decimal | None,
        reanchor_exit_on_fill: bool,
    ) -> OpportunityOutcome:
        plan = opportunity.plan
        if plan.market_entry_policy == "NEXT_OPEN":
            if not candles:
                return _unfilled_outcome(
                    opportunity,
                    outcome=OPEN,
                    fill_status="MARKET_NEXT_OPEN_PENDING",
                    exit_reason="next_open_unavailable",
                    wait_candles=0,
                )
            entry_price = Decimal(str(candles[0].open))
        else:
            entry_price = plan.entry_reference_price
        stop, take = self._effective_barriers_on_fill(
            opportunity=opportunity,
            entry_price=entry_price,
            price_tick=price_tick,
            reanchor_exit_on_fill=reanchor_exit_on_fill,
        )
        barriers = Barriers(
            direction=opportunity.direction,
            stop=stop,
            take=take,
            liquidation=liquidation_price,
            take_trigger_offset=trade_through,
        )
        resolution = first_barrier(
            barriers,
            candles,
            max_bars=plan.max_holding_bars,
            ambiguity_policy=self.ambiguity_policy,
            sub_candles=sub_candles,
        )
        return self._outcome_from_resolution(
            opportunity=opportunity,
            resolution=resolution,
            fill_status="MARKET_FILLED",
            entry_price=entry_price,
            stop_price=stop,
            take_price=take,
            entry_index=0,
            wait_candles=0,
            entry_maker=False,
            funding_rate=funding_rate,
            spread_bps=spread_bps,
        )

    def _resolve_limit_outcome(
        self,
        *,
        opportunity: TradeOpportunity,
        candles: Sequence[Any],
        sub_candles: SubCandles | None,
        trade_through: Decimal,
        price_tick: Decimal,
        liquidation_price: Decimal | None,
        funding_rate: Decimal | None,
        spread_bps: Decimal | None,
        reanchor_exit_on_fill: bool,
    ) -> OpportunityOutcome:
        plan = opportunity.plan
        limit_price = plan.limit_price
        timeout = max(0, int(plan.limit_timeout_candles))
        if limit_price is None or limit_price <= 0 or timeout <= 0:
            return _unfilled_outcome(
                opportunity,
                outcome=NOT_FILLED,
                fill_status="LIMIT_EXPIRED",
                exit_reason="limit_entry_expired",
                wait_candles=0,
            )
        window = candles[:timeout]
        for idx, candle in enumerate(window):
            wait = idx + 1
            if limit_entry_touched(
                opportunity.direction,
                candle,
                limit_price,
                trade_through_offset=trade_through,
            ):
                return self._resolve_after_limit_fill(
                    opportunity=opportunity,
                    fill_candle=candle,
                    remaining_candles=candles[idx + 1 :],
                    fill_index=idx,
                    wait_candles=wait,
                    entry_price=limit_price,
                    sub_candles=sub_candles,
                    trade_through=trade_through,
                    price_tick=price_tick,
                    liquidation_price=liquidation_price,
                    funding_rate=funding_rate,
                    spread_bps=spread_bps,
                    reanchor_exit_on_fill=reanchor_exit_on_fill,
                )
        if len(candles) < timeout:
            return _unfilled_outcome(
                opportunity,
                outcome=OPEN,
                fill_status="LIMIT_PENDING",
                exit_reason="limit_entry_pending",
                wait_candles=len(candles),
            )
        if plan.limit_fallback_to_market:
            fallback_index = timeout - 1
            entry_price = Decimal(str(candles[fallback_index].close))
            stop, take = self._effective_barriers_on_fill(
                opportunity=opportunity,
                entry_price=entry_price,
                price_tick=price_tick,
                reanchor_exit_on_fill=reanchor_exit_on_fill,
            )
            barriers = Barriers(
                direction=opportunity.direction,
                stop=stop,
                take=take,
                liquidation=liquidation_price,
                take_trigger_offset=trade_through,
            )
            after_fallback = candles[timeout:]
            if not after_fallback:
                resolution = BarrierResolution(
                    outcome=OPEN,
                    exit_price=entry_price,
                    exit_reason="open",
                    bars_held=0,
                    exit_index=fallback_index,
                )
            else:
                res = first_barrier(
                    barriers,
                    after_fallback,
                    max_bars=plan.max_holding_bars,
                    ambiguity_policy=self.ambiguity_policy,
                    sub_candles=sub_candles,
                )
                resolution = replace(res, exit_index=timeout + res.exit_index)
            return self._outcome_from_resolution(
                opportunity=opportunity,
                resolution=resolution,
                fill_status="LIMIT_FALLBACK_TO_MARKET",
                entry_price=entry_price,
                stop_price=stop,
                take_price=take,
                entry_index=fallback_index,
                wait_candles=timeout,
                entry_maker=False,
                funding_rate=funding_rate,
                spread_bps=spread_bps,
            )
        return _unfilled_outcome(
            opportunity,
            outcome=NOT_FILLED,
            fill_status="LIMIT_EXPIRED",
            exit_reason="limit_entry_expired",
            wait_candles=timeout,
            exit_index=timeout - 1,
        )

    def _resolve_after_limit_fill(
        self,
        *,
        opportunity: TradeOpportunity,
        fill_candle: Any,
        remaining_candles: Sequence[Any],
        fill_index: int,
        wait_candles: int,
        entry_price: Decimal,
        sub_candles: SubCandles | None,
        trade_through: Decimal,
        price_tick: Decimal,
        liquidation_price: Decimal | None,
        funding_rate: Decimal | None,
        spread_bps: Decimal | None,
        reanchor_exit_on_fill: bool,
    ) -> OpportunityOutcome:
        plan = opportunity.plan
        stop, take = self._effective_barriers_on_fill(
            opportunity=opportunity,
            entry_price=entry_price,
            price_tick=price_tick,
            reanchor_exit_on_fill=reanchor_exit_on_fill,
        )
        barriers = Barriers(
            direction=opportunity.direction,
            stop=stop,
            take=take,
            liquidation=liquidation_price,
            take_trigger_offset=trade_through,
        )
        same_candle_hit = hit_after_limit_fill(barriers, fill_candle)
        if isinstance(same_candle_hit, BarrierHit):
            resolution = BarrierResolution(
                outcome=same_candle_hit.kind,
                exit_price=same_candle_hit.price,
                exit_reason=same_candle_hit.exit_reason,
                bars_held=1,
                exit_index=fill_index,
                maker_exit=same_candle_hit.maker,
                ambiguous=same_candle_hit.ambiguous,
                ambiguity_resolution=same_candle_hit.ambiguity_resolution,
            )
        elif plan.max_holding_bars <= 1:
            resolution = BarrierResolution(
                outcome=TIMEOUT,
                exit_price=Decimal(str(fill_candle.close)),
                exit_reason="time_barrier",
                bars_held=1,
                exit_index=fill_index,
                ambiguous=isinstance(same_candle_hit, AmbiguousRejected),
                ambiguity_resolution=(
                    same_candle_hit.ambiguity_resolution
                    if isinstance(same_candle_hit, AmbiguousRejected)
                    else ""
                ),
            )
        elif not remaining_candles:
            resolution = BarrierResolution(
                outcome=OPEN,
                exit_price=Decimal(str(fill_candle.close)),
                exit_reason="open",
                bars_held=1,
                exit_index=fill_index,
                ambiguous=isinstance(same_candle_hit, AmbiguousRejected),
                ambiguity_resolution=(
                    same_candle_hit.ambiguity_resolution
                    if isinstance(same_candle_hit, AmbiguousRejected)
                    else ""
                ),
            )
        else:
            later = first_barrier(
                barriers,
                remaining_candles,
                max_bars=plan.max_holding_bars - 1,
                ambiguity_policy=self.ambiguity_policy,
                sub_candles=sub_candles,
            )
            resolution = replace(
                later,
                bars_held=later.bars_held + 1,
                exit_index=fill_index + 1 + later.exit_index,
            )
        return self._outcome_from_resolution(
            opportunity=opportunity,
            resolution=resolution,
            fill_status="LIMIT_FILLED",
            entry_price=entry_price,
            stop_price=stop,
            take_price=take,
            entry_index=fill_index,
            wait_candles=wait_candles,
            entry_maker=bool(self.assume_limit_maker),
            funding_rate=funding_rate,
            spread_bps=spread_bps,
        )

    def _effective_barriers_on_fill(
        self,
        *,
        opportunity: TradeOpportunity,
        entry_price: Decimal,
        price_tick: Decimal,
        reanchor_exit_on_fill: bool,
    ) -> tuple[Decimal, Decimal]:
        plan = opportunity.plan
        if reanchor_exit_on_fill and not plan.dynamic_exit_used and price_tick > 0:
            rules = SymbolRules(
                symbol=opportunity.symbol,
                quantity_step=Decimal("0.0001"),
                min_quantity=Decimal("0.0001"),
                min_notional=Decimal("0"),
                price_tick=price_tick,
            )
            return _static_exit_prices(
                action=opportunity.action,
                entry=entry_price,
                rules=rules,
                stop_pct=self.exit_planner.stop_loss_pct,
                take_pct=self.exit_planner.take_profit_pct,
            )
        if price_tick > 0:
            rules = SymbolRules(
                symbol=opportunity.symbol,
                quantity_step=Decimal("0.0001"),
                min_quantity=Decimal("0.0001"),
                min_notional=Decimal("0"),
                price_tick=price_tick,
            )
            try:
                return normalize_planned_exit_prices(
                    action=TradeAction(opportunity.action),
                    entry_price=entry_price,
                    rules=rules,
                    planned_stop_price=plan.stop_price,
                    planned_take_profit_price=plan.take_profit_price,
                )
            except ValueError:
                return plan.stop_price, plan.take_profit_price
        return plan.stop_price, plan.take_profit_price

    def _outcome_from_resolution(
        self,
        *,
        opportunity: TradeOpportunity,
        resolution: BarrierResolution,
        fill_status: str,
        entry_price: Decimal,
        stop_price: Decimal,
        take_price: Decimal,
        entry_index: int,
        wait_candles: int,
        entry_maker: bool,
        funding_rate: Decimal | None,
        spread_bps: Decimal | None,
    ) -> OpportunityOutcome:
        outcome_label = map_barrier_kind_to_outcome(resolution.outcome)
        gross_r, cost_r, net_r, net_bps = self._realized_r(
            direction=opportunity.direction,
            entry_price=entry_price,
            exit_price=resolution.exit_price,
            stop_price=stop_price,
            outcome_label=outcome_label,
            entry_maker=entry_maker,
            maker_exit=resolution.maker_exit,
            bars_held=resolution.bars_held,
            timeframe=opportunity.plan.timeframe,
            funding_rate=funding_rate,
            spread_bps=spread_bps,
        )
        return OpportunityOutcome(
            opportunity_id=opportunity.opportunity_id,
            policy_version=self.version,
            outcome=outcome_label,
            filled=True,
            fill_status=fill_status,
            entry_price=entry_price,
            exit_price=resolution.exit_price,
            stop_price=stop_price,
            take_profit_price=take_price,
            exit_reason=resolution.exit_reason,
            entry_index=entry_index,
            exit_index=resolution.exit_index,
            wait_candles=wait_candles,
            bars_held=resolution.bars_held,
            entry_maker=entry_maker,
            maker_exit=resolution.maker_exit,
            gap=resolution.gap,
            ambiguous=resolution.ambiguous,
            ambiguity_resolution=resolution.ambiguity_resolution,
            refined=resolution.refined,
            gross_r=gross_r,
            cost_r=cost_r,
            net_r=net_r,
            net_bps=net_bps,
        )

    def _realized_r(
        self,
        *,
        direction: str,
        entry_price: Decimal,
        exit_price: Decimal,
        stop_price: Decimal,
        outcome_label: str,
        entry_maker: bool,
        maker_exit: bool,
        bars_held: int,
        timeframe: str,
        funding_rate: Decimal | None,
        spread_bps: Decimal | None,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        long = direction == LONG
        risk = abs(entry_price - stop_price)
        if entry_price <= 0 or risk <= 0:
            return Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")

        gross_move = (exit_price - entry_price) if long else (entry_price - exit_price)
        half_spread = (
            spread_bps if spread_bps is not None else self.cost_model.estimated_spread_bps
        ) / Decimal("2")
        slippage = self.cost_model.estimated_slippage_bps
        stop_slippage = self.stop_slippage_bps if self.stop_slippage_bps is not None else slippage
        taker = self.cost_model.taker_fee_pct
        maker = self.cost_model.maker_fee_pct

        entry_cost = entry_price * (maker if entry_maker else taker)
        if not entry_maker:
            entry_cost += entry_price * (half_spread + slippage) / BPS

        exit_slippage = stop_slippage if outcome_label == SL_FIRST else slippage
        exit_cost = exit_price * (maker if maker_exit else taker)
        if not maker_exit:
            exit_cost += exit_price * (half_spread + exit_slippage) / BPS

        rate = funding_rate if funding_rate is not None else self.cost_model.estimated_funding_rate
        events = _funding_events_for_bars(bars_held, timeframe, self.default_funding_events)
        funding = entry_price * Decimal(str(rate)) * Decimal(events) * (1 if long else -1)

        total_cost = entry_cost + exit_cost + funding
        gross_r = gross_move / risk
        cost_r = total_cost / risk
        net_r = (gross_move - total_cost) / risk
        net_bps = (gross_move - total_cost) / entry_price * BPS
        return gross_r, cost_r, net_r, net_bps

    @staticmethod
    def summarize_outcomes(outcomes: Sequence[OpportunityOutcome]) -> OutcomeDistribution:
        """Compute empirical ``p_fill`` and conditional barrier rates from policy outcomes."""

        return summarize_outcomes(outcomes)


def _funding_events_for_bars(bars_held: int, timeframe: str, default_events: int) -> int:
    tf = str(timeframe or "15m").lower().strip()
    if not is_supported_timeframe(tf):
        return max(0, int(default_events))
    held_ms = max(1, int(bars_held)) * timeframe_duration_ms(tf)
    eight_hours_ms = 8 * 3_600_000
    return max(int(default_events), held_ms // eight_hours_ms)


def _unfilled_outcome(
    opportunity: TradeOpportunity,
    *,
    outcome: str,
    fill_status: str,
    exit_reason: str,
    wait_candles: int,
    exit_index: int = -1,
) -> OpportunityOutcome:
    plan = opportunity.plan
    ref_price = plan.limit_price if plan.limit_price is not None else plan.entry_reference_price
    return OpportunityOutcome(
        opportunity_id=opportunity.opportunity_id,
        policy_version=opportunity.policy_version,
        outcome=outcome,
        filled=False,
        fill_status=fill_status,
        entry_price=ref_price,
        exit_price=ref_price,
        stop_price=plan.stop_price,
        take_profit_price=plan.take_profit_price,
        exit_reason=exit_reason,
        entry_index=-1,
        exit_index=exit_index,
        wait_candles=wait_candles,
        bars_held=0,
    )


def _static_exit_prices(
    *,
    action: str,
    entry: Decimal,
    rules: SymbolRules,
    stop_pct: Decimal,
    take_pct: Decimal,
) -> tuple[Decimal, Decimal]:
    if _direction_for_action(action) == LONG:
        stop = rules.round_price_down(entry * (Decimal("1") - stop_pct))
        take = rules.round_price_up(entry * (Decimal("1") + take_pct))
        return stop, take
    stop = rules.round_price_up(entry * (Decimal("1") + stop_pct))
    take = rules.round_price_down(entry * (Decimal("1") - take_pct))
    return stop, take


def _timeframe_from_score(score: Any) -> str | None:
    metadata = getattr(score, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    for key in ("primary_timeframe", "signal_timeframe", "timeframe", "interval"):
        raw = metadata.get(key)
        if raw and is_supported_timeframe(str(raw)):
            return str(raw).strip().lower()
    return None


def _resolve_watermark_and_knowledge_time(
    *,
    context: TradeContext,
    timeframe: str,
    signal_timing_policy: str,
    explicit_knowledge_time_ms: int | None,
    fallback_created_at: float,
) -> tuple[DataWatermark, int, int]:
    candles = tuple(context.candles or ())
    open_ms: int | None = None
    close_ms: int | None = None
    if candles:
        last = candles[-1]
        raw_open = getattr(last, "open_time_ms", None)
        raw_close = getattr(last, "close_time_ms", None)
        open_ms = int(raw_open) if raw_open is not None else None
        close_ms = int(raw_close) if raw_close is not None else None
    timing = str(signal_timing_policy or "ON_CLOSE").upper().strip()
    if timing == "ON_OPEN" and open_ms is not None:
        candle_watermark_ms = open_ms
    elif close_ms is not None:
        candle_watermark_ms = close_ms + 1
    else:
        candle_watermark_ms = int(round(float(fallback_created_at) * 1000.0))
    knowledge_ms = (
        int(explicit_knowledge_time_ms)
        if explicit_knowledge_time_ms is not None
        else candle_watermark_ms
    )
    watermark = DataWatermark(
        timeframe=timeframe,
        last_closed_open_time_ms=open_ms,
        last_closed_close_time_ms=close_ms,
        candle_count=len(candles),
        funding_rate_known=context.funding_rate is not None,
        mark_price_known=context.mark_price is not None and context.mark_price > 0,
    )
    return watermark, candle_watermark_ms, knowledge_ms


def _check_causal_watermark(
    context: TradeContext,
    watermark: DataWatermark,
    knowledge_time_ms: int,
) -> ExecutabilityCheckItem:
    if context.stale:
        return ExecutabilityCheckItem(
            name="causal_watermark",
            passed=False,
            reason="STALE_MARKET_DATA",
            details={"data_age_seconds": context.data_age_seconds},
        )
    future_candles = [
        int(getattr(candle, "close_time_ms", 0))
        for candle in (context.candles or ())
        if getattr(candle, "close_time_ms", None) is not None
        and int(getattr(candle, "close_time_ms", 0)) > knowledge_time_ms
    ]
    if future_candles:
        return ExecutabilityCheckItem(
            name="causal_watermark",
            passed=False,
            reason="FUTURE_DATA_IN_CANDLE_WINDOW",
            details={
                "knowledge_time_ms": knowledge_time_ms,
                "max_candle_close_time_ms": max(future_candles),
            },
        )
    return ExecutabilityCheckItem(
        name="causal_watermark",
        passed=True,
        reason="CAUSAL_WATERMARK_OK",
        details={
            "knowledge_time_ms": knowledge_time_ms,
            "last_closed_close_time_ms": watermark.last_closed_close_time_ms,
        },
    )


def _check_timeframe_allowed(timeframe: str, execution_mode: str) -> ExecutabilityCheckItem:
    if not is_supported_timeframe(timeframe):
        return ExecutabilityCheckItem(
            name="timeframe_allowed",
            passed=False,
            reason="UNSUPPORTED_TIMEFRAME",
            details={"timeframe": timeframe},
        )
    mode = str(execution_mode or "").upper().strip()
    if mode in {"REAL", "REAL_LIVE"} and not is_signal_timeframe_allowed_for_real(timeframe):
        return ExecutabilityCheckItem(
            name="timeframe_allowed",
            passed=False,
            reason="REAL_MODE_TIMEFRAME_TOO_FAST",
            details={"timeframe": timeframe, "execution_mode": mode},
        )
    return ExecutabilityCheckItem(
        name="timeframe_allowed",
        passed=True,
        reason="TIMEFRAME_ALLOWED",
        details={"timeframe": timeframe, "execution_mode": mode},
    )


def _check_entry_rule(plan: OpportunityPlan, rules: SymbolRules) -> ExecutabilityCheckItem:
    if plan.entry_reference_price <= 0:
        return ExecutabilityCheckItem(
            name="entry_rule",
            passed=False,
            reason="ENTRY_PRICE_NOT_POSITIVE",
            details={"entry_reference_price": _norm_dec(plan.entry_reference_price)},
        )
    if plan.entry_order_type == "LIMIT":
        if plan.limit_price is None or plan.limit_price <= 0:
            return ExecutabilityCheckItem(
                name="entry_rule",
                passed=False,
                reason="LIMIT_PRICE_MISSING_OR_INVALID",
                details={"limit_price": _norm_dec(plan.limit_price) if plan.limit_price else None},
            )
        if plan.limit_timeout_candles < 1:
            return ExecutabilityCheckItem(
                name="entry_rule",
                passed=False,
                reason="LIMIT_TIMEOUT_NOT_POSITIVE",
                details={"limit_timeout_candles": plan.limit_timeout_candles},
            )
        rounded = rules.round_price(plan.limit_price)
        if rounded != plan.limit_price:
            return ExecutabilityCheckItem(
                name="entry_rule",
                passed=False,
                reason="LIMIT_PRICE_NOT_ON_TICK",
                details={
                    "limit_price": _norm_dec(plan.limit_price),
                    "rounded": _norm_dec(rounded),
                    "price_tick": _norm_dec(rules.price_tick),
                },
            )
    return ExecutabilityCheckItem(
        name="entry_rule",
        passed=True,
        reason="ENTRY_RULE_OK",
        details={
            "entry_order_type": plan.entry_order_type,
            "market_entry_policy": plan.market_entry_policy,
        },
    )


def _check_exchange_rules(plan: OpportunityPlan, rules: SymbolRules) -> ExecutabilityCheckItem:
    price = (
        plan.limit_price
        if plan.entry_order_type == "LIMIT" and plan.limit_price
        else plan.entry_reference_price
    )
    qty = plan.quantity
    if price <= 0 or qty <= 0:
        return ExecutabilityCheckItem(
            name="exchange_rules",
            passed=False,
            reason="QUANTITY_NOT_POSITIVE",
            details={"quantity": _norm_dec(qty), "price": _norm_dec(price)},
        )
    rounded = rules.round_quantity(qty)
    if rounded != qty:
        return ExecutabilityCheckItem(
            name="exchange_rules",
            passed=False,
            reason="QUANTITY_STEP_MISMATCH",
            details={
                "quantity": _norm_dec(qty),
                "rounded": _norm_dec(rounded),
                "quantity_step": _norm_dec(rules.quantity_step),
            },
        )
    if qty < rules.min_quantity:
        return ExecutabilityCheckItem(
            name="exchange_rules",
            passed=False,
            reason="BELOW_MIN_QUANTITY",
            details={"quantity": _norm_dec(qty), "min_quantity": _norm_dec(rules.min_quantity)},
        )
    notional = qty * price
    if rules.min_notional > 0 and notional < rules.min_notional:
        return ExecutabilityCheckItem(
            name="exchange_rules",
            passed=False,
            reason="BELOW_MIN_NOTIONAL",
            details={
                "notional": _norm_dec(notional),
                "min_notional": _norm_dec(rules.min_notional),
            },
        )
    return ExecutabilityCheckItem(
        name="exchange_rules",
        passed=True,
        reason="EXCHANGE_RULES_OK",
        details={
            "quantity": _norm_dec(qty),
            "notional": _norm_dec(notional),
            "min_notional": _norm_dec(rules.min_notional),
        },
    )


def _check_protection_viable(
    *,
    direction: str,
    plan: OpportunityPlan,
    context: TradeContext,
    working_type: str,
    buffer_bps: Decimal,
) -> ExecutabilityCheckItem:
    stop = plan.stop_price
    take = plan.take_profit_price
    entry = (
        plan.limit_price
        if plan.entry_order_type == "LIMIT" and plan.limit_price
        else plan.entry_reference_price
    )
    if direction not in {LONG, SHORT} or stop is None or take is None or stop <= 0 or take <= 0:
        return ExecutabilityCheckItem(
            name="protection_viable",
            passed=False,
            reason="PROTECTION_PRICES_MISSING_OR_INVALID",
            details={"stop_price": _norm_dec(stop), "take_profit_price": _norm_dec(take)},
        )
    long = direction == LONG
    if (stop >= entry or take <= entry) if long else (stop <= entry or take >= entry):
        return ExecutabilityCheckItem(
            name="protection_viable",
            passed=False,
            reason="PROTECTION_ON_WRONG_SIDE_OF_ENTRY",
            details={
                "direction": direction,
                "entry": _norm_dec(entry),
                "stop_price": _norm_dec(stop),
                "take_profit_price": _norm_dec(take),
            },
        )
    trigger_ref = (
        context.mark_price
        if str(working_type or "").upper().strip() == "MARK_PRICE"
        and context.mark_price is not None
        and context.mark_price > 0
        else entry
    )
    min_distance = max(
        context.exchange_rules.price_tick,
        abs(trigger_ref) * max(Decimal("0"), Decimal(str(buffer_bps))) / BPS,
    )
    stop_dist = (trigger_ref - stop) if long else (stop - trigger_ref)
    take_dist = (take - trigger_ref) if long else (trigger_ref - take)
    if stop_dist < min_distance or take_dist < min_distance:
        return ExecutabilityCheckItem(
            name="protection_viable",
            passed=False,
            reason="PROTECTION_WITHIN_TRIGGER_BUFFER",
            details={
                "trigger_reference": _norm_dec(trigger_ref),
                "min_buffer_distance": _norm_dec(min_distance),
                "stop_distance": _norm_dec(stop_dist),
                "take_distance": _norm_dec(take_dist),
            },
        )
    return ExecutabilityCheckItem(
        name="protection_viable",
        passed=True,
        reason="PROTECTION_VIABLE",
        details={
            "stop_price": _norm_dec(stop),
            "take_profit_price": _norm_dec(take),
            "min_buffer_distance": _norm_dec(min_distance),
        },
    )


def _check_cadence(
    *,
    opened_trades_today: int,
    seconds_since_last_entry: float | None,
    max_daily_trades: int,
    min_seconds_between_entries: float,
) -> ExecutabilityCheckItem:
    elapsed = Decimal("-1") if seconds_since_last_entry is None else seconds_since_last_entry
    blocked = entry_cadence_block(
        opened_trades_today=opened_trades_today,
        seconds_since_entry=elapsed,
        max_daily_trades=max_daily_trades,
        min_seconds_between_entries=min_seconds_between_entries,
    )
    if blocked == "min_seconds_between_entries":
        return ExecutabilityCheckItem(
            name="cadence",
            passed=False,
            reason="MIN_SECONDS_BETWEEN_ENTRIES",
            details={
                "seconds_since_last_entry": seconds_since_last_entry,
                "min_seconds_between_entries": effective_min_seconds_between_entries(
                    min_seconds_between_entries
                ),
            },
        )
    if blocked == "max_daily_trades":
        return ExecutabilityCheckItem(
            name="cadence",
            passed=False,
            reason="MAX_DAILY_TRADES",
            details={
                "opened_trades_today": opened_trades_today,
                "max_daily_trades": effective_max_daily_trades(max_daily_trades),
            },
        )
    return ExecutabilityCheckItem(
        name="cadence",
        passed=True,
        reason="CADENCE_OK",
        details={
            "opened_trades_today": opened_trades_today,
            "seconds_since_last_entry": seconds_since_last_entry,
        },
    )


def _build_path_context(context: TradeContext) -> PathDependentContext:
    snap = context.portfolio_snapshot if isinstance(context.portfolio_snapshot, dict) else {}
    open_positions = snap.get("open_positions")
    open_count = len(open_positions) if isinstance(open_positions, list) else 0
    exposure = Decimal("0")
    if isinstance(open_positions, list):
        for item in open_positions:
            if isinstance(item, dict) and item.get("notional") is not None:
                try:
                    exposure += abs(Decimal(str(item["notional"])))
                except Exception:
                    continue
    realized = _safe_decimal(snap.get("daily_realized_pnl"), Decimal("0")) or Decimal("0")
    available = _safe_decimal(snap.get("available_balance_usdt"), None)
    daily_trades = int(snap.get("daily_trades", 0) or 0)
    consecutive_losses = int(snap.get("consecutive_losses", 0) or 0)
    return PathDependentContext(
        position_open=bool(getattr(context.position, "is_open", False)),
        open_positions_count=open_count,
        gross_exposure_usdt=exposure,
        daily_realized_pnl_usdt=realized,
        daily_trades=daily_trades,
        consecutive_losses=consecutive_losses,
        available_balance_usdt=available,
    )


def _safe_decimal(raw: Any, default: Decimal | None) -> Decimal | None:
    if raw is None or raw == "":
        return default
    try:
        value = Decimal(str(raw))
    except Exception:
        return default
    return value if value.is_finite() else default


__all__ = [
    "DEFAULT_MAX_HOLDING_BARS",
    "LIQUIDATION",
    "LONG",
    "NOT_FILLED",
    "NotExecutable",
    "OPEN",
    "OpportunityOutcome",
    "OpportunityPlan",
    "OutcomeDistribution",
    "SHORT",
    "SL_FIRST",
    "STOP_LOSS",
    "TAKE_PROFIT",
    "TIMEOUT",
    "TP_FIRST",
    "TRADING_POLICY_CODE_VERSION",
    "TradeOpportunity",
    "TradingPolicy",
    "summarize_outcomes",
]
