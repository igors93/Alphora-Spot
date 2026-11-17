from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

SPREAD_BPS_SEMANTICS = "FULL_BID_ASK_SPREAD"
SLIPPAGE_BPS_SEMANTICS = "PER_MARKET_FILL"


@dataclass(frozen=True)
class CostBreakdown:
    entry_fee: Decimal
    exit_fee: Decimal
    slippage_cost: Decimal
    spread_cost: Decimal
    funding_cost: Decimal
    liquidation_fee: Decimal = Decimal("0")

    @property
    def fees(self) -> Decimal:
        return self.entry_fee + self.exit_fee

    @property
    def total_cost(self) -> Decimal:
        return (
            self.fees
            + self.slippage_cost
            + self.spread_cost
            + self.funding_cost
            + self.liquidation_fee
        )


@dataclass(frozen=True)
class CostModel:
    maker_fee_pct: Decimal = Decimal("0.0002")
    taker_fee_pct: Decimal = Decimal("0.0005")
    slippage_bps: Decimal = Decimal("2")
    spread_bps: Decimal = Decimal("1")
    funding_enabled: bool = True
    default_funding_rate: Decimal = Decimal("0")
    model_version: str = "1.1.0"
    spread_bps_semantics: str = SPREAD_BPS_SEMANTICS
    slippage_bps_semantics: str = SLIPPAGE_BPS_SEMANTICS

    def __post_init__(self) -> None:
        for field_name in (
            "maker_fee_pct",
            "taker_fee_pct",
            "slippage_bps",
            "spread_bps",
            "default_funding_rate",
        ):
            object.__setattr__(self, field_name, Decimal(str(getattr(self, field_name))))
        for field_name in ("maker_fee_pct", "taker_fee_pct", "slippage_bps", "spread_bps"):
            if not getattr(self, field_name).is_finite() or getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} nao pode ser negativo")
        if self.maker_fee_pct > 1 or self.taker_fee_pct > 1:
            raise ValueError("Taxas maker/taker nao podem superar 100%")
        if not self.default_funding_rate.is_finite():
            raise ValueError("default_funding_rate precisa ser finita")

    @classmethod
    def from_numbers(
        cls,
        *,
        maker_fee_pct: float | str | Decimal = Decimal("0.0002"),
        taker_fee_pct: float | str | Decimal = Decimal("0.0005"),
        slippage_bps: float | str | Decimal = Decimal("2"),
        spread_bps: float | str | Decimal = Decimal("1"),
        funding_enabled: bool = True,
        default_funding_rate: float | str | Decimal = Decimal("0"),
    ) -> "CostModel":
        return cls(
            maker_fee_pct=Decimal(str(maker_fee_pct)),
            taker_fee_pct=Decimal(str(taker_fee_pct)),
            slippage_bps=Decimal(str(slippage_bps)),
            spread_bps=Decimal(str(spread_bps)),
            funding_enabled=funding_enabled,
            default_funding_rate=Decimal(str(default_funding_rate)),
        )

    def calculate_entry_fee(self, notional: Decimal, *, maker: bool = False) -> Decimal:
        return notional * self._fee_pct(maker=maker)

    def calculate_exit_fee(self, notional: Decimal, *, maker: bool = False) -> Decimal:
        return notional * self._fee_pct(maker=maker)

    def calculate_slippage_cost(self, notional: Decimal, *, fills: int = 2) -> Decimal:
        return notional * _bps_to_pct(self.slippage_bps) * Decimal(str(fills))

    def calculate_spread_cost(self, notional: Decimal, *, fills: int = 2) -> Decimal:
        return notional * _bps_to_pct(self.spread_bps) * Decimal(str(fills)) / Decimal("2")

    def calculate_funding_cost(
        self,
        notional: Decimal,
        *,
        holding_hours: Decimal = Decimal("0"),
        direction: str | Any | None = None,
        entry_time: str | None = None,
        exit_time: str | None = None,
        rate_points: Sequence[Any] | None = None,
    ) -> Decimal:
        if not self.funding_enabled:
            return Decimal("0")
        side_multiplier = _funding_side_multiplier(direction)
        entry_dt = _parse_timestamp(entry_time)
        exit_dt = _parse_timestamp(exit_time)
        if entry_dt is not None and exit_dt is not None and exit_dt > entry_dt:
            points = list(rate_points or ())
            if points:
                total = Decimal("0")
                for point in points:
                    point_dt = _parse_timestamp(getattr(point, "timestamp", None))
                    if point_dt is None or not (entry_dt < point_dt <= exit_dt):
                        continue
                    total += notional * Decimal(str(point.rate)) * side_multiplier
                return total
            if self.default_funding_rate == 0:
                return Decimal("0")
            periods = _funding_boundaries_crossed(entry_dt, exit_dt)
            return notional * self.default_funding_rate * Decimal(str(periods)) * side_multiplier
        if self.default_funding_rate == 0:
            return Decimal("0")
        funding_periods = holding_hours / Decimal("8")
        return notional * self.default_funding_rate * funding_periods * side_multiplier

    def calculate_total_cost(
        self,
        *,
        entry_notional: Decimal,
        exit_notional: Decimal,
        holding_hours: Decimal = Decimal("0"),
        entry_maker: bool = False,
        exit_maker: bool = False,
        entry_market_impact: bool = True,
        exit_market_impact: bool = True,
        direction: str | Any | None = None,
        entry_time: str | None = None,
        exit_time: str | None = None,
        funding_rate_points: Sequence[Any] | None = None,
        entry_extra_slippage_bps: Decimal = Decimal("0"),
        exit_extra_slippage_bps: Decimal = Decimal("0"),
        liquidation_fee: Decimal = Decimal("0"),
    ) -> CostBreakdown:
        slippage_cost = Decimal("0")
        spread_cost = Decimal("0")
        if entry_market_impact:
            slippage_cost += entry_notional * _bps_to_pct(
                self.slippage_bps + Decimal(str(entry_extra_slippage_bps))
            )
            spread_cost += entry_notional * _bps_to_pct(self.spread_bps) / Decimal("2")
        if exit_market_impact:
            slippage_cost += exit_notional * _bps_to_pct(
                self.slippage_bps + Decimal(str(exit_extra_slippage_bps))
            )
            spread_cost += exit_notional * _bps_to_pct(self.spread_bps) / Decimal("2")
        average_notional = (entry_notional + exit_notional) / Decimal("2")
        return CostBreakdown(
            entry_fee=self.calculate_entry_fee(entry_notional, maker=entry_maker),
            exit_fee=self.calculate_exit_fee(exit_notional, maker=exit_maker),
            slippage_cost=slippage_cost,
            spread_cost=spread_cost,
            funding_cost=self.calculate_funding_cost(
                average_notional,
                holding_hours=holding_hours,
                direction=direction,
                entry_time=entry_time,
                exit_time=exit_time,
                rate_points=funding_rate_points,
            ),
            liquidation_fee=Decimal(str(liquidation_fee)),
        )

    def calculate_net_pnl(
        self,
        *,
        gross_pnl: Decimal,
        entry_notional: Decimal,
        exit_notional: Decimal,
        holding_hours: Decimal = Decimal("0"),
        entry_maker: bool = False,
        exit_maker: bool = False,
        entry_market_impact: bool = True,
        exit_market_impact: bool = True,
        direction: str | Any | None = None,
        entry_time: str | None = None,
        exit_time: str | None = None,
        funding_rate_points: Sequence[Any] | None = None,
        entry_extra_slippage_bps: Decimal = Decimal("0"),
        exit_extra_slippage_bps: Decimal = Decimal("0"),
        liquidation_fee: Decimal = Decimal("0"),
    ) -> tuple[Decimal, CostBreakdown]:
        costs = self.calculate_total_cost(
            entry_notional=entry_notional,
            exit_notional=exit_notional,
            holding_hours=holding_hours,
            entry_maker=entry_maker,
            exit_maker=exit_maker,
            entry_market_impact=entry_market_impact,
            exit_market_impact=exit_market_impact,
            direction=direction,
            entry_time=entry_time,
            exit_time=exit_time,
            funding_rate_points=funding_rate_points,
            entry_extra_slippage_bps=entry_extra_slippage_bps,
            exit_extra_slippage_bps=exit_extra_slippage_bps,
            liquidation_fee=liquidation_fee,
        )
        return gross_pnl - costs.total_cost, costs

    def _fee_pct(self, *, maker: bool) -> Decimal:
        return self.maker_fee_pct if maker else self.taker_fee_pct


def _bps_to_pct(value: Decimal) -> Decimal:
    return value / Decimal("10000")


def _funding_side_multiplier(direction: str | Any | None) -> Decimal:
    value = getattr(direction, "value", direction)
    normalized = str(value or "LONG").upper().strip()
    return Decimal("-1") if normalized == "SHORT" else Decimal("1")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _funding_boundaries_crossed(entry: datetime, exit_: datetime) -> int:
    # Binance USD-M funding is normally exchanged at 00:00, 08:00 and 16:00 UTC.
    period_seconds = 8 * 60 * 60
    entry_seconds = int(entry.timestamp())
    exit_seconds = int(exit_.timestamp())
    first_boundary = ((entry_seconds // period_seconds) + 1) * period_seconds
    if first_boundary > exit_seconds:
        return 0
    return ((exit_seconds - first_boundary) // period_seconds) + 1
