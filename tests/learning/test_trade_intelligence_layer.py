from dataclasses import replace
from decimal import Decimal

import pytest

from config.symbol_context import symbol_config_from_app_config
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeDecision
from domain.symbol_rules import SymbolRules
from futures.costs.model import COST_MODEL_VERSION, CostModel
from futures.planning.economic_gate import EconomicTradeGate
from futures.planning.exit_planner import ExitPlanner
from futures.planning.trade_plan_builder import TradePlanBuilder
from futures.planning.trade_plan_comparison import (
    PROFILE_REFERENCE_FLOW,
    compare_plan_to_profile_reference,
)
from governor.portfolio_allocator import PortfolioAllocator
from governor.strategy_coordinator import StrategyCoordinator
from scanner.signal_score import SignalScore
from trading.context import TradeContext
from trading.enums import TradePlanStatus
from trading.intent import TradeIntent
from trading.plan import TRADE_PLAN_SOURCE_SHADOW, TRADE_PLAN_VERSION, TradePlan


class GlobalIntentStrategy:
    def score_symbol(self, symbol, market, position, state):
        state["score_calls"] = int(state.get("score_calls", 0)) + 1
        return SignalScore(symbol=symbol, score=95, action="OPEN_LONG", reason=f"score {symbol}")

    def decide_symbol(self, symbol, market, position, state):
        state["decide_calls"] = int(state.get("decide_calls", 0)) + 1
        return TradeDecision.open_long(f"decide {symbol}")

    def intent_symbol(self, symbol, market, position, state):
        state["intent_calls"] = int(state.get("intent_calls", 0)) + 1
        return TradeIntent(
            symbol=symbol,
            action="OPEN_LONG",
            confidence=80 + state["intent_calls"],
            reason=f"intent {symbol}",
            strategy_name="tests.GlobalIntentStrategy",
            metadata={"calls": state["intent_calls"]},
        )


class ParamAwareGlobalStrategy(GlobalIntentStrategy):
    def score_symbol(self, symbol, market, position, state):
        params = state.get("symbol_strategy_params", {})
        state["seen_bias"] = params.get("bias", "none")
        return SignalScore(symbol=symbol, score=95, action="OPEN_LONG", reason=f"bias {symbol}")


def test_trade_intent_normalizes_and_validates_action():
    intent = TradeIntent(
        symbol="btcusdt",
        action="open_long",
        confidence=120,
        reason=" setup ",
        strategy_name="Strategy",
    )

    assert intent.symbol == "BTCUSDT"
    assert intent.action == "OPEN_LONG"
    assert intent.confidence == 100
    assert intent.reason == "setup"

    with pytest.raises(ValueError, match="action nao suportada"):
        TradeIntent("BTCUSDT", "BUY_NOW", 50, "bad", "Strategy")


def test_strategy_coordinator_uses_one_global_strategy_with_isolated_symbol_state(
    config_factory,
    monkeypatch,
):
    import governor.strategy_coordinator as module

    monkeypatch.setattr(module, "load_strategy", lambda *_args, **_kwargs: GlobalIntentStrategy())
    config = config_factory(symbols=("APTUSDT", "SEIUSDT"))
    profiles = [
        symbol_config_from_app_config(config, symbol="APTUSDT"),
        symbol_config_from_app_config(config, symbol="SEIUSDT"),
    ]
    coordinator = StrategyCoordinator(config=config, symbol_configs=profiles)

    first = coordinator.intent(profiles[0], _market("APTUSDT"), _flat("APTUSDT"))
    second = coordinator.intent(profiles[1], _market("SEIUSDT"), _flat("SEIUSDT"))
    third = coordinator.intent(profiles[0], _market("APTUSDT"), _flat("APTUSDT"))

    assert coordinator.global_strategy_name == f"{config.strategy_module}.{config.strategy_class}"
    assert first.metadata["calls"] == 1
    assert second.metadata["calls"] == 1
    assert third.metadata["calls"] == 2
    assert coordinator.state_for_symbol("APTUSDT").data["intent_calls"] == 2
    assert coordinator.state_for_symbol("SEIUSDT").data["intent_calls"] == 1


