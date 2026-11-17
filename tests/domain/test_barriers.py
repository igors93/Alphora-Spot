"""The shared first-barrier rules (domain.barriers): what a stop/take outcome means."""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.barriers import (
    AMBIGUITY_NEAREST_TO_OPEN,
    AMBIGUITY_REJECT,
    AMBIGUITY_STOP_FIRST,
    AMBIGUITY_TAKE_FIRST,
    LIQUIDATION,
    OPEN,
    STOP_LOSS,
    TAKE_PROFIT,
    TIMEOUT,
    AmbiguousRejected,
    Barriers,
    first_barrier,
    hit_after_limit_fill,
    hit_at_open,
    hit_within_candle,
    limit_entry_touched,
)
from domain.candle import Candle

D = Decimal


def bar(open_, high, low, close, index=0):
    return Candle(
        symbol="BTCUSDT",
        interval="15m",
        open_time_ms=index * 900_000,
        close_time_ms=(index + 1) * 900_000 - 1,
        open=D(str(open_)),
        high=D(str(high)),
        low=D(str(low)),
        close=D(str(close)),
        volume=D("10"),
        is_closed=True,
    )


LONG = Barriers(direction="LONG", stop=D("99"), take=D("102"))
SHORT = Barriers(direction="SHORT", stop=D("101"), take=D("98"))


def test_gap_through_the_stop_fills_at_the_open():
    hit = hit_at_open(LONG, D("98.5"))

    assert hit is not None
    assert (hit.kind, hit.price, hit.exit_reason, hit.at_open) == (
        STOP_LOSS,
        D("98.5"),
        "gap_stop_loss",
        True,
    )


def test_gap_beyond_the_take_fills_at_the_target_as_maker():
    hit = hit_at_open(SHORT, D("97"))

    assert hit is not None
    assert (hit.kind, hit.price, hit.maker) == (TAKE_PROFIT, D("98"), True)


def test_liquidation_gap_comes_before_the_stop():
    barriers = Barriers(direction="LONG", stop=D("99"), take=D("102"), liquidation=D("99.5"))

    hit = hit_at_open(barriers, D("99.2"))

    assert hit is not None
    assert (hit.kind, hit.exit_reason) == (LIQUIDATION, "LIQUIDATION_GAP")


def test_stop_and_take_inside_the_candle():
    stop = hit_within_candle(LONG, bar(100, 101, 98.9, 99.5))
    take = hit_within_candle(SHORT, bar(100, 100.5, 97.5, 98.2))

    assert stop is not None and take is not None
    assert (stop.kind, stop.price, stop.maker) == (STOP_LOSS, D("99"), False)
    assert (take.kind, take.price, take.maker) == (TAKE_PROFIT, D("98"), True)


def test_take_needs_a_trade_through_tick_when_configured():
    barriers = Barriers(
        direction="LONG", stop=D("99"), take=D("102"), take_trigger_offset=D("0.10")
    )

    assert hit_within_candle(barriers, bar(100, 102.05, 99.5, 101)) is None
    assert hit_within_candle(barriers, bar(100, 102.10, 99.5, 101)) is not None


@pytest.mark.parametrize(
    ("policy", "kind", "resolution"),
    [
        (AMBIGUITY_STOP_FIRST, STOP_LOSS, "STOP_FIRST"),
        (AMBIGUITY_TAKE_FIRST, TAKE_PROFIT, "TAKE_FIRST"),
    ],
)
def test_ambiguous_candle_follows_the_policy(policy, kind, resolution):
    hit = hit_within_candle(LONG, bar(100, 103, 98, 100), ambiguity_policy=policy)

    assert hit is not None and not isinstance(hit, AmbiguousRejected)
    assert (hit.kind, hit.ambiguous, hit.ambiguity_resolution) == (kind, True, resolution)


def test_nearest_to_open_picks_the_closer_barrier():
    near_take = hit_within_candle(
        LONG, bar(101.5, 103, 98, 100), ambiguity_policy=AMBIGUITY_NEAREST_TO_OPEN
    )
    near_stop = hit_within_candle(
        LONG, bar(99.4, 103, 98, 100), ambiguity_policy=AMBIGUITY_NEAREST_TO_OPEN
    )

    assert near_take is not None and not isinstance(near_take, AmbiguousRejected)
    assert near_stop is not None and not isinstance(near_stop, AmbiguousRejected)
    assert (near_take.kind, near_take.ambiguity_resolution) == (TAKE_PROFIT, "NEAREST_TAKE")
    assert (near_stop.kind, near_stop.ambiguity_resolution) == (STOP_LOSS, "NEAREST_STOP")


