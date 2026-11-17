"""Defaults of probabilistic AI training, shared by the library, the CLI and the desktop.

A label event must be larger than the round-trip cost: a move smaller than fees, spread and
slippage is a net loss even when the direction is right.
"""

from __future__ import annotations

DEFAULT_HORIZON_CANDLES = 8
DEFAULT_THRESHOLD_BPS = 40.0
DEFAULT_ESTIMATED_ROUND_TRIP_COST_BPS = 15.0
DEFAULT_MIN_TRAINING_ROWS = 200


def validate_label_threshold(threshold_bps: float, round_trip_cost_bps: float) -> None:
    if float(threshold_bps) <= float(round_trip_cost_bps):
        raise ValueError(
            f"threshold_bps ({float(threshold_bps):g}) precisa ser maior que o custo de ida e "
            f"volta ({float(round_trip_cost_bps):g} bps): um evento menor que o custo e prejuizo "
            "liquido mesmo com a direcao certa"
        )


__all__ = [
    "DEFAULT_ESTIMATED_ROUND_TRIP_COST_BPS",
    "DEFAULT_HORIZON_CANDLES",
    "DEFAULT_MIN_TRAINING_ROWS",
    "DEFAULT_THRESHOLD_BPS",
    "validate_label_threshold",
]
