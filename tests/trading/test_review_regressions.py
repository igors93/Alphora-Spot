"""Regression tests for defects found during the trading-logic review."""

import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from binance.futures_client import BinanceFuturesAPIError, BinanceFuturesClient
from config import ExecutionMode
from config.symbol_context import symbol_config_from_app_config
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeAction
from domain.symbol_rules import SymbolRules
from execution.simulated_broker import SimulatedBroker
from futures.exit_models import ATRExitModel
from futures.performance.cost_model import CostModel
from futures.planning.position_sizer import PositionSizer
from futures.planning.quality_scorer import QualityDecision
from futures.risk_manager import FuturesRiskManager
from governor.daily_portfolio_metrics import DailyPortfolioMetricsReader
from governor.execution_engine import ExecutionEngine
from indicators.atr import PriceCandle
from strategies.futures.ai.probabilistic_strategy import ProbabilisticAIStrategy


def _rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_tick=Decimal("0.10"),
    )


# ---------------------------------------------------------------------------
# ATR exit model never emits unusable protection prices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", [TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT])
def test_atr_exit_falls_back_when_atr_is_zero(action):
    flat = [PriceCandle(Decimal("100"), Decimal("100"), Decimal("100")) for _ in range(20)]
    model = ATRExitModel(period=14, stop_multiplier=1.5, take_multiplier=2.0)

    prices = model.calculate(
        action=action,
        entry_price=Decimal("100"),
        rules=_rules(),
        stop_loss_pct=Decimal("0.01"),
        take_profit_pct=Decimal("0.02"),
        candles=flat,
    )

    assert prices.fallback_used is True
    if action is TradeAction.OPEN_LONG:
        assert prices.stop_price < Decimal("100") < prices.take_profit_price
    else:
        assert prices.take_profit_price < Decimal("100") < prices.stop_price


def test_atr_exit_falls_back_when_short_take_would_be_negative():
    wild = [
        PriceCandle(Decimal("200"), Decimal("1"), Decimal("100")),
        *[PriceCandle(Decimal("200"), Decimal("1"), Decimal("100")) for _ in range(19)],
    ]
    model = ATRExitModel(period=14, stop_multiplier=1.5, take_multiplier=3.0)

    prices = model.calculate(
        action=TradeAction.OPEN_SHORT,
        entry_price=Decimal("100"),
        rules=_rules(),
        stop_loss_pct=Decimal("0.01"),
        take_profit_pct=Decimal("0.02"),
        candles=wild,
    )

    assert prices.fallback_used is True
    assert prices.take_profit_price > 0


# ---------------------------------------------------------------------------
# Position sizer reaches the exchange minimum instead of rounding below it
# ---------------------------------------------------------------------------


def test_position_sizer_reduced_size_rounds_up_to_executable_minimum():
    context = SimpleNamespace(
        position=None,
        market_price=Decimal("333"),
        symbol_config=SimpleNamespace(position_notional_usdt=50.0),
        exchange_rules=SymbolRules(
            symbol="BTCUSDT",
            quantity_step=Decimal("0.01"),
            min_quantity=Decimal("0.01"),
            min_notional=Decimal("20"),
            price_tick=Decimal("0.10"),
        ),
    )
    quality = QualityDecision(
        status="ALLOW_REDUCED",
        quality_score=60,
        size_multiplier=Decimal("0.1"),
        block_reason=None,
    )

    sized = PositionSizer().size(
        intent=SimpleNamespace(action="OPEN_LONG"),
        context=context,
        quality=quality,
    )

    assert sized.block_reason is None
    assert sized.notional >= Decimal("20")
    assert sized.notional <= Decimal("50")


def test_position_sizer_blocks_without_market_price():
    context = SimpleNamespace(
        position=None,
        market_price=Decimal("0"),
        symbol_config=SimpleNamespace(position_notional_usdt=50.0),
        exchange_rules=_rules(),
    )
    quality = QualityDecision(
        status="ALLOW_NORMAL",
        quality_score=80,
        size_multiplier=Decimal("1"),
        block_reason=None,
    )

    sized = PositionSizer().size(
        intent=SimpleNamespace(action="OPEN_LONG"),
        context=context,
        quality=quality,
    )

    assert sized.block_reason == "MARKET_PRICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# PAPER_LIVE: realistic holding time, simulated stops and configured costs
# ---------------------------------------------------------------------------


def _paper_broker() -> SimulatedBroker:
    return SimulatedBroker(
        symbol="BTCUSDT",
        risk_manager=FuturesRiskManager(
            position_notional_usdt=50,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
        ),
        symbol_rules=_rules(),
        cost_model=CostModel(),
    )


def test_paper_position_reports_real_opened_at():
    broker = _paper_broker()
    opened = time.time()
    broker.open_position(
        action=TradeAction.OPEN_LONG,
        timestamp=str(opened),
        price=Decimal("100"),
        reason="test",
    )

    position = broker.current_position()

    assert position.opened_at == pytest.approx(opened, abs=1)
    # Previously opened_at=0 made any max-hold guard fire immediately.
    assert position.holding_seconds is not None
    assert position.holding_seconds < 60


class _PaperClient:
    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": "100"}


