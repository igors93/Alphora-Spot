"""Tests for PortfolioRiskEngine.evaluate_trade_intent with override_notional.

These verify that the portfolio risk evaluation uses the exact notional that
will reach execution (e.g. after smart sizing) rather than the static value
from the intent metadata or symbol profile.
"""

from decimal import Decimal

from config.symbol_context import PortfolioRiskSettings, symbol_config_from_app_config
from governor.models import SymbolPriority
from governor.portfolio_risk_engine import PortfolioRiskEngine
from trading.intent import TradeIntent


def _intent(
    symbol: str = "BTCUSDT",
    action: str = "OPEN_LONG",
    notional: str = "100",
) -> TradeIntent:
    return TradeIntent(
        symbol=symbol,
        action=action,
        score=90,
        priority=SymbolPriority.READY,
        confidence=90,
        reason="ready",
        strategy_name="tests.Strategy",
        market_price="50000",
        suggested_next_check_seconds=0,
        metadata={"notional": notional},
    )


def _engine(max_total_exposure: float = 1000.0) -> PortfolioRiskEngine:
    return PortfolioRiskEngine(
        PortfolioRiskSettings(
            max_open_positions=10,
            max_total_exposure_usdt=max_total_exposure,
        )
    )


# ---------------------------------------------------------------------------
# Core contract: override_notional is used when supplied
# ---------------------------------------------------------------------------


def test_override_notional_is_used_instead_of_metadata(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="BTCUSDT")
    engine = _engine(max_total_exposure=150)

    # Intent says notional=100, which fits under limit of 150.
    # override says 200, which should be blocked.
    decision = engine.evaluate_trade_intent(
        _intent(notional="100"),
        [profile],
        {"open_positions": []},
        override_notional=Decimal("200"),
    )

    assert decision.allowed is False
    assert decision.reason == "MAX_TOTAL_EXPOSURE"
    assert decision.checks["candidate_notional"] == "200"


def test_override_notional_smaller_than_metadata_passes_when_under_limit(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="BTCUSDT")
    # Tight limit: only 50 allowed; metadata says 100 but override says 40.
    engine = _engine(max_total_exposure=50)

    decision = engine.evaluate_trade_intent(
        _intent(notional="100"),
        [profile],
        {"open_positions": []},
        override_notional=Decimal("40"),
    )

    assert decision.allowed is True
    assert decision.checks["candidate_notional"] == "40"


def test_without_override_uses_metadata_notional(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="BTCUSDT")
    engine = _engine(max_total_exposure=150)

    # metadata notional=100, no override → should pass (100 < 150)
    decision = engine.evaluate_trade_intent(
        _intent(notional="100"),
        [profile],
        {"open_positions": []},
    )

    assert decision.allowed is True
    assert decision.checks["candidate_notional"] == "100"


# ---------------------------------------------------------------------------
# Close position bypass is unaffected by override_notional
# ---------------------------------------------------------------------------


def test_close_position_is_not_blocked_regardless_of_override_notional(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="BTCUSDT")
    engine = _engine(max_total_exposure=0)  # zero limit → would block any open

    decision = engine.evaluate_trade_intent(
        _intent(action="CLOSE_POSITION", notional="999"),
        [profile],
        {"open_positions": []},
        override_notional=Decimal("999"),
    )

    assert decision.allowed is True
    assert decision.reason == "APPROVED_NO_NEW_POSITION"


# ---------------------------------------------------------------------------
# Balance check uses override_notional for margin calculation
# ---------------------------------------------------------------------------


def test_balance_check_uses_override_notional(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="BTCUSDT")
    engine = PortfolioRiskEngine(
        PortfolioRiskSettings(
            max_open_positions=10,
            max_total_exposure_usdt=10000,
        )
    )

    # available=100, override notional=5000 → margin required = 5000/1lev * 1.1 = 5500 > 100
    decision = engine.evaluate_trade_intent(
        _intent(notional="50"),  # metadata says 50, which would need only 55 margin
        [profile],
        {"open_positions": [], "available_balance_usdt": "100"},
        override_notional=Decimal("5000"),
    )

    assert decision.allowed is False
    assert decision.reason == "BALANCE_INSUFFICIENT"
    # Required margin should reflect the override notional, not 50
    assert Decimal(decision.checks["required_margin_usdt"]) > Decimal("100")


# ---------------------------------------------------------------------------
# Approval records approved_notional in checks
# ---------------------------------------------------------------------------


def test_approved_decision_includes_candidate_notional_in_checks(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="BTCUSDT")
    engine = _engine(max_total_exposure=1000)

    decision = engine.evaluate_trade_intent(
        _intent(notional="100"),
        [profile],
        {"open_positions": []},
        override_notional=Decimal("75"),
    )

    assert decision.allowed is True
    assert decision.checks.get("candidate_notional") == "75"
