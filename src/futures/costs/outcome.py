"""Costs and net R of each outcome of a trade: take profit first or stop loss first.

The round-trip estimate of ``CostModel`` charges one taker exit whatever happens. The two
outcomes of a protected trade do not cost the same: the take rests as a limit order and exits
as maker, without crossing the spread or slipping; the stop exits as a taker, crossing half the
spread and slipping (more in a fast market). Funding has a sign: with a positive rate the long
pays and the short receives.

From the per-unit costs of each outcome, in units of the risk ``R = |entry - stop|``:

    r_win  = (|take - entry| - cost_take) / R
    r_loss = (|entry - stop| + cost_stop) / R
    p*     = r_loss / (r_win + r_loss)

``p*`` is the probability of reaching the take first above which the trade has a positive
expected value (ignoring a time exit): the bar a forecast must clear (OPP-007).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from futures.costs.model import CostModel

BPS = Decimal("10000")


@dataclass(frozen=True)
class OutcomeCosts:
    direction: str
    entry_price: Decimal
    stop_price: Decimal
    take_price: Decimal
    risk_per_unit: Decimal
    reward_per_unit: Decimal
    cost_take_per_unit: Decimal
    cost_stop_per_unit: Decimal
    r_win: Decimal
    r_loss: Decimal
    breakeven_probability: Decimal
    take_exit_maker: bool
    stop_slippage_bps: Decimal
    funding_events: int

    @property
    def cost_take_bps(self) -> Decimal:
        return self.cost_take_per_unit / self.entry_price * BPS

    @property
    def cost_stop_bps(self) -> Decimal:
        return self.cost_stop_per_unit / self.entry_price * BPS

    def expected_r(
        self,
        *,
        p_take: Decimal,
        p_stop: Decimal,
        p_timeout: Decimal = Decimal("0"),
        r_timeout: Decimal = Decimal("0"),
        p_fill: Decimal = Decimal("1"),
    ) -> Decimal:
        """Expected net R: ``p_fill * (p_take * r_win - p_stop * r_loss + p_timeout * r_timeout)``."""

        fill_prob = min(Decimal("1"), max(Decimal("0"), Decimal(str(p_fill))))
        conditional = p_take * self.r_win - p_stop * self.r_loss + p_timeout * r_timeout
        return fill_prob * conditional

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "risk_per_unit": str(self.risk_per_unit),
            "reward_per_unit": str(self.reward_per_unit),
            "cost_take_bps": str(self.cost_take_bps.quantize(Decimal("0.0001"))),
            "cost_stop_bps": str(self.cost_stop_bps.quantize(Decimal("0.0001"))),
            "r_win": str(self.r_win.quantize(Decimal("0.0001"))),
            "r_loss": str(self.r_loss.quantize(Decimal("0.0001"))),
            "breakeven_probability": str(self.breakeven_probability.quantize(Decimal("0.0001"))),
            "take_exit_maker": self.take_exit_maker,
            "stop_slippage_bps": str(self.stop_slippage_bps),
            "funding_events": self.funding_events,
        }


def estimate_outcome_costs(
    *,
    direction: str,
    entry_price: Decimal,
    stop_price: Decimal,
    take_price: Decimal,
    cost_model: CostModel,
    spread_bps: Decimal | None = None,
    funding_rate: Decimal | None = None,
    funding_events: int = 1,
    market_entry: bool = True,
    take_exit_maker: bool = True,
    stop_slippage_bps: Decimal | None = None,
) -> OutcomeCosts | None:
    """Per-outcome costs of a trade; ``None`` when the stop or the take is on the wrong side."""

    side = str(direction or "").upper().strip()
    if side not in {"LONG", "SHORT"}:
        return None
    entry = Decimal(str(entry_price))
    stop = Decimal(str(stop_price))
    take = Decimal(str(take_price))
    long = side == "LONG"
    risk = entry - stop if long else stop - entry
    reward = take - entry if long else entry - take
    if entry <= 0 or risk <= 0 or reward <= 0:
        return None

    half_spread = (spread_bps if spread_bps is not None else cost_model.estimated_spread_bps) / 2
    slippage = cost_model.estimated_slippage_bps
    stop_slippage = stop_slippage_bps if stop_slippage_bps is not None else slippage
    taker = cost_model.taker_fee_pct
    maker = cost_model.maker_fee_pct

    entry_cost = entry * (taker if market_entry else maker)
    if market_entry:
        entry_cost += entry * (half_spread + slippage) / BPS
    rate = funding_rate if funding_rate is not None else cost_model.estimated_funding_rate
    funding = entry * Decimal(str(rate)) * Decimal(int(funding_events)) * (1 if long else -1)

    take_exit = take * (maker if take_exit_maker else taker)
    if not take_exit_maker:
        take_exit += take * (half_spread + slippage) / BPS
    stop_exit = stop * taker + stop * (half_spread + stop_slippage) / BPS

    cost_take = entry_cost + take_exit + funding
    cost_stop = entry_cost + stop_exit + funding
    r_win = (reward - cost_take) / risk
    r_loss = (risk + cost_stop) / risk
    breakeven = r_loss / (r_win + r_loss) if r_win > 0 else Decimal("1")
    return OutcomeCosts(
        direction=side,
        entry_price=entry,
        stop_price=stop,
        take_price=take,
        risk_per_unit=risk,
        reward_per_unit=reward,
        cost_take_per_unit=cost_take,
        cost_stop_per_unit=cost_stop,
        r_win=r_win,
        r_loss=r_loss,
        breakeven_probability=min(Decimal("1"), max(Decimal("0"), breakeven)),
        take_exit_maker=take_exit_maker,
        stop_slippage_bps=Decimal(str(stop_slippage)),
        funding_events=int(funding_events),
    )


__all__ = ["OutcomeCosts", "estimate_outcome_costs"]
