"""Governor test factory backed by the production composition root."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from config import AppConfig
from config.symbol_context import (
    PortfolioRiskSettings,
    SymbolTradingConfig,
    symbol_config_from_app_config,
)
from governor.alphora_governor import AlphoraGovernor


def build_governor_for_tests(
    *,
    config: AppConfig,
    client: Any,
    symbol_configs: Iterable[SymbolTradingConfig] | None = None,
    portfolio_settings: PortfolioRiskSettings | None = None,
    max_symbols_per_cycle: int = 10,
    **overrides: Any,
) -> AlphoraGovernor:
    """Build a real Governor graph, then replace explicitly requested test seams."""

    profiles = list(symbol_configs or [symbol_config_from_app_config(config)])
    governor = AlphoraGovernor(
        config=config,
        client=client,
        symbol_configs=profiles,
        portfolio_settings=portfolio_settings,
        max_symbols_per_cycle=max_symbols_per_cycle,
    )
    for name, value in overrides.items():
        if not hasattr(governor, name):
            raise AttributeError(f"Unknown Governor test override: {name}")
        setattr(governor, name, value)
    return governor
