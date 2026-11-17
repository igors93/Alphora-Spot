"""Translate module paths saved with the former ``alphora`` namespace."""

TOP_LEVEL_NAMES = frozenset(
    {
        "backtesting",
        "binance",
        "config",
        "desktop",
        "engine",
        "futures",
        "governor",
        "indicators",
        "market_data",
        "module_paths",
        "observability",
        "operational_module_modes",
        "ops",
        "research",
        "resilience",
        "scanner",
        "strategies",
        "trading",
        "trading_cadence",
        "utils",
    }
)


def qualify_module_path(path: str) -> str:
    """Return the current top-level path, accepting saved ``alphora.*`` paths.

    GUI settings, symbol profiles, backtest requests and ``.env`` files written
    before the move may still say ``alphora.strategies.futures...``.
    """

    text = str(path or "").strip()
    prefix, separator, remainder = text.partition(".")
    if prefix == "alphora" and separator and remainder.split(".", 1)[0] in TOP_LEVEL_NAMES:
        return remainder
    return text
