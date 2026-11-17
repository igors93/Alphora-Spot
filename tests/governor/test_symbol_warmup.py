from decimal import Decimal
from types import SimpleNamespace

from domain.models import MarketSnapshot
from governor.market_data_router import LightMarketSnapshot
from governor.services.symbol_health import SymbolHealthService, build_symbol_quarantine
from governor.symbol_warmup import SymbolWarmupEvaluator


def _snapshot(*, prices=1, stale=False, age=0.0):
    values = [Decimal("100")] * prices
    return LightMarketSnapshot(
        symbol="BTCUSDT",
        market=MarketSnapshot(symbol="BTCUSDT", price=Decimal("100"), prices=values),
        timestamp=1.0,
        data_age_seconds=age,
        stale=stale,
    )


def test_symbol_warmup_blocks_new_entry_with_few_prices():
    result = SymbolWarmupEvaluator(min_observed_prices=3).evaluate(
        symbol="BTCUSDT",
        snapshot=_snapshot(prices=2),
        action="OPEN_LONG",
    )

    assert result.complete is False
    assert result.blocks_new_entry is True
    assert result.reason == "SYMBOL_WARMUP_INCOMPLETE"


def test_symbol_warmup_does_not_block_close_position():
    result = SymbolWarmupEvaluator(min_observed_prices=3).evaluate(
        symbol="BTCUSDT",
        snapshot=_snapshot(prices=1),
        action="CLOSE_POSITION",
    )

    assert result.complete is False
    assert result.blocks_new_entry is False


def test_symbol_warmup_does_not_block_open_position_protection():
    result = SymbolWarmupEvaluator(min_observed_prices=3).evaluate(
        symbol="BTCUSDT",
        snapshot=_snapshot(prices=1),
        action="OPEN_LONG",
        position_open=True,
    )

    assert result.complete is False
    assert result.blocks_new_entry is False


def test_symbol_warmup_releases_when_prices_are_ready_and_fresh():
    result = SymbolWarmupEvaluator(min_observed_prices=3).evaluate(
        symbol="BTCUSDT",
        snapshot=_snapshot(prices=3, stale=False, age=1.0),
        action="OPEN_SHORT",
    )

    assert result.complete is True
    assert result.blocks_new_entry is False


def test_symbol_warmup_blocks_new_entry_when_stale():
    result = SymbolWarmupEvaluator(
        min_observed_prices=3,
        max_data_age_seconds=30.0,
    ).evaluate(
        symbol="BTCUSDT",
        snapshot=_snapshot(prices=3, stale=True, age=31.0),
        action="OPEN_LONG",
    )

    assert result.complete is False
    assert result.blocks_new_entry is True
    assert "STALE_DATA" in result.diagnostics["failures"]


def test_governor_symbol_warmup_uses_strategy_required_history():
    health = SymbolHealthService(
        config=SimpleNamespace(),
        strategy=SimpleNamespace(
            _global_strategy=SimpleNamespace(
                metadata=SimpleNamespace(required_history=7),
            )
        ),
        quarantine=build_symbol_quarantine(SimpleNamespace()),
        board=None,
        journal=None,
        logger=None,
    )

    assert health.warmup_min_observed_prices() == 7