def test_symbol_strategy_params_are_state_context_with_global_strategy(
    config_factory,
    monkeypatch,
):
    import governor.strategy_coordinator as module

    monkeypatch.setattr(
        module,
        "load_strategy",
        lambda *_args, **_kwargs: ParamAwareGlobalStrategy(),
    )
    config = config_factory(symbols=("APTUSDT", "SEIUSDT"))
    profiles = [
        replace(
            symbol_config_from_app_config(config, symbol="APTUSDT"),
            strategy_params={"bias": "fast"},
        ),
        replace(
            symbol_config_from_app_config(config, symbol="SEIUSDT"),
            strategy_params={"bias": "slow"},
        ),
    ]
    coordinator = StrategyCoordinator(config=config, symbol_configs=profiles)

    coordinator.score(profiles[0], _market("APTUSDT"), _flat("APTUSDT"))
    coordinator.score(profiles[1], _market("SEIUSDT"), _flat("SEIUSDT"))

    assert coordinator.global_strategy_name == f"{config.strategy_module}.{config.strategy_class}"
    assert coordinator.profile_strategy_warnings
    assert coordinator.state_for_symbol("APTUSDT").data["seen_bias"] == "fast"
    assert coordinator.state_for_symbol("SEIUSDT").data["seen_bias"] == "slow"


def test_trade_plan_builder_creates_shadow_plan_without_execution(config_factory, symbol_rules):
    context = _context(config_factory(), symbol_rules)
    intent = TradeIntent(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        confidence=95,
        reason="ready",
        strategy_name="tests.Strategy",
    )

    plan = TradePlanBuilder(cost_model=CostModel()).build(intent=intent, context=context)

    assert plan.status in {
        TradePlanStatus.ALLOW_NORMAL.value,
        TradePlanStatus.ALLOW_AGGRESSIVE.value,
    }
    assert plan.quantity > 0
    assert plan.notional <= Decimal(str(context.symbol_config.position_notional_usdt))
    assert plan.stop_price is not None
    assert plan.take_profit_price is not None
    assert plan.estimated_round_trip_cost > 0
    assert plan.plan_id.startswith("tp_")
    assert plan.plan_version == TRADE_PLAN_VERSION
    assert plan.source == TRADE_PLAN_SOURCE_SHADOW
    assert plan.created_at
    assert plan.diagnostics["shadow_mode_safe"] is True
    assert plan.diagnostics["execution_unchanged"] is True
    assert plan.diagnostics["plan_version"] == TRADE_PLAN_VERSION
    assert plan.diagnostics["plan_source"] == TRADE_PLAN_SOURCE_SHADOW
    assert plan.diagnostics["exit_recalculated_after_sizing"] is True
    assert plan.diagnostics["final_quantity"] == str(plan.quantity)
    assert plan.diagnostics["cost_summary"]["cost_model_version"] == COST_MODEL_VERSION
    assert plan.diagnostics["cost_summary"]["notional"] == str(plan.notional)
    assert plan.diagnostics["cost_summary"]["round_trip_cost"] == str(
        plan.estimated_round_trip_cost
    )
    assert plan.diagnostics["cost"]["cost_model_version"] == COST_MODEL_VERSION
    assert plan.diagnostics["cost"]["components"]["entry_fee"] == str(plan.estimated_entry_fee)
    assert plan.diagnostics["cost"]["components"]["exit_fee"] == str(plan.estimated_exit_fee)
    assert "round_trip_cost_bps" in plan.diagnostics["cost"]


def test_trade_plan_builder_generates_unique_plan_ids(config_factory, symbol_rules):
    context = _context(config_factory(), symbol_rules)
    intent = TradeIntent(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        confidence=95,
        reason="ready",
        strategy_name="tests.Strategy",
    )

    builder = TradePlanBuilder(cost_model=CostModel())
    first = builder.build(intent=intent, context=context)
    second = builder.build(intent=intent, context=context)

    assert first.plan_id != second.plan_id
    assert first.plan_version == second.plan_version == TRADE_PLAN_VERSION
    assert first.source == second.source == TRADE_PLAN_SOURCE_SHADOW


