from dataclasses import dataclass
from decimal import Decimal

from domain.models import TradeAction
from futures.exit_models import ATRExitModel, FixedPctExitModel


@dataclass(frozen=True)
class Candle:
    high: Decimal
    low: Decimal
    close: Decimal


def test_fixed_pct_exit_model_preserves_current_long_behavior(symbol_rules):
    result = FixedPctExitModel().calculate(
        action=TradeAction.OPEN_LONG,
        entry_price=Decimal("100"),
        rules=symbol_rules,
        stop_loss_pct=Decimal("0.01"),
        take_profit_pct=Decimal("0.02"),
    )

    assert result.stop_price == Decimal("99.00")
    assert result.take_profit_price == Decimal("102.00")
    assert result.fallback_used is False


def test_atr_exit_model_falls_back_without_history(symbol_rules):
    result = ATRExitModel(period=14, stop_multiplier=1.5, take_multiplier=2).calculate(
        action=TradeAction.OPEN_LONG,
        entry_price=Decimal("100"),
        rules=symbol_rules,
        stop_loss_pct=Decimal("0.01"),
        take_profit_pct=Decimal("0.02"),
        candles=[],
    )

    assert result.stop_price == Decimal("99.00")
    assert result.take_profit_price == Decimal("102.00")
    assert result.fallback_used is True
    assert "ATR sem historico suficiente" in result.warning


def test_atr_exit_model_calculates_prices_with_history(symbol_rules):
    candles = [
        Candle(Decimal("101"), Decimal("99"), Decimal("100")),
        Candle(Decimal("103"), Decimal("100"), Decimal("102")),
        Candle(Decimal("104"), Decimal("101"), Decimal("103")),
    ]

    result = ATRExitModel(period=2, stop_multiplier=1.5, take_multiplier=2).calculate(
        action=TradeAction.OPEN_LONG,
        entry_price=Decimal("100"),
        rules=symbol_rules,
        stop_loss_pct=Decimal("0.01"),
        take_profit_pct=Decimal("0.02"),
        candles=candles,
    )

    assert result.stop_price == Decimal("95.50")
    assert result.take_profit_price == Decimal("106.00")
    assert result.fallback_used is False