def test_reject_policy_keeps_the_position_open():
    hit = hit_within_candle(LONG, bar(100, 103, 98, 100), ambiguity_policy=AMBIGUITY_REJECT)

    assert isinstance(hit, AmbiguousRejected)


def test_intrabar_liquidation_beyond_the_stop_is_ignored():
    # A liquidation price below the stop of a long is never reached first.
    barriers = Barriers(direction="LONG", stop=D("99"), take=D("102"), liquidation=D("98"))

    hit = hit_within_candle(barriers, bar(100, 100.5, 97.5, 98.5))

    assert hit is not None and not isinstance(hit, AmbiguousRejected)
    assert hit.kind == STOP_LOSS


def test_first_barrier_walks_until_a_hit():
    candles = [bar(100, 101, 99.5, 100.5, 0), bar(100.5, 102.3, 100.2, 102, 1)]

    resolution = first_barrier(LONG, candles, max_bars=5)

    assert (resolution.outcome, resolution.bars_held, resolution.exit_index) == (TAKE_PROFIT, 2, 1)
    assert resolution.maker_exit is True


def test_first_barrier_times_out_at_the_close_of_the_last_bar():
    candles = [bar(100, 100.5, 99.5, 100.2, index) for index in range(3)]

    resolution = first_barrier(LONG, candles, max_bars=3)

    assert (resolution.outcome, resolution.exit_price, resolution.bars_held) == (
        TIMEOUT,
        D("100.2"),
        3,
    )


def test_first_barrier_reports_open_when_history_ends_before_the_horizon():
    resolution = first_barrier(LONG, [bar(100, 100.5, 99.5, 100.2)], max_bars=4)

    assert (resolution.outcome, resolution.bars_held) == (OPEN, 1)


def test_ambiguous_candle_is_refined_with_lower_timeframe_candles():
    coarse = bar(100, 103, 98, 100)
    fine = [bar(100, 102.5, 99.8, 102.2), bar(102.2, 102.4, 98, 98.5)]

    conservative = first_barrier(LONG, [coarse], max_bars=1)
    refined = first_barrier(LONG, [coarse], max_bars=1, sub_candles=lambda _candle: fine)

    assert conservative.outcome == STOP_LOSS and conservative.ambiguous
    assert refined.outcome == TAKE_PROFIT
    assert refined.refined is True
    assert refined.exit_reason == "refined_take_profit"


def test_rejected_ambiguity_keeps_walking():
    candles = [bar(100, 103, 98, 100, 0), bar(100, 102.5, 99.5, 102, 1)]

    resolution = first_barrier(LONG, candles, max_bars=3, ambiguity_policy=AMBIGUITY_REJECT)

    assert (resolution.outcome, resolution.bars_held) == (TAKE_PROFIT, 2)


def test_limit_entry_touched_respects_trade_through():
    candle = bar(100, 100.5, 99.5, 100)

    assert limit_entry_touched("LONG", candle, D("99.5")) is True
    assert limit_entry_touched("LONG", candle, D("99.5"), trade_through_offset=D("0.10")) is False
    assert limit_entry_touched("SHORT", candle, D("100.5")) is True
    assert limit_entry_touched("SHORT", candle, D("100.5"), trade_through_offset=D("0.10")) is False


def test_hit_after_limit_fill_applies_conservative_same_candle_rules():
    stop_hit = hit_after_limit_fill(LONG, bar(100, 103, 98.9, 102.5))
    confirmed_take = hit_after_limit_fill(LONG, bar(100, 102.5, 99.5, 102.1))
    unconfirmed_take = hit_after_limit_fill(LONG, bar(100, 102.5, 99.5, 101.5))

    assert stop_hit is not None and not isinstance(stop_hit, AmbiguousRejected)
    assert (stop_hit.kind, stop_hit.exit_reason) == (
        STOP_LOSS,
        "limit_entry_same_candle_stop_loss",
    )
    assert confirmed_take is not None and not isinstance(confirmed_take, AmbiguousRejected)
    assert (confirmed_take.kind, confirmed_take.exit_reason, confirmed_take.maker) == (
        TAKE_PROFIT,
        "limit_entry_same_candle_take_profit_confirmed",
        True,
    )
    assert isinstance(unconfirmed_take, AmbiguousRejected)
    assert unconfirmed_take.ambiguity_resolution == "LIMIT_ENTRY_UNCONFIRMED_TAKE"


def test_invalid_direction_is_rejected():
    with pytest.raises(ValueError, match="direcao"):
        Barriers(direction="NONE", stop=D("1"), take=D("2"))
