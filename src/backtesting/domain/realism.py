from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class FundingRatePoint:
    timestamp: str
    rate: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", str(self.timestamp).strip())
        object.__setattr__(self, "rate", Decimal(str(self.rate)))
        if not self.timestamp:
            raise ValueError("FundingRatePoint exige timestamp")
        if not self.rate.is_finite():
            raise ValueError("FundingRatePoint exige rate finita")


@dataclass(frozen=True, slots=True)
class BacktestFillSettings:
    """Execution assumptions that cannot be inferred from OHLCV alone.

    Defaults preserve broad compatibility while every approximation is exposed in
    diagnostics.  The reliability gate refuses approval when observed participation
    exceeds ``reliable_volume_participation`` or when market impact is disabled.
    """

    max_volume_participation: Decimal = Decimal("0.05")
    reliable_volume_participation: Decimal = Decimal("0.05")
    allow_partial_fills: bool = True
    require_limit_trade_through: bool = True
    assume_limit_maker: bool = True
    intrabar_limit_volume_fraction: Decimal = Decimal("0.25")
    market_impact_bps_at_full_participation: Decimal = Decimal("5")
    maintenance_margin_rate: Decimal = Decimal("0.005")
    liquidation_fee_pct: Decimal = Decimal("0.005")

    def __post_init__(self) -> None:
        for field_name in (
            "max_volume_participation",
            "reliable_volume_participation",
            "intrabar_limit_volume_fraction",
            "market_impact_bps_at_full_participation",
            "maintenance_margin_rate",
            "liquidation_fee_pct",
        ):
            value = Decimal(str(getattr(self, field_name)))
            object.__setattr__(self, field_name, value)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} nao pode ser negativo")
        if self.max_volume_participation <= 0 or self.max_volume_participation > 1:
            raise ValueError("max_volume_participation deve ficar entre 0 e 1")
        if self.reliable_volume_participation > 1:
            raise ValueError("reliable_volume_participation nao pode superar 1")
        if self.reliable_volume_participation > self.max_volume_participation:
            raise ValueError(
                "reliable_volume_participation nao pode superar max_volume_participation"
            )
        if not Decimal("0") < self.intrabar_limit_volume_fraction <= Decimal("1"):
            raise ValueError("intrabar_limit_volume_fraction deve ficar entre 0 e 1")
        if self.maintenance_margin_rate >= 1:
            raise ValueError("maintenance_margin_rate deve ser menor que 1")
        if self.liquidation_fee_pct > 1:
            raise ValueError("liquidation_fee_pct nao pode superar 1")


@dataclass(frozen=True, slots=True)
class BacktestValidationSettings:
    evaluation_mode: str = "IN_SAMPLE"
    require_out_of_sample_for_approval: bool = True
    fail_on_model_training_overlap: bool = True
    purge_candles: int = 0
    embargo_candles: int = 0
    walk_forward_folds: int = 5
    min_profitable_walk_forward_folds: int = 3
    bootstrap_samples: int = 1000
    confidence_level: Decimal = Decimal("0.95")
    random_seed: int = 1729
    parameter_trials: int = 1
    min_probabilistic_sharpe: Decimal = Decimal("0.95")
    min_deflated_sharpe: Decimal = Decimal("0.95")
    max_risk_of_ruin: Decimal = Decimal("0.01")
    risk_of_ruin_horizon_trades: int = 1000
    stress_cost_multipliers: tuple[Decimal, ...] = (
        Decimal("1.0"),
        Decimal("1.5"),
        Decimal("2.0"),
    )
    stress_extra_slippage_bps: tuple[Decimal, ...] = (
        Decimal("0"),
        Decimal("2"),
        Decimal("5"),
    )

    def __post_init__(self) -> None:
        mode = str(self.evaluation_mode or "IN_SAMPLE").upper().strip()
        if mode not in {"IN_SAMPLE", "VALIDATION", "OUT_OF_SAMPLE", "WALK_FORWARD"}:
            raise ValueError(f"evaluation_mode invalido: {mode}")
        object.__setattr__(self, "evaluation_mode", mode)
        object.__setattr__(self, "purge_candles", max(0, int(self.purge_candles)))
        object.__setattr__(self, "embargo_candles", max(0, int(self.embargo_candles)))
        object.__setattr__(self, "walk_forward_folds", max(2, int(self.walk_forward_folds)))
        object.__setattr__(
            self,
            "min_profitable_walk_forward_folds",
            min(
                max(1, int(self.min_profitable_walk_forward_folds)),
                max(2, int(self.walk_forward_folds)),
            ),
        )
        object.__setattr__(self, "bootstrap_samples", max(100, int(self.bootstrap_samples)))
        object.__setattr__(self, "parameter_trials", max(1, int(self.parameter_trials)))
        object.__setattr__(
            self,
            "risk_of_ruin_horizon_trades",
            max(100, int(self.risk_of_ruin_horizon_trades)),
        )
        confidence = Decimal(str(self.confidence_level))
        if not confidence.is_finite() or confidence <= 0 or confidence >= 1:
            raise ValueError("confidence_level deve ficar entre 0 e 1")
        object.__setattr__(self, "confidence_level", confidence)
        object.__setattr__(
            self,
            "min_probabilistic_sharpe",
            Decimal(str(self.min_probabilistic_sharpe)),
        )
        object.__setattr__(
            self,
            "min_deflated_sharpe",
            Decimal(str(self.min_deflated_sharpe)),
        )
        object.__setattr__(
            self,
            "max_risk_of_ruin",
            Decimal(str(self.max_risk_of_ruin)),
        )
        object.__setattr__(
            self,
            "stress_cost_multipliers",
            tuple(Decimal(str(value)) for value in self.stress_cost_multipliers),
        )
        object.__setattr__(
            self,
            "stress_extra_slippage_bps",
            tuple(Decimal(str(value)) for value in self.stress_extra_slippage_bps),
        )
        for field_name in ("min_probabilistic_sharpe", "min_deflated_sharpe"):
            value = getattr(self, field_name)
            if not value.is_finite() or not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{field_name} deve ficar entre 0 e 1")
        if not Decimal(str(self.max_risk_of_ruin)).is_finite() or not (
            Decimal("0") <= self.max_risk_of_ruin <= Decimal("1")
        ):
            raise ValueError("max_risk_of_ruin deve ficar entre 0 e 1")
        if not self.stress_cost_multipliers or any(
            not value.is_finite() or value < 1 for value in self.stress_cost_multipliers
        ):
            raise ValueError("stress_cost_multipliers exige valores finitos >= 1")
        if not self.stress_extra_slippage_bps or any(
            not value.is_finite() or value < 0 for value in self.stress_extra_slippage_bps
        ):
            raise ValueError("stress_extra_slippage_bps exige valores finitos >= 0")


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: str
    balance: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    estimated_open_costs: Decimal
    equity: Decimal
    gross_exposure: Decimal
    reserved_margin: Decimal
    open_positions: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "timestamp": self.timestamp,
            "balance": str(self.balance),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "estimated_open_costs": str(self.estimated_open_costs),
            "equity": str(self.equity),
            "gross_exposure": str(self.gross_exposure),
            "reserved_margin": str(self.reserved_margin),
            "open_positions": self.open_positions,
        }
