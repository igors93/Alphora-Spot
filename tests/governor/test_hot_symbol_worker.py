from decimal import Decimal

from config.symbol_context import symbol_config_from_app_config
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection
from domain.symbol_rules import SymbolRules
from governor.hot_symbol_worker import GuardOutcome, HotSymbolWorker
from governor.market_data_router import HeavyMarketSnapshot
from scanner.signal_score import SignalScore
from trading.intent import TradeIntent


def _heavy_snapshot(*, stale: bool = False, open_orders=None, position=None):
    return HeavyMarketSnapshot(
        symbol="BTCUSDT",
        market=MarketSnapshot(
            symbol="BTCUSDT",
            price=Decimal("100"),
            prices=[Decimal("99"), Decimal("100")],
        ),
        mark_price=Decimal("100"),
        exchange_rules=SymbolRules(
            symbol="BTCUSDT",
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
            price_tick=Decimal("0.10"),
        ),
        position=position
        or FuturesPosition(
            symbol="BTCUSDT",
            direction=PositionDirection.NONE,
            quantity=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        ),
        open_orders=open_orders or [],
        open_algo_orders=[],
        account_info={},
        timestamp=1.0,
        data_age_seconds=99 if stale else 0,
        stale=stale,
    )


def _intent(profile, score, *, action="OPEN_LONG", reason="confirmado") -> TradeIntent:
    return TradeIntent(
        symbol=profile.symbol,
        action=action,
        confidence=score.score,
        score=score.score,
        priority=score.priority,
        reason=reason,
        strategy_name="tests.HotSymbolStrategy",
        market_price="100",
        suggested_next_check_seconds=score.suggested_next_check_seconds,
        metadata={"source": "test"},
    )


def test_hot_symbol_worker_blocks_stale_data(config_factory):
    profile = symbol_config_from_app_config(config_factory())
    score = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")

    result = HotSymbolWorker().evaluate(
        symbol_config=profile,
        score=score,
        intent=_intent(profile, score),
        heavy_snapshot=_heavy_snapshot(stale=True),
    )

    assert result.outcome is GuardOutcome.BLOCK
    assert result.reason == "STALE_DATA"


def test_hot_symbol_worker_blocks_open_order_conflict(config_factory):
    profile = symbol_config_from_app_config(config_factory())
    score = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")

    result = HotSymbolWorker().evaluate(
        symbol_config=profile,
        score=score,
        intent=_intent(profile, score),
        heavy_snapshot=_heavy_snapshot(open_orders=[{"orderId": 1}]),
    )

    assert result.outcome is GuardOutcome.BLOCK
    assert result.reason == "OPEN_ORDER_CONFLICT"
    assert result.checks["open_orders_count"] == 1
    assert result.checks["open_algo_orders_count"] == 0
    assert result.checks["open_order_conflict_policy"] == "BLOCK_ANY_OPEN_ORDER"


def test_hot_symbol_worker_allows_confirmed_open(config_factory):
    profile = symbol_config_from_app_config(config_factory())
    score = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")

    result = HotSymbolWorker().evaluate(
        symbol_config=profile,
        score=score,
        intent=_intent(profile, score),
        heavy_snapshot=_heavy_snapshot(),
    )

    assert result.outcome is GuardOutcome.ALLOW


def test_hot_symbol_worker_does_not_require_ready_to_close(config_factory):
    profile = symbol_config_from_app_config(config_factory())
    score = SignalScore(symbol="BTCUSDT", score=10, reason="cold")
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        opened_at=1.0,
    )

    result = HotSymbolWorker().evaluate(
        symbol_config=profile,
        score=score,
        intent=_intent(profile, score, action="CLOSE_POSITION", reason="close confirmed"),
        heavy_snapshot=_heavy_snapshot(position=position),
    )

    assert result.outcome is GuardOutcome.ALLOW


def test_hot_symbol_worker_requires_strategy_confirmation_for_open(config_factory):
    profile = symbol_config_from_app_config(config_factory())
    score = SignalScore(symbol="BTCUSDT", score=95, action="OPEN_LONG", reason="ready")

    result = HotSymbolWorker().evaluate(
        symbol_config=profile,
        score=score,
        intent=_intent(profile, score, action="HOLD", reason="hold"),
        heavy_snapshot=_heavy_snapshot(),
    )

    assert result.outcome is GuardOutcome.DEFER
    assert result.reason == "STRATEGY_HOLD"