def test_trade_plan_builder_recalculates_exit_metrics_after_final_sizing(
    config_factory,
    symbol_rules,
):
    context = _context(config_factory(), symbol_rules)
    intent = TradeIntent(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        confidence=65,
        reason="reduced but valid",
        strategy_name="tests.Strategy",
    )

    plan = TradePlanBuilder(cost_model=CostModel()).build(
        intent=intent,
        context=context,
    )

    expected_net_gain = (
        abs(plan.take_profit_price - plan.entry_reference_price) * plan.quantity
        - plan.estimated_round_trip_cost
    )
    expected_net_loss = (
        abs(plan.entry_reference_price - plan.stop_price) * plan.quantity
        + plan.estimated_round_trip_cost
    )

    assert plan.status == TradePlanStatus.ALLOW_REDUCED.value
    assert plan.size_multiplier == Decimal("0.70")
    assert plan.expected_net_gain == expected_net_gain
    assert plan.expected_net_loss == expected_net_loss
    assert plan.reward_risk == expected_net_gain / expected_net_loss
    assert plan.diagnostics["initial_quantity"] != plan.diagnostics["final_quantity"]
    assert plan.diagnostics["initial_plan"]["expected_net_gain"] != str(plan.expected_net_gain)


def test_trade_plan_builder_from_config_injects_economic_exit_planner(
    config_factory,
):
    config = config_factory(max_economic_take_bps=80.0)
    builder = TradePlanBuilder.from_config(config)

    assert builder.exit_planner.max_economic_take_bps == Decimal("80.0")


def test_trade_plan_builder_adds_economic_exit_diagnostics(
    config_factory,
    symbol_rules,
):
    context = _context(config_factory(), symbol_rules)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 95, "ready", "tests.Strategy")

    plan = TradePlanBuilder().build(intent=intent, context=context)

    assert plan.diagnostics["exit"]["model"] == "economic_aware"
    assert "required_take_bps" in plan.diagnostics["exit"]
    assert "round_trip_cost_bps" in plan.diagnostics["exit"]


def test_trade_plan_builder_adjusted_take_passes_economic_gate(
    config_factory,
    symbol_rules,
):
    config = replace(config_factory(), stop_loss_pct=0.003, take_profit_pct=0.004)
    context = _context(config, symbol_rules)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 95, "ready", "tests.Strategy")

    plan = TradePlanBuilder.from_config(config).build(intent=intent, context=context)
    decision = EconomicTradeGate.from_config(config).evaluate(plan, "OPEN_LONG")

    assert plan.diagnostics["exit"]["economic_exit_adjusted"] is True
    assert plan.take_profit_price > Decimal("100.40")
    assert decision.allowed is True


def test_trade_plan_builder_blocks_when_required_take_is_unrealistic(
    config_factory,
    symbol_rules,
):
    config = replace(
        config_factory(max_economic_take_bps=20.0),
        stop_loss_pct=0.003,
        take_profit_pct=0.004,
    )
    context = _context(config, symbol_rules)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 95, "ready", "tests.Strategy")

    plan = TradePlanBuilder.from_config(config).build(intent=intent, context=context)
    decision = EconomicTradeGate.from_config(config).evaluate(plan, "OPEN_LONG")

    assert plan.status == TradePlanStatus.BLOCK_ECONOMIC.value
    assert plan.block_reason == "ECONOMIC_TAKE_REQUIRED_TOO_FAR"
    assert plan.diagnostics["exit"]["economic_exit_blocked_unrealistic_take"] is True
    assert decision.allowed is False
    assert decision.reason == "ECONOMIC_TAKE_REQUIRED_TOO_FAR"


def test_quality_scorer_treats_non_positive_gain_as_economic_penalty(
    config_factory,
    symbol_rules,
):
    context = _context(config_factory(), symbol_rules)
    intent = TradeIntent(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        confidence=95,
        reason="strong signal despite high cost",
        strategy_name="tests.Strategy",
    )

    plan = TradePlanBuilder(
        cost_model=CostModel(
            taker_fee_pct=Decimal("0.05"),
            maker_fee_pct=Decimal("0"),
            estimated_spread_bps=Decimal("0"),
            estimated_slippage_bps=Decimal("0"),
            estimated_funding_rate=Decimal("0"),
        ),
        exit_planner=ExitPlanner(economic_exit_adjustment_enabled=False),
    ).build(intent=intent, context=context)

    assert plan.status == TradePlanStatus.ALLOW_REDUCED.value
    assert plan.block_reason is None
    assert plan.quantity > 0
    assert "expected_net_gain_not_positive" in plan.diagnostics["penalties"]
    assert "expected_net_gain_not_positive" in plan.diagnostics["quality"]["economic_penalties"]
    assert plan.diagnostics["quality"]["hard_blocks"] == {}
    assert plan.diagnostics["quality"]["economic_penalties_only_reduce_score"] is True


