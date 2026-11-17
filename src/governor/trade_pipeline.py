from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from config.symbol_context import SymbolTradingConfig
from domain.opportunity import TradeOpportunity
from futures.planning.trade_plan_builder import TradePlanBuilder
from futures.planning.trade_plan_comparison import (
    TradePlanComparison,
    compare_plan_to_profile_reference,
)
from futures.trading_policy import TradingPolicy
from governor.market_data_router import HeavyMarketSnapshot
from market_data.market_snapshots import (
    extract_funding_rate,
    extract_index_price,
    extract_next_funding_time,
)
from trading.context import TradeContext
from trading.enums import OPEN_ACTIONS
from trading.intent import TradeIntent
from trading.plan import TradePlan


class TradeContextBuilder:
    def build(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        portfolio_snapshot: dict[str, Any],
    ) -> TradeContext:
        if heavy_snapshot.exchange_rules is None:
            raise ValueError("TradeContext exige exchange_rules confirmadas")
        return TradeContext(
            symbol=heavy_snapshot.symbol,
            market_price=heavy_snapshot.market.price,
            mark_price=heavy_snapshot.mark_price,
            index_price=_index_price_from_heavy_snapshot(heavy_snapshot),
            spread_pct=_spread_pct_from_account_info(heavy_snapshot.account_info),
            volatility=None,
            atr=_atr_from_market(heavy_snapshot.market),
            funding_rate=_funding_rate_from_heavy_snapshot(heavy_snapshot),
            next_funding_time=_next_funding_time_from_heavy_snapshot(heavy_snapshot),
            position=heavy_snapshot.position,
            exchange_rules=heavy_snapshot.exchange_rules,
            symbol_config=symbol_config,
            portfolio_snapshot=portfolio_snapshot,
            data_age_seconds=heavy_snapshot.data_age_seconds,
            stale=heavy_snapshot.stale,
            candles=_candles_from_market(heavy_snapshot.market),
        )


@dataclass
class TradePipelineResult:
    intent: TradeIntent
    context: TradeContext
    plan: TradePlan
    comparison: TradePlanComparison
    opportunity: TradeOpportunity | None = None


class TradePipeline:
    def __init__(
        self,
        *,
        context_builder: TradeContextBuilder | None = None,
        plan_builder: TradePlanBuilder | None = None,
        trading_policy: TradingPolicy | None = None,
        config=None,
    ):
        self.context_builder = context_builder or TradeContextBuilder()
        if trading_policy is None:
            if config is not None:
                trading_policy = TradingPolicy.from_config(config)
            elif plan_builder is not None:
                trading_policy = TradingPolicy(
                    cost_model=plan_builder.cost_model,
                    exit_planner=plan_builder.exit_planner,
                )
            else:
                trading_policy = TradingPolicy()
        self.trading_policy = trading_policy
        self.plan_builder = plan_builder or self.trading_policy.plan_builder

    def build_plan(
        self,
        *,
        intent: TradeIntent,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        portfolio_snapshot: dict[str, Any],
        score: Any = None,
        knowledge_time_ms: int | None = None,
        timeframe: str | None = None,
        opened_trades_today: int = 0,
        seconds_since_last_entry: float | None = None,
    ) -> TradePipelineResult:
        """Build the operational trade plan and operable ``TradeOpportunity`` for execution.

        This is the canonical method for creating a trade plan that will be used
        by the economic gate and execution pipeline. ``build_shadow_plan`` is a
        backward-compatible alias.
        """
        context = self.context_builder.build(
            symbol_config=symbol_config,
            heavy_snapshot=heavy_snapshot,
            portfolio_snapshot=portfolio_snapshot,
        )
        symbol_policy = self.trading_policy.for_symbol(symbol_config)
        builder = (
            symbol_policy.plan_builder
            if self.plan_builder.policy_version is not None
            else self.plan_builder
        )
        plan = builder.build(intent=intent, context=context)
        comparison = compare_plan_to_profile_reference(plan=plan, context=context)
        opportunity: TradeOpportunity | None = None
        if intent.action in OPEN_ACTIONS:
            resolved_tf = timeframe or getattr(heavy_snapshot.market, "primary_timeframe", None)
            opportunity = symbol_policy.opportunity_at(
                intent=intent,
                context=context,
                score=score,
                plan=plan,
                knowledge_time_ms=knowledge_time_ms,
                timeframe=resolved_tf,
                opened_trades_today=opened_trades_today,
                seconds_since_last_entry=seconds_since_last_entry,
            )
        return TradePipelineResult(
            intent=intent,
            context=context,
            plan=plan,
            comparison=comparison,
            opportunity=opportunity,
        )

    def build_shadow_plan(
        self,
        *,
        intent: TradeIntent,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        portfolio_snapshot: dict[str, Any],
        score: Any = None,
        knowledge_time_ms: int | None = None,
        timeframe: str | None = None,
        opened_trades_today: int = 0,
        seconds_since_last_entry: float | None = None,
    ) -> TradePipelineResult:
        """Backward-compatible alias for ``build_plan``."""
        return self.build_plan(
            intent=intent,
            symbol_config=symbol_config,
            heavy_snapshot=heavy_snapshot,
            portfolio_snapshot=portfolio_snapshot,
            score=score,
            knowledge_time_ms=knowledge_time_ms,
            timeframe=timeframe,
            opened_trades_today=opened_trades_today,
            seconds_since_last_entry=seconds_since_last_entry,
        )


def _spread_pct_from_account_info(account_info: dict[str, Any]) -> Decimal | None:
    raw = account_info.get("estimatedSpreadPct")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


def _funding_rate_from_heavy_snapshot(heavy_snapshot: HeavyMarketSnapshot) -> Decimal | None:
    direct = getattr(heavy_snapshot, "funding_rate", None)
    if direct is not None:
        try:
            rate = Decimal(str(direct))
        except Exception:
            rate = None
        if rate is not None and rate.is_finite():
            return rate
    return extract_funding_rate(getattr(heavy_snapshot, "account_info", None))


def _next_funding_time_from_heavy_snapshot(heavy_snapshot: HeavyMarketSnapshot) -> int | None:
    direct = getattr(heavy_snapshot, "next_funding_time", None)
    if direct is not None:
        try:
            value = int(direct)
        except Exception:
            value = 0
        if value > 0:
            return value
    return extract_next_funding_time(getattr(heavy_snapshot, "account_info", None))


def _index_price_from_heavy_snapshot(heavy_snapshot: HeavyMarketSnapshot) -> Decimal | None:
    direct = getattr(heavy_snapshot, "index_price", None)
    if direct is not None:
        try:
            price = Decimal(str(direct))
        except Exception:
            price = None
        if price is not None and price.is_finite() and price > 0:
            return price
    return extract_index_price(getattr(heavy_snapshot, "account_info", None))


def _atr_from_market(market) -> Decimal | None:
    indicators = getattr(market, "indicators", {}) or {}
    if not isinstance(indicators, dict):
        return None
    for key in ("ATR", "atr"):
        raw = indicators.get(key)
        if raw is None:
            continue
        try:
            return Decimal(str(raw))
        except Exception:
            return None
    return None


def _candles_from_market(market) -> tuple[Any, ...]:
    candles = getattr(market, "candles", ()) or ()
    if isinstance(candles, dict):
        primary = getattr(market, "primary_timeframe", None)
        if primary and primary in candles:
            return tuple(candles[primary] or ())
        if "1m" in candles:
            return tuple(candles["1m"] or ())
        for value in candles.values():
            return tuple(value or ())
        return ()
    return tuple(candles)
