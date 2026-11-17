"""Which barrier a price path touches first: the definition behind every stop/take outcome.

The backtest simulator, the AI opportunity labels and the live reconciliation of forecasts
all ask the same question — did the price reach the take profit before the stop loss? — so
they share these pure functions instead of each reimplementing the candle path rules:

- a gap at the open beyond a barrier fills at the open (stop, liquidation) or at the target
  (take, which rests as a limit order);
- inside a candle, liquidation comes first, then a candle that touches both the stop and the
  take is *ambiguous* and resolved by the configured policy;
- a take may require the price to trade one tick through it before it counts as filled.

Prices are ``Decimal``; candles need ``open``, ``high``, ``low`` and ``close``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

LONG = "LONG"
SHORT = "SHORT"

STOP_LOSS = "STOP_LOSS"
TAKE_PROFIT = "TAKE_PROFIT"
LIQUIDATION = "LIQUIDATION"
TIMEOUT = "TIMEOUT"
OPEN = "OPEN"
NOT_FILLED = "NOT_FILLED"

TP_FIRST = "TP_FIRST"
SL_FIRST = "SL_FIRST"

AMBIGUITY_STOP_FIRST = "CONSERVATIVE_STOP_FIRST"
AMBIGUITY_TAKE_FIRST = "OPTIMISTIC_TAKE_FIRST"
AMBIGUITY_NEAREST_TO_OPEN = "NEAREST_TO_OPEN"
AMBIGUITY_REJECT = "REJECT_AMBIGUOUS"


@dataclass(frozen=True)
class Barriers:
    """The exits of one position. ``take_trigger_offset`` is the trade-through tick, or 0."""

    direction: str
    stop: Decimal
    take: Decimal
    liquidation: Decimal | None = None
    take_trigger_offset: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.direction not in {LONG, SHORT}:
            raise ValueError(f"direcao invalida para barreiras: {self.direction!r}")


@dataclass(frozen=True)
class BarrierHit:
    """A barrier touched by one candle and the price the exit fills at."""

    kind: str
    price: Decimal
    exit_reason: str
    maker: bool = False
    at_open: bool = False
    # Stop and take both inside the candle; ``ambiguity_resolution`` says how it was decided.
    ambiguous: bool = False
    ambiguity_resolution: str = ""
    # Decided by the lower-timeframe candles inside an ambiguous candle.
    refined: bool = False


@dataclass(frozen=True)
class AmbiguousRejected:
    """Both barriers inside the candle under ``REJECT_AMBIGUOUS``: the position stays open."""

    ambiguous: bool = True
    ambiguity_resolution: str = "REJECTED"


def hit_at_open(barriers: Barriers, open_price: Decimal) -> BarrierHit | None:
    """A gap through a barrier at the candle open, checked before anything inside the candle."""

    price = Decimal(str(open_price))
    long = barriers.direction == LONG
    liquidation = barriers.liquidation
    if liquidation is not None and (price <= liquidation if long else price >= liquidation):
        return BarrierHit(LIQUIDATION, price, "LIQUIDATION_GAP", at_open=True)
    if price <= barriers.stop if long else price >= barriers.stop:
        return BarrierHit(STOP_LOSS, price, "gap_stop_loss", at_open=True)
    if price >= barriers.take if long else price <= barriers.take:
        return BarrierHit(TAKE_PROFIT, barriers.take, "gap_take_profit", maker=True, at_open=True)
    return None


def hit_within_candle(
    barriers: Barriers,
    candle: Any,
    *,
    ambiguity_policy: str = AMBIGUITY_STOP_FIRST,
) -> BarrierHit | AmbiguousRejected | None:
    """The barrier touched between the open and the close of ``candle`` (open gaps excluded)."""

    long = barriers.direction == LONG
    high = Decimal(str(candle.high))
    low = Decimal(str(candle.low))
    stop_hit = low <= barriers.stop if long else high >= barriers.stop
    take_hit = (
        high >= barriers.take + barriers.take_trigger_offset
        if long
        else low <= barriers.take - barriers.take_trigger_offset
    )
    liquidation = barriers.liquidation
    if liquidation is not None and (
        (low <= liquidation and liquidation >= barriers.stop)
        if long
        else (high >= liquidation and liquidation <= barriers.stop)
    ):
        return BarrierHit(LIQUIDATION, liquidation, "LIQUIDATION_INTRABAR")
    if stop_hit and take_hit:
        return _resolve_ambiguous(barriers, candle, ambiguity_policy)
    if stop_hit:
        return BarrierHit(STOP_LOSS, barriers.stop, "stop_loss")
    if take_hit:
        return BarrierHit(TAKE_PROFIT, barriers.take, "take_profit", maker=True)
    return None


def limit_entry_touched(
    direction: str,
    candle: Any,
    limit_price: Decimal,
    *,
    trade_through_offset: Decimal = Decimal("0"),
) -> bool:
    """Return whether ``candle`` traded at (or through) a resting ``limit_price`` entry."""

    side = str(direction or "").upper().strip()
    offset = max(Decimal("0"), Decimal(str(trade_through_offset)))
    limit = Decimal(str(limit_price))
    if side in {"OPEN_LONG", LONG}:
        return Decimal(str(candle.low)) <= limit - offset
    if side in {"OPEN_SHORT", SHORT}:
        return Decimal(str(candle.high)) >= limit + offset
    return False


def hit_after_limit_fill(
    barriers: Barriers,
    candle: Any,
) -> BarrierHit | AmbiguousRejected | None:
    """Conservative OHLC barrier check on the same candle an intrabar limit entry filled.

    An intrabar extreme may have occurred before the limit order filled: a touch
    of the stop is always treated as a stop loss, while a touch of the take
    profit only counts when the closing price confirms that the target stayed
    crossed; otherwise the candle is marked as an unconfirmed intrabar ambiguity
    and the position remains open.
    """

    long = barriers.direction == LONG
    high = Decimal(str(candle.high))
    low = Decimal(str(candle.low))
    close = Decimal(str(candle.close))
    stop_hit = low <= barriers.stop if long else high >= barriers.stop
    if stop_hit:
        return BarrierHit(
            STOP_LOSS,
            barriers.stop,
            "limit_entry_same_candle_stop_loss",
        )
    take_hit = (
        high >= barriers.take + barriers.take_trigger_offset
        if long
        else low <= barriers.take - barriers.take_trigger_offset
    )
    if take_hit:
        confirmed = close >= barriers.take if long else close <= barriers.take
        if confirmed:
            return BarrierHit(
                TAKE_PROFIT,
                barriers.take,
                "limit_entry_same_candle_take_profit_confirmed",
                maker=True,
            )
        return AmbiguousRejected(
            ambiguous=True,
            ambiguity_resolution="LIMIT_ENTRY_UNCONFIRMED_TAKE",
        )
    return None


def _resolve_ambiguous(
    barriers: Barriers,
    candle: Any,
    policy: str,
) -> BarrierHit | AmbiguousRejected:
    stop = BarrierHit(
        STOP_LOSS,
        barriers.stop,
        "ambiguous_stop_take_stop_loss",
        ambiguous=True,
        ambiguity_resolution="STOP_FIRST",
    )
    take = BarrierHit(
        TAKE_PROFIT,
        barriers.take,
        "ambiguous_stop_take_take_profit",
        maker=True,
        ambiguous=True,
        ambiguity_resolution="TAKE_FIRST",
    )
    normalized = str(policy or AMBIGUITY_STOP_FIRST).upper().strip()
    if normalized == AMBIGUITY_REJECT:
        return AmbiguousRejected()
    if normalized == AMBIGUITY_TAKE_FIRST:
        return take
    if normalized == AMBIGUITY_NEAREST_TO_OPEN:
        open_price = Decimal(str(candle.open))
        if abs(open_price - barriers.take) < abs(open_price - barriers.stop):
            return replace(take, ambiguity_resolution="NEAREST_TAKE")
        return replace(stop, ambiguity_resolution="NEAREST_STOP")
    return stop


@dataclass(frozen=True)
class BarrierResolution:
    """How a position opened before ``candles[0]`` would end over at most ``max_bars`` candles."""

    outcome: str
    exit_price: Decimal
    exit_reason: str
    bars_held: int
    exit_index: int
    maker_exit: bool = False
    gap: bool = False
    ambiguous: bool = False
    ambiguity_resolution: str = ""
    refined: bool = False


SubCandles = Callable[[Any], Sequence[Any]]


def first_barrier(
    barriers: Barriers,
    candles: Sequence[Any],
    *,
    max_bars: int,
    ambiguity_policy: str = AMBIGUITY_STOP_FIRST,
    sub_candles: SubCandles | None = None,
) -> BarrierResolution:
    """Walk ``candles`` (the ones after the entry) until a barrier or ``max_bars`` is reached.

    ``sub_candles(candle)`` may return the lower-timeframe candles inside an ambiguous candle;
    they are walked with the same rules (stop first if still ambiguous) and the result is marked
    ``refined``. Without a hit the position times out at the close of the last candle walked;
    with fewer candles than ``max_bars`` and no hit, the outcome is ``OPEN``.
    """

    limit = max(1, int(max_bars))
    for index, candle in enumerate(candles[:limit]):
        hit: BarrierHit | AmbiguousRejected | None = hit_at_open(
            barriers, Decimal(str(candle.open))
        )
        if hit is None:
            hit = _refined(barriers, candle, sub_candles) if sub_candles else None
            if hit is None:
                hit = hit_within_candle(barriers, candle, ambiguity_policy=ambiguity_policy)
        if isinstance(hit, AmbiguousRejected):
            continue
        if hit is not None:
            return BarrierResolution(
                outcome=hit.kind,
                exit_price=hit.price,
                exit_reason=hit.exit_reason,
                bars_held=index + 1,
                exit_index=index,
                maker_exit=hit.maker,
                gap=hit.at_open,
                ambiguous=hit.ambiguous,
                ambiguity_resolution=hit.ambiguity_resolution,
                refined=hit.refined,
            )
    walked = min(limit, len(candles))
    if walked < limit:
        last = candles[walked - 1] if walked else None
        return BarrierResolution(
            outcome=OPEN,
            exit_price=Decimal(str(last.close)) if last is not None else Decimal("0"),
            exit_reason="open",
            bars_held=walked,
            exit_index=walked - 1,
        )
    return BarrierResolution(
        outcome=TIMEOUT,
        exit_price=Decimal(str(candles[walked - 1].close)),
        exit_reason="time_barrier",
        bars_held=walked,
        exit_index=walked - 1,
    )


def _refined(barriers: Barriers, candle: Any, sub_candles: SubCandles) -> BarrierHit | None:
    """Resolve a candle that touches both barriers with its lower-timeframe candles."""

    first_pass = hit_within_candle(barriers, candle, ambiguity_policy=AMBIGUITY_STOP_FIRST)
    if not (isinstance(first_pass, BarrierHit) and first_pass.ambiguous):
        return None
    inner = list(sub_candles(candle) or ())
    if not inner:
        return None
    for index, sub in enumerate(inner):
        hit: BarrierHit | AmbiguousRejected | None = (
            hit_at_open(barriers, Decimal(str(sub.open))) if index else None
        )
        if hit is None:
            hit = hit_within_candle(barriers, sub, ambiguity_policy=AMBIGUITY_STOP_FIRST)
        if isinstance(hit, BarrierHit):
            return replace(
                hit,
                exit_reason=f"refined_{hit.exit_reason}",
                at_open=False,
                ambiguity_resolution=hit.ambiguity_resolution or "REFINED",
                refined=True,
            )
    return None


__all__ = [
    "AMBIGUITY_NEAREST_TO_OPEN",
    "AMBIGUITY_REJECT",
    "AMBIGUITY_STOP_FIRST",
    "AMBIGUITY_TAKE_FIRST",
    "AmbiguousRejected",
    "BarrierHit",
    "BarrierResolution",
    "Barriers",
    "LIQUIDATION",
    "LONG",
    "NOT_FILLED",
    "OPEN",
    "SHORT",
    "SL_FIRST",
    "STOP_LOSS",
    "TAKE_PROFIT",
    "TIMEOUT",
    "TP_FIRST",
    "first_barrier",
    "hit_after_limit_fill",
    "hit_at_open",
    "hit_within_candle",
    "limit_entry_touched",
]