def test_quality_scorer_blocks_economic_when_score_falls_below_minimum(
    config_factory,
    symbol_rules,
):
    context = _context(config_factory(), symbol_rules)
    intent = TradeIntent(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        confidence=60,
        reason="weak signal with high cost",
        strategy_name="tests.Strategy",
    )

    plan = TradePlanBuilder(
        cost_model=CostModel(
            taker_fee_pct=Decimal("0.05"),
            maker_fee_pct=Decimal("0"),
            estimated_spread_bps=Decimal("0"),
            estimated_slippage_bps=Decimal("0"),
            estimated_funding_rate=Decimal("0"),
        ),
        exit_planner=ExitPlanner(economic_exit_adjustment_enabled=False),
    ).build(intent=intent, context=context)

    assert plan.status == TradePlanStatus.BLOCK_ECONOMIC.value
    assert plan.block_reason == "QUALITY_SCORE_BELOW_MINIMUM"
    assert plan.quantity == Decimal("0")
    assert plan.diagnostics["quality"]["block_category"] == "ECONOMIC"
    assert "expected_net_gain_not_positive" in plan.diagnostics["quality"]["economic_penalties"]
    assert plan.diagnostics["quality"]["hard_blocks"] == {}


def test_trade_plan_comparison_records_profile_reference_after_sizing(
    config_factory,
    symbol_rules,
):
    context = _context(config_factory(), symbol_rules)
    intent = TradeIntent(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        confidence=60,
        reason="reduced but valid",
        strategy_name="tests.Strategy",
    )

    plan = TradePlanBuilder(cost_model=CostModel()).build(
        intent=intent,
        context=context,
    )
    comparison = compare_plan_to_profile_reference(plan=plan, context=context)

    assert comparison.plan_id == plan.plan_id
    assert comparison.reference_flow == PROFILE_REFERENCE_FLOW
    assert comparison.reference_quantity > comparison.planned_quantity
    assert comparison.planned_quantity == plan.quantity
    assert comparison.planned_notional == plan.notional
    assert comparison.quantity_changed is True
    assert comparison.notional_changed is True
    assert comparison.exit_changed is False
    assert comparison.diagnostics["shadow_only"] is True
    assert comparison.diagnostics["execution_unchanged"] is True
    assert comparison.diagnostics["exit_comparable"] is True


def test_quality_scorer_hard_blocks_stale_data(config_factory, symbol_rules):
    context = replace(_context(config_factory(), symbol_rules), stale=True)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 95, "ready", "tests.Strategy")

    plan = TradePlanBuilder().build(intent=intent, context=context)

    assert plan.status == TradePlanStatus.BLOCK_SAFETY.value
    assert plan.block_reason == "STALE_DATA"
    assert plan.diagnostics["quality"]["block_category"] == "SAFETY"
    assert plan.diagnostics["quality"]["hard_blocks"] == {"STALE_DATA": True}
    assert plan.diagnostics["quality"]["economic_penalties"] == {}


def test_position_sizer_respects_min_notional(config_factory, symbol_rules):
    config = config_factory()
    profile = replace(
        symbol_config_from_app_config(config),
        position_notional_usdt=1.0,
    )
    context = replace(_context(config, symbol_rules), symbol_config=profile)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 95, "ready", "tests.Strategy")

    plan = TradePlanBuilder().build(intent=intent, context=context)

    assert plan.status == TradePlanStatus.BLOCK_ECONOMIC.value
    assert plan.block_reason in {
        "PROFILE_NOTIONAL_BELOW_MIN_NOTIONAL",
        "EXPECTED_NET_GAIN_NOT_POSITIVE",
    }


def test_cost_model_is_strategy_independent(config_factory, symbol_rules):
    context = _context(config_factory(), symbol_rules)
    model = CostModel(taker_fee_pct=Decimal("0.001"), estimated_spread_bps=Decimal("1"))

    first = model.estimate(context=context, quantity=Decimal("0.1"))
    second = model.estimate(context=context, quantity=Decimal("0.1"))

    assert first == second
    assert first.round_trip_cost > 0
    assert first.notional == Decimal("10.0")
    assert first.cost_model_version == COST_MODEL_VERSION
    assert first.round_trip_cost_bps > 0
    assert first.diagnostics["cost_model_version"] == COST_MODEL_VERSION
    assert first.diagnostics["components"]["entry_fee"] == str(first.entry_fee)
    assert first.diagnostics["components"]["exit_fee"] == str(first.exit_fee)
    assert first.diagnostics["components"]["spread_cost"] == str(first.spread_cost)
    assert first.diagnostics["component_bps"]["entry_fee_bps"] == "10.000"
    assert first.diagnostics["sources"]["spread"] == "estimated_config"


