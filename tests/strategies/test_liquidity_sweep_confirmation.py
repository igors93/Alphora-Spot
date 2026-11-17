from decimal import Decimal

from domain.candle import Candle
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection
from strategies.futures.fixed.liquidity_sweep_reversal import (
    LiquiditySweepReversalStrategy,
)


def _candle(
    index: int,
    interval: str,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    volume: str = "10",
) -> Candle:
    duration = 3_600_000 if interval == "1h" else 900_000
    return Candle(
        symbol="BTCUSDT",
        interval=interval,
        open_time_ms=1_577_836_800_000 + index * duration,
        close_time_ms=1_577_836_800_000 + (index + 1) * duration - 1,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        is_closed=True,
        source="test",
    )


def _market(primary: list[Candle], higher: list[Candle]) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        price=primary[-1].close,
        prices=[item.close for item in primary],
        candles={"15m": tuple(primary), "1h": tuple(higher)},
    )


def test_liquidity_sweep_waits_for_next_candle_confirmation_before_entry():
    strategy = LiquiditySweepReversalStrategy(
        primary_history=31,
        higher_timeframe_history=51,
        entry_confirmation_candles=1,
    )
    primary = [_candle(index, "15m", volume=str(10 + index % 3)) for index in range(30)]
    primary.append(
        _candle(
            30,
            "15m",
            open_="99.6",
            high="101",
            low="98",
            close="100",
            volume="13",
        )
    )
    higher = [_candle(index, "1h") for index in range(51)]
    state = {}
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )

    setup = strategy.score_symbol("BTCUSDT", _market(primary, higher), position, state)

    assert setup.action == "HOLD"
    assert setup.metadata["entry_confirmation"]["status"] == "PENDING"

    primary.append(
        _candle(
            31,
            "15m",
            open_="100",
            high="101.2",
            low="99.2",
            close="100.8",
            volume="20",
        )
    )
    confirmed = strategy.score_symbol(
        "BTCUSDT",
        _market(primary[-31:], higher),
        position,
        state,
    )

    assert confirmed.action == "OPEN_LONG"
    assert confirmed.metadata["entry_confirmation"]["status"] == "CONFIRMED"
    assert Decimal(confirmed.metadata["recommended_stop_price"]) < Decimal("98")


def test_liquidity_sweep_rejects_extreme_volume_shock():
    strategy = LiquiditySweepReversalStrategy(
        primary_history=31,
        higher_timeframe_history=51,
    )
    primary = [_candle(index, "15m", volume=str(10 + index % 3)) for index in range(30)]
    primary.append(
        _candle(
            30,
            "15m",
            open_="99.6",
            high="101",
            low="98",
            close="100",
            volume="100",
        )
    )
    higher = [_candle(index, "1h") for index in range(51)]
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )

    score = strategy.score_symbol("BTCUSDT", _market(primary, higher), position, {})

    assert score.action == "HOLD"
    assert score.metadata["long_conditions"]["volume_climax"] is True
    assert score.metadata["long_conditions"]["volume_not_extreme"] is False


def _flat_position() -> FuturesPosition:
    return FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def _setup_series() -> list[Candle]:
    primary = [_candle(index, "15m", volume=str(10 + index % 3)) for index in range(30)]
    primary.append(
        _candle(
            30,
            "15m",
            open_="99.6",
            high="101",
            low="98",
            close="100",
            volume="13",
        )
    )
    return primary


def _confirmation_candle(index: int) -> Candle:
    return _candle(
        index,
        "15m",
        open_="100",
        high="101.2",
        low="99.2",
        close="100.8",
        volume="20",
    )


def test_liquidity_sweep_reevaluating_setup_candle_never_opens():
    strategy = LiquiditySweepReversalStrategy(
        primary_history=31,
        higher_timeframe_history=51,
        entry_confirmation_candles=1,
    )
    primary = _setup_series()
    higher = [_candle(index, "1h") for index in range(51)]
    state: dict = {}

    first = strategy.score_symbol("BTCUSDT", _market(primary, higher), _flat_position(), state)
    again = strategy.score_symbol("BTCUSDT", _market(primary, higher), _flat_position(), state)

    assert first.action == "HOLD"
    assert again.action == "HOLD"
    assert again.metadata["entry_confirmation"]["status"] == "PENDING"

    primary.append(_confirmation_candle(31))
    confirmed = strategy.score_symbol(
        "BTCUSDT",
        _market(primary[-31:], higher),
        _flat_position(),
        state,
    )

    assert confirmed.action == "OPEN_LONG"


def test_liquidity_sweep_pending_setup_expires_after_skipped_candle():
    strategy = LiquiditySweepReversalStrategy(
        primary_history=31,
        higher_timeframe_history=51,
        entry_confirmation_candles=1,
    )
    primary = _setup_series()
    higher = [_candle(index, "1h") for index in range(51)]
    state: dict = {}

    strategy.score_symbol("BTCUSDT", _market(primary, higher), _flat_position(), state)
    # Candle 31 was never evaluated; candle 32 can no longer confirm the setup.
    primary.append(_candle(31, "15m"))
    primary.append(_confirmation_candle(32))
    late = strategy.score_symbol(
        "BTCUSDT",
        _market(primary[-31:], higher),
        _flat_position(),
        state,
    )

    assert late.action == "HOLD"
    assert late.metadata["entry_confirmation"]["status"] == "EXPIRED_OR_INVALIDATED"
    assert "liquidity_sweep_pending_confirmation" not in state