def _paper_execution(config_factory):
    config = config_factory(execution_mode=ExecutionMode.PAPER_LIVE)
    profile = symbol_config_from_app_config(config)
    execution = ExecutionEngine(base_config=config, client=_PaperClient())
    engine = execution._get_engine(profile)
    execution._ensure_symbol_rules(engine, profile)
    result = execution._paper_open_position(
        engine,
        action=TradeAction.OPEN_LONG,
        price=Decimal("100"),
        reason="test",
    )
    assert result.position_changed
    return execution, engine, profile


def _paper_closed_events(engine) -> list[dict]:
    with open(engine.paper_journal.path, encoding="utf-8") as file:
        events = [json.loads(line) for line in file]
    return [event for event in events if event["event_type"] == "paper_position_closed"]


def test_paper_stop_loss_is_triggered(config_factory):
    execution, engine, profile = _paper_execution(config_factory)
    stop_price = engine.paper_broker.position.stop_price

    with patch.object(
        engine,
        "get_trigger_reference_price",
        return_value=stop_price - Decimal("0.5"),
    ):
        still_open = execution.audit_symbol_protections(profile)

    assert still_open is False
    closed = _paper_closed_events(engine)
    assert closed and closed[-1]["data"]["trade"]["exit_reason"] == "paper_stop_loss"
    # A stop never fills better than its trigger.
    assert Decimal(closed[-1]["data"]["trade"]["exit_price"]) <= stop_price


def test_paper_take_profit_is_triggered(config_factory):
    execution, engine, profile = _paper_execution(config_factory)
    take_price = engine.paper_broker.position.take_profit_price

    with patch.object(
        engine,
        "get_trigger_reference_price",
        return_value=take_price + Decimal("3"),
    ):
        still_open = execution.audit_symbol_protections(profile)

    assert still_open is False
    closed = _paper_closed_events(engine)
    assert closed[-1]["data"]["trade"]["exit_reason"] == "paper_take_profit"
    assert Decimal(closed[-1]["data"]["trade"]["exit_price"]) == take_price


def test_paper_position_inside_protections_stays_open(config_factory):
    execution, engine, profile = _paper_execution(config_factory)

    with patch.object(engine, "get_trigger_reference_price", return_value=Decimal("100")):
        assert execution.audit_symbol_protections(profile) is True

    with patch.object(
        engine,
        "get_trigger_reference_price",
        side_effect=BinanceFuturesAPIError(500, -1000, "boom"),
    ):
        # A failed price read skips the check; it never faults the symbol.
        assert execution.audit_symbol_protections(profile) is True


def test_paper_broker_uses_configured_costs(config_factory):
    _, engine, _ = _paper_execution(config_factory)
    config = engine.config

    cost_model = engine.paper_broker.cost_model

    assert cost_model.taker_fee_pct == Decimal(str(config.taker_fee_pct))
    assert cost_model.maker_fee_pct == Decimal(str(config.maker_fee_pct))
    assert cost_model.spread_bps == Decimal(str(config.estimated_spread_bps))
    assert cost_model.slippage_bps == Decimal(str(config.estimated_slippage_bps))


# ---------------------------------------------------------------------------
# AI strategy never acts on placeholder probabilities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "AI_MODEL_TIMEFRAME_MISMATCH",
        "AI_MODEL_INPUT_CONTRACT_MISSING",
        "AI_REFINED_GLOBAL_TIMEFRAME_MISMATCH",
    ],
)
def test_ai_strategy_does_not_close_positions_without_a_usable_model(reason):
    strategy = ProbabilisticAIStrategy()
    long_position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.01"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )
    prediction = {
        "reason": reason,
        "model_available": False,
        "p_long_event": 0.0,
        "p_short_event": 0.0,
    }

    market = MarketSnapshot(symbol="BTCUSDT", price=Decimal("100"), prices=[Decimal("100")])

    assert strategy._action_for_prediction(prediction, long_position, {}, market) == "NO_ACTION"


# ---------------------------------------------------------------------------
# Daily portfolio metrics fail closed on unreadable PnL
# ---------------------------------------------------------------------------


def test_daily_metrics_count_unreadable_pnl_as_unknown(config_factory, tmp_path):
    from datetime import datetime, timezone

    config = config_factory()
    profile = symbol_config_from_app_config(config)
    journal = tmp_path / "data" / "trades.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": "position_closed",
                "data": {"net_pnl": "not-a-number"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    events = SimpleNamespace(append=lambda *args, **kwargs: None)
    reader = DailyPortfolioMetricsReader(
        config=config,
        symbol_configs=[profile],
        events_journal=events,
    )

    metrics = reader.read()

    assert metrics["pnl_unknown_closes"] == 1


# ---------------------------------------------------------------------------
# Binance client
# ---------------------------------------------------------------------------


def test_binance_client_default_recv_window_is_tight():
    client = BinanceFuturesClient(
        base_url="https://demo-fapi.binance.com",
        api_key="key",
        api_secret="secret",
    )
    try:
        assert client.recv_window <= 10_000
    finally:
        client.close()


def test_binance_error_with_non_object_body_raises_api_error():
    response = SimpleNamespace(status_code=500, text="[1, 2]", json=lambda: [1, 2])

    with pytest.raises(BinanceFuturesAPIError) as raised:
        BinanceFuturesClient._handle_response(response)

    assert raised.value.status_code == 500