def test_cost_model_diagnostics_identify_market_context_spread(
    config_factory,
    symbol_rules,
):
    context = replace(
        _context(config_factory(), symbol_rules),
        spread_pct=Decimal("0.0003"),
    )

    estimate = CostModel(estimated_spread_bps=Decimal("1")).estimate(
        context=context,
        quantity=Decimal("0.1"),
    )

    assert estimate.diagnostics["rates"]["spread_bps"] == "3.0000"
    assert estimate.diagnostics["sources"]["spread"] == "market_context"


def test_cost_model_reads_gui_cost_env_aliases(monkeypatch, config_factory, symbol_rules):
    monkeypatch.delenv("ALPHORA_ESTIMATED_SPREAD_BPS", raising=False)
    monkeypatch.delenv("ALPHORA_ESTIMATED_SLIPPAGE_BPS", raising=False)
    monkeypatch.delenv("ALPHORA_ESTIMATED_FUNDING_RATE", raising=False)
    monkeypatch.setenv("ALPHORA_SPREAD_BPS", "4")
    monkeypatch.setenv("ALPHORA_SLIPPAGE_BPS", "6")
    monkeypatch.setenv("ALPHORA_DEFAULT_FUNDING_RATE", "-0.0002")

    model = CostModel.from_env()

    assert model.estimated_spread_bps == Decimal("4")
    assert model.estimated_slippage_bps == Decimal("6")
    assert model.estimated_funding_rate == Decimal("-0.0002")


def test_cost_model_can_be_created_from_app_config(config_factory):
    config = config_factory()

    model = CostModel.from_config(config)

    assert model.maker_fee_pct == Decimal("0.0002")
    assert model.taker_fee_pct == Decimal("0.0005")
    assert model.estimated_spread_bps == Decimal("1.0")
    assert model.estimated_slippage_bps == Decimal("2.0")
    assert model.estimated_funding_rate == Decimal("0.0")


def test_portfolio_allocator_ranks_by_quality_score():
    low = _plan("BTCUSDT", quality_score=60)
    high = _plan("ETHUSDT", quality_score=90)
    blocked = _plan("SOLUSDT", quality_score=99, status=TradePlanStatus.BLOCK_SAFETY.value)

    allocation = PortfolioAllocator().allocate([low, high, blocked], max_selected=1)

    assert allocation.selected_plans == [high]
    assert blocked in allocation.rejected_plans
    assert low in allocation.rejected_plans


def _context(config, symbol_rules: SymbolRules) -> TradeContext:
    profile = symbol_config_from_app_config(config)
    return TradeContext(
        symbol="BTCUSDT",
        market_price=Decimal("100"),
        mark_price=Decimal("100"),
        index_price=None,
        spread_pct=None,
        volatility=None,
        atr=None,
        funding_rate=None,
        next_funding_time=None,
        position=_flat("BTCUSDT"),
        exchange_rules=symbol_rules,
        symbol_config=profile,
        portfolio_snapshot={"available_balance_usdt": "1000"},
        data_age_seconds=0.0,
        stale=False,
    )


def _market(symbol: str) -> MarketSnapshot:
    return MarketSnapshot(symbol, Decimal("100"), [Decimal("99"), Decimal("100")])


def _flat(symbol: str) -> FuturesPosition:
    return FuturesPosition(
        symbol,
        PositionDirection.NONE,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )


