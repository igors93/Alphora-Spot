from decimal import Decimal

from domain.models import FuturesPosition, PositionDirection
from domain.symbol_rules import SymbolRules
from futures.costs.model import CostEstimate
from futures.planning.exit_planner import ExitPlan
from futures.planning.quality_scorer import QualityScorer
from trading.context import TradeContext
from trading.enums import TradePlanStatus
from trading.intent import TradeIntent


def _context(*, funding_rate=None, volatility=None, stale=False, position_open=False):
    direction = PositionDirection.LONG if position_open else PositionDirection.NONE
    qty = Decimal("0.5") if position_open else Decimal("0")
    entry = Decimal("100") if position_open else Decimal("0")
    return TradeContext(
        symbol="BTCUSDT",
        market_price=Decimal("100"),
        mark_price=Decimal("100"),
        index_price=None,
        spread_pct=None,
        volatility=volatility,
        atr=None,
        funding_rate=funding_rate,
        next_funding_time=None,
        position=FuturesPosition("BTCUSDT", direction, qty, entry, Decimal("0")),
        exchange_rules=SymbolRules(
            "BTCUSDT", Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.10")
        ),
        symbol_config=None,
        portfolio_snapshot={},
        data_age_seconds=0.0,
        stale=stale,
    )


def _exit_plan(*, stop=Decimal("99"), take=Decimal("102"), net_gain=Decimal("1.9")):
    return ExitPlan(
        stop_price=stop,
        take_profit_price=take,
        expected_net_gain=net_gain,
        expected_net_loss=Decimal("1.1"),
        reward_risk=Decimal("1.7"),
    )


def _cost(*, round_trip=Decimal("0.10")):
    return CostEstimate(
        entry_fee=round_trip / 4,
        exit_fee=round_trip / 4,
        spread_cost=round_trip / 4,
        slippage_cost=round_trip / 4,
        funding_cost=Decimal("0"),
        round_trip_cost=round_trip,
        minimum_profitable_move=round_trip,
        notional=Decimal("100"),
        round_trip_cost_bps=round_trip * Decimal("100"),
    )


def _intent(action="OPEN_LONG", confidence=95):
    return TradeIntent(
        symbol="BTCUSDT",
        action=action,
        confidence=confidence,
        reason="ready",
        strategy_name="tests.Strategy",
    )


def test_quality_scorer_does_not_penalize_open_long_with_negative_funding():
    """OPEN_LONG with negative funding rate means longs receive — should not be penalized."""
    context = _context(funding_rate=Decimal("-0.001"))
    intent = _intent("OPEN_LONG")

    decision = QualityScorer().score(
        intent=intent, context=context, cost=_cost(), exit_plan=_exit_plan()
    )

    assert "funding_against_signal" not in decision.penalties
    assert decision.quality_score == 95


def test_quality_scorer_penalizes_open_long_with_positive_funding():
    """OPEN_LONG with positive funding rate means longs pay shorts — must be penalized."""
    context = _context(funding_rate=Decimal("0.001"))
    intent = _intent("OPEN_LONG")

    decision = QualityScorer().score(
        intent=intent, context=context, cost=_cost(), exit_plan=_exit_plan()
    )

    assert "funding_against_signal" in decision.penalties
    assert decision.quality_score == 90


def test_quality_scorer_penalizes_open_short_with_negative_funding():
    """OPEN_SHORT with negative funding rate means shorts pay longs — must be penalized."""
    context = _context(funding_rate=Decimal("-0.001"))
    intent = _intent("OPEN_SHORT")

    decision = QualityScorer().score(
        intent=intent, context=context, cost=_cost(), exit_plan=_exit_plan()
    )

    assert "funding_against_signal" in decision.penalties
    assert decision.quality_score == 90


def test_quality_scorer_does_not_penalize_open_short_with_positive_funding():
    """OPEN_SHORT with positive funding rate means shorts receive — should not be penalized."""
    context = _context(funding_rate=Decimal("0.001"))
    intent = _intent("OPEN_SHORT")

    decision = QualityScorer().score(
        intent=intent, context=context, cost=_cost(), exit_plan=_exit_plan()
    )

    assert "funding_against_signal" not in decision.penalties
    assert decision.quality_score == 95


