from governor.models import (
    GovernorDecision,
    PortfolioSnapshot,
    SymbolPriority,
    SymbolRuntimeSnapshot,
    SymbolRuntimeStatus,
)
from trading.intent import TradeIntent


def test_trade_intent_normalizes_symbol_and_action():
    intent = TradeIntent(
        symbol="aptusdt",
        action="open_long",
        score=95,
        priority=SymbolPriority.READY,
        confidence=90,
        reason="ready",
        strategy_name="tests.ModelStrategy",
        market_price="10",
        suggested_next_check_seconds=0,
    )

    assert intent.symbol == "APTUSDT"
    assert intent.action == "OPEN_LONG"


def test_governor_snapshot_defaults_are_safe():
    snapshot = SymbolRuntimeSnapshot(
        symbol="BTCUSDT",
        enabled=True,
        status=SymbolRuntimeStatus.MONITORING,
    )
    portfolio = PortfolioSnapshot()
    decision = GovernorDecision(
        allowed=True,
        symbol="BTCUSDT",
        action="HOLD",
        reason="APPROVED",
    )

    assert snapshot.priority is SymbolPriority.COLD
    assert portfolio.total_open_positions == 0
    assert decision.allowed is True