def _plan(
    symbol: str,
    *,
    quality_score: int,
    status: str = TradePlanStatus.ALLOW_NORMAL.value,
) -> TradePlan:
    return TradePlan(
        symbol=symbol,
        action="OPEN_LONG",
        status=status,
        strategy_name="tests.Strategy",
        signal_confidence=quality_score,
        quality_score=quality_score,
        entry_reference_price=Decimal("100"),
        quantity=Decimal("0.1"),
        notional=Decimal("10"),
        stop_price=Decimal("99"),
        take_profit_price=Decimal("102"),
        estimated_entry_fee=Decimal("0.01"),
        estimated_exit_fee=Decimal("0.01"),
        estimated_spread_cost=Decimal("0.01"),
        estimated_slippage_cost=Decimal("0.01"),
        estimated_funding_cost=Decimal("0"),
        estimated_round_trip_cost=Decimal("0.04"),
        expected_net_gain=Decimal("1"),
        expected_net_loss=Decimal("1"),
        reward_risk=Decimal("1"),
        size_multiplier=Decimal("1"),
        execution_style="MARKET",
        management_style="PROTECTION_POLICY_ONLY",
        reason="test",
        block_reason=None,
    )


def test_trade_plan_records_outcome_costs_and_breakeven_probability(
    config_factory,
    symbol_rules,
):
    config = config_factory()
    context = _context(config, symbol_rules)
    intent = TradeIntent("BTCUSDT", "OPEN_LONG", 95, "ready", "tests.Strategy")

    plan = TradePlanBuilder.from_config(config).build(intent=intent, context=context)
    decision = EconomicTradeGate.from_config(config).evaluate(plan, "OPEN_LONG")

    outcome = plan.diagnostics["outcome_costs"]
    r_win, r_loss = Decimal(outcome["r_win"]), Decimal(outcome["r_loss"])
    breakeven = Decimal(outcome["breakeven_probability"])
    assert outcome["direction"] == "LONG"
    assert outcome["take_exit_maker"] is True
    assert Decimal(outcome["cost_stop_bps"]) > Decimal(outcome["cost_take_bps"])
    assert breakeven == pytest.approx(r_loss / (r_win + r_loss), abs=Decimal("0.0001"))
    # Shadow only: the gate shows p* but decides as before.
    assert decision.diagnostics["outcome_costs"] == outcome


def test_live_trade_context_carries_current_funding_rate_into_outcome_costs(
    config_factory,
):
    from governor.market_data_router import MarketDataRouter
    from governor.trade_pipeline import TradePipeline
    from tests.fakes import FakeFuturesClient

    class PremiumIndexClient(FakeFuturesClient):
        def mark_price(self, symbol, *, timeout=None):
            return {
                "symbol": symbol,
                "markPrice": "100",
                "indexPrice": "99.95",
                "lastFundingRate": "0.0005",
                "nextFundingTime": 1700000000000,
            }

    config = config_factory()
    profile = symbol_config_from_app_config(config)
    pipeline = TradePipeline(config=config)

    for snapshot_enabled in (True, False):
        router = MarketDataRouter(
            PremiumIndexClient(),
            snapshot_enabled=snapshot_enabled,
        )
        heavy = router.get_heavy_snapshot("BTCUSDT", now=1000.0)
        assert heavy.funding_rate == Decimal("0.0005")
        assert heavy.next_funding_time == 1700000000000
        assert heavy.index_price == Decimal("99.95")

        long_result = pipeline.build_plan(
            intent=TradeIntent("BTCUSDT", "OPEN_LONG", 95, "ready", "tests.Strategy"),
            symbol_config=profile,
            heavy_snapshot=heavy,
            portfolio_snapshot={"available_balance_usdt": "1000"},
        )
        short_result = pipeline.build_plan(
            intent=TradeIntent("BTCUSDT", "OPEN_SHORT", 95, "ready", "tests.Strategy"),
            symbol_config=profile,
            heavy_snapshot=heavy,
            portfolio_snapshot={"available_balance_usdt": "1000"},
        )
        assert long_result.context.funding_rate == Decimal("0.0005")
        assert long_result.context.next_funding_time == 1700000000000
        assert long_result.context.index_price == Decimal("99.95")
        assert long_result.plan.diagnostics["cost"]["sources"]["funding"] == "market_context"
        long_outcome = long_result.plan.diagnostics["outcome_costs"]
        short_outcome = short_result.plan.diagnostics["outcome_costs"]
        # Positive funding costs the long (+5 bps) and pays the short (-5 bps).
        assert Decimal(long_outcome["cost_take_bps"]) > Decimal(short_outcome["cost_take_bps"])
        assert Decimal(long_outcome["breakeven_probability"]) > Decimal(
            short_outcome["breakeven_probability"]
        )
