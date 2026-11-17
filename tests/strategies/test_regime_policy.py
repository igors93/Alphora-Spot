from futures.trend import MarketRegime, TrendContext, TrendDirection, TrendStrength
from futures.trend.regime_policy import evaluate_regime_policy


def _context(
    *,
    direction: TrendDirection,
    strength: TrendStrength,
    regime: MarketRegime,
) -> TrendContext:
    return TrendContext(
        symbol="BTCUSDT",
        timestamp="2026-01-01T00:00:00Z",
        direction=direction,
        strength=strength,
        regime=regime,
        confidence=0.9,
        fast_ma=101.0,
        slow_ma=100.0,
        fast_ma_slope=0.2,
        slow_ma_slope=0.1,
        price_above_fast_ma=True,
        price_above_slow_ma=True,
        price_distance_to_fast_ma_pct=0.1,
        price_distance_to_slow_ma_pct=0.2,
        volatility_pct=0.5,
        chop_score=0.1,
        data_points=80,
    )


def test_strong_counter_trend_entry_is_blocked():
    decision = evaluate_regime_policy(
        action="OPEN_SHORT",
        trend_context=_context(
            direction=TrendDirection.UP,
            strength=TrendStrength.STRONG,
            regime=MarketRegime.TREND_UP,
        ),
        mode="BLOCK_STRONG_COUNTER_TREND",
    )

    assert decision.allowed is False
    assert decision.reason == "STRONG_COUNTER_TREND_ENTRY_BLOCKED"
    assert decision.against_trend is True


def test_weak_counter_trend_entry_remains_available_in_strong_only_mode():
    decision = evaluate_regime_policy(
        action="OPEN_SHORT",
        trend_context=_context(
            direction=TrendDirection.UP,
            strength=TrendStrength.WEAK,
            regime=MarketRegime.TREND_UP,
        ),
        mode="BLOCK_STRONG_COUNTER_TREND",
    )

    assert decision.allowed is True


def test_close_is_never_blocked_by_regime_policy():
    decision = evaluate_regime_policy(
        action="CLOSE_POSITION",
        trend_context=_context(
            direction=TrendDirection.UP,
            strength=TrendStrength.STRONG,
            regime=MarketRegime.HIGH_VOLATILITY,
        ),
        mode="CONSERVATIVE",
    )

    assert decision.allowed is True
    assert decision.reason == "REGIME_POLICY_NOT_AN_OPEN_ACTION"


def test_observe_mode_never_changes_execution():
    decision = evaluate_regime_policy(
        action="OPEN_LONG",
        trend_context=_context(
            direction=TrendDirection.DOWN,
            strength=TrendStrength.STRONG,
            regime=MarketRegime.TREND_DOWN,
        ),
        mode="OBSERVE",
    )

    assert decision.allowed is True
    assert decision.against_trend is True
