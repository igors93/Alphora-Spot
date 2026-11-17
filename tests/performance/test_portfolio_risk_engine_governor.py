from dataclasses import replace

from config.symbol_context import (
    PortfolioRiskSettings,
    portfolio_settings_from_dict,
    symbol_config_from_app_config,
)
from governor.models import SymbolPriority
from governor.portfolio_risk_engine import (
    PortfolioRiskEngine,
    correlation_group,
    resolve_correlation_group,
)
from trading.intent import TradeIntent


def _intent(symbol: str = "APTUSDT", action: str = "OPEN_LONG") -> TradeIntent:
    return TradeIntent(
        symbol=symbol,
        action=action,
        score=95,
        priority=SymbolPriority.READY,
        confidence=95,
        reason="ready",
        strategy_name="tests.PortfolioStrategy",
        market_price="10",
        suggested_next_check_seconds=0,
        metadata={"notional": "50"},
    )


def test_portfolio_risk_blocks_max_open_positions(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="APTUSDT")
    engine = PortfolioRiskEngine(PortfolioRiskSettings(max_open_positions=1))

    decision = engine.evaluate_trade_intent(
        _intent(),
        [profile],
        {"open_positions": [{"symbol": "BTCUSDT", "direction": "LONG", "notional": "50"}]},
    )

    assert decision.allowed is False
    assert decision.reason == "MAX_OPEN_POSITIONS"


def test_portfolio_risk_blocks_total_exposure(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="APTUSDT")
    engine = PortfolioRiskEngine(
        PortfolioRiskSettings(max_open_positions=5, max_total_exposure_usdt=80)
    )

    decision = engine.evaluate_trade_intent(
        _intent(),
        [profile],
        {"open_positions": [{"symbol": "BTCUSDT", "direction": "LONG", "notional": "50"}]},
    )

    assert decision.allowed is False
    assert decision.reason == "MAX_TOTAL_EXPOSURE"


def test_portfolio_risk_approves_when_limits_allow(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="APTUSDT")
    engine = PortfolioRiskEngine(
        PortfolioRiskSettings(max_open_positions=5, max_total_exposure_usdt=200)
    )

    decision = engine.evaluate_trade_intent(_intent(), [profile], {"open_positions": []})

    assert decision.allowed is True
    assert decision.reason == "APPROVED"


def test_portfolio_risk_accepts_short_when_short_limits_allow(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="APTUSDT")
    engine = PortfolioRiskEngine(
        PortfolioRiskSettings(
            max_open_positions=5,
            max_total_exposure_usdt=200,
            max_short_exposure_usdt=100,
        )
    )

    decision = engine.evaluate_trade_intent(
        _intent(action="OPEN_SHORT"),
        [profile],
        {
            "open_positions": [{"symbol": "BTCUSDT", "direction": "LONG", "notional": "50"}],
            "available_balance_usdt": "100",
        },
    )

    assert decision.allowed is True
    assert decision.reason == "APPROVED"
    assert decision.checks["direction"] == "SHORT"
    assert decision.checks["direction_exposure_after"] == "50"


def test_portfolio_risk_blocks_short_exposure_limit(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="APTUSDT")
    engine = PortfolioRiskEngine(
        PortfolioRiskSettings(max_open_positions=5, max_short_exposure_usdt=80)
    )

    decision = engine.evaluate_trade_intent(
        _intent(action="OPEN_SHORT"),
        [profile],
        {
            "open_positions": [{"symbol": "BTCUSDT", "direction": "SHORT", "notional": "50"}],
            "available_balance_usdt": "100",
        },
    )

    assert decision.allowed is False
    assert decision.reason == "MAX_SHORT_EXPOSURE"


def test_portfolio_risk_uses_margin_not_notional_for_available_balance(config_factory):
    profile = replace(symbol_config_from_app_config(config_factory(), symbol="APTUSDT"), leverage=5)
    engine = PortfolioRiskEngine(PortfolioRiskSettings(max_open_positions=5))

    decision = engine.evaluate_trade_intent(
        _intent(),
        [profile],
        {
            "open_positions": [],
            "available_balance_usdt": "25",
        },
    )

    assert decision.allowed is True
    assert decision.reason == "APPROVED"
    assert decision.checks["required_margin_usdt"] == "10"
    assert decision.checks["required_margin_with_safety_usdt"] == "11.00"