def test_quality_scorer_does_not_penalize_close_position_with_negative_funding():
    """CLOSE_POSITION must not receive any entry-only economic penalty."""
    context = _context(
        funding_rate=Decimal("-0.001"), volatility=Decimal("0.10"), position_open=True
    )
    intent = _intent("CLOSE_POSITION")

    decision = QualityScorer().score(
        intent=intent,
        context=context,
        cost=_cost(),
        exit_plan=ExitPlan(
            stop_price=None,
            take_profit_price=None,
            expected_net_gain=None,
            expected_net_loss=None,
            reward_risk=None,
        ),
    )

    assert "funding_against_signal" not in decision.penalties
    assert "high_volatility" not in decision.penalties
    assert "high_cost_bps" not in decision.penalties
    assert decision.status != TradePlanStatus.BLOCK_ECONOMIC.value


def test_quality_scorer_does_not_penalize_close_position_with_high_volatility():
    """CLOSE_POSITION must not receive the high_volatility penalty."""
    context = _context(volatility=Decimal("0.20"), position_open=True)
    intent = _intent("CLOSE_POSITION")

    decision = QualityScorer().score(
        intent=intent,
        context=context,
        cost=_cost(),
        exit_plan=ExitPlan(
            stop_price=None,
            take_profit_price=None,
            expected_net_gain=None,
            expected_net_loss=None,
            reward_risk=None,
        ),
    )

    assert "high_volatility" not in decision.penalties
    assert decision.quality_score == 95


def test_quality_scorer_does_not_penalize_close_position_with_high_cost_bps():
    """CLOSE_POSITION must not receive the high_cost_bps penalty."""
    context = _context(position_open=True)
    intent = _intent("CLOSE_POSITION")

    decision = QualityScorer().score(
        intent=intent,
        context=context,
        cost=_cost(round_trip=Decimal("2.00")),
        exit_plan=ExitPlan(
            stop_price=None,
            take_profit_price=None,
            expected_net_gain=None,
            expected_net_loss=None,
            reward_risk=None,
        ),
    )

    assert "high_cost_bps" not in decision.penalties


def test_quality_scorer_penalizes_open_long_with_high_cost_bps():
    """OPEN_LONG with cost_bps > 15 must receive the high_cost_bps penalty."""
    context = _context()
    intent = _intent("OPEN_LONG")

    decision = QualityScorer().score(
        intent=intent,
        context=context,
        cost=_cost(round_trip=Decimal("2.00")),
        exit_plan=_exit_plan(),
    )

    assert "high_cost_bps" in decision.penalties
    assert decision.quality_score == 85


def test_quality_scorer_penalizes_open_long_with_high_volatility():
    """OPEN_LONG with volatility > 0.05 must receive the high_volatility penalty."""
    context = _context(volatility=Decimal("0.10"))
    intent = _intent("OPEN_LONG")

    decision = QualityScorer().score(
        intent=intent, context=context, cost=_cost(), exit_plan=_exit_plan()
    )

    assert "high_volatility" in decision.penalties
    assert decision.quality_score == 90


def test_quality_scorer_zero_funding_rate_never_penalized():
    """Zero funding rate must not trigger any penalty for any action."""
    for action in ("OPEN_LONG", "OPEN_SHORT"):
        context = _context(funding_rate=Decimal("0"))
        intent = _intent(action)

        decision = QualityScorer().score(
            intent=intent, context=context, cost=_cost(), exit_plan=_exit_plan()
        )

        assert "funding_against_signal" not in decision.penalties, (
            f"Unexpected penalty for {action}"
        )


def test_quality_scorer_none_funding_rate_never_penalized():
    """None funding rate (unavailable) must not trigger any penalty."""
    for action in ("OPEN_LONG", "OPEN_SHORT"):
        context = _context(funding_rate=None)
        intent = _intent(action)

        decision = QualityScorer().score(
            intent=intent, context=context, cost=_cost(), exit_plan=_exit_plan()
        )

        assert "funding_against_signal" not in decision.penalties, (
            f"Unexpected penalty for {action}"
        )