def test_portfolio_risk_blocks_when_estimated_margin_is_insufficient(config_factory):
    profile = replace(symbol_config_from_app_config(config_factory(), symbol="APTUSDT"), leverage=5)
    engine = PortfolioRiskEngine(PortfolioRiskSettings(max_open_positions=5))

    decision = engine.evaluate_trade_intent(
        _intent(),
        [profile],
        {
            "open_positions": [],
            "available_balance_usdt": "10.99",
        },
    )

    assert decision.allowed is False
    assert decision.reason == "BALANCE_INSUFFICIENT"


def test_portfolio_risk_blocks_new_entries_when_daily_pnl_is_incomplete(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="APTUSDT")
    engine = PortfolioRiskEngine(
        PortfolioRiskSettings(max_open_positions=5, max_daily_loss_usdt=20)
    )

    decision = engine.evaluate_trade_intent(
        _intent(),
        [profile],
        {
            "open_positions": [],
            "available_balance_usdt": "100",
            "pnl_unknown_closes": 1,
        },
    )

    assert decision.allowed is False
    assert decision.reason == "DAILY_PNL_INCOMPLETE"


def test_portfolio_correlation_groups_are_basic_and_explicit():
    assert correlation_group("BTCUSDT") == "BTC"
    assert correlation_group("ETHUSDT") == "ETH"
    assert correlation_group("APTUSDT") == "ALT_USDT"


def test_portfolio_correlation_group_resolution_uses_fallbacks_by_default():
    settings = PortfolioRiskSettings()

    assert resolve_correlation_group("BTCUSDT", settings) == ("BTC", "FALLBACK")
    assert resolve_correlation_group("ETHUSDT", settings) == ("ETH", "FALLBACK")
    assert resolve_correlation_group("SOLUSDT", settings) == ("ALT_USDT", "FALLBACK")


def test_portfolio_correlation_group_resolution_uses_configured_group():
    settings = PortfolioRiskSettings(correlation_groups={"SOLUSDT": " L1_HIGH_BETA "})

    assert resolve_correlation_group("solusdt", settings) == (
        "L1_HIGH_BETA",
        "CONFIGURED",
    )


def test_portfolio_settings_loads_correlation_groups_normalized():
    settings = portfolio_settings_from_dict(
        {
            "correlation_groups": {"solusdt": " l1_high_beta "},
            "correlation_group_mode": "enforce",
        }
    )

    assert settings.correlation_groups == {"SOLUSDT": "L1_HIGH_BETA"}
    assert settings.correlation_group_mode == "ENFORCE"


def test_portfolio_group_exposure_can_use_configured_group_in_enforce_mode(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="SOLUSDT")
    engine = PortfolioRiskEngine(
        PortfolioRiskSettings(
            max_open_positions=5,
            max_group_exposure_usdt=80,
            correlation_groups={
                "SOLUSDT": "L1_HIGH_BETA",
                "APTUSDT": "L1_HIGH_BETA",
            },
            correlation_group_mode="ENFORCE",
        )
    )

    decision = engine.evaluate_trade_intent(
        _intent(symbol="SOLUSDT"),
        [profile],
        {
            "open_positions": [{"symbol": "APTUSDT", "direction": "LONG", "notional": "50"}],
            "available_balance_usdt": "1000",
        },
    )

    assert decision.allowed is False
    assert decision.reason == "MAX_GROUP_EXPOSURE"
    assert decision.checks["group"] == "L1_HIGH_BETA"
    assert decision.checks["group_source"] == "CONFIGURED"
    assert decision.checks["configured_correlation_groups_count"] == 2


def test_portfolio_configured_group_observe_mode_does_not_block(config_factory):
    profile = symbol_config_from_app_config(config_factory(), symbol="SOLUSDT")
    engine = PortfolioRiskEngine(
        PortfolioRiskSettings(
            max_open_positions=5,
            max_group_exposure_usdt=80,
            correlation_groups={
                "SOLUSDT": "L1_HIGH_BETA",
                "APTUSDT": "L1_HIGH_BETA",
            },
            correlation_group_mode="OBSERVE",
        )
    )

    decision = engine.evaluate_trade_intent(
        _intent(symbol="SOLUSDT"),
        [profile],
        {
            "open_positions": [{"symbol": "APTUSDT", "direction": "LONG", "notional": "50"}],
            "available_balance_usdt": "1000",
        },
    )

    assert decision.allowed is True
    assert decision.checks["group_exposure_shadow_blocked"] is True
