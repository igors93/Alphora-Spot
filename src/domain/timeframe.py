from __future__ import annotations

from trading_cadence import MIN_REAL_SIGNAL_TIMEFRAME

SUPPORTED_TIMEFRAMES: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

TIMEFRAME_LABELS: dict[str, str] = {
    "1m": "1 minuto",
    "3m": "3 minutos",
    "5m": "5 minutos",
    "15m": "15 minutos",
    "30m": "30 minutos",
    "1h": "1 hora",
    "2h": "2 horas",
    "4h": "4 horas",
    "6h": "6 horas",
    "8h": "8 horas",
    "12h": "12 horas",
    "1d": "1 dia",
}


def normalize_timeframe(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SUPPORTED_TIMEFRAMES:
        raise ValueError(
            f"Timeframe invalido: {value!r}. Suportados: {', '.join(SUPPORTED_TIMEFRAMES)}"
        )
    return normalized


def is_supported_timeframe(value: str) -> bool:
    return str(value or "").strip().lower() in SUPPORTED_TIMEFRAMES


def timeframe_duration_ms(timeframe: str) -> int:
    return SUPPORTED_TIMEFRAMES[normalize_timeframe(timeframe)]


def expected_open_time_ms(timestamp_ms: int, timeframe: str) -> int:
    """Floor timestamp to the candle open boundary for the given timeframe."""
    duration = timeframe_duration_ms(timeframe)
    return (int(timestamp_ms) // duration) * duration


def expected_close_time_ms(open_time_ms: int, timeframe: str) -> int:
    """close_time = open_time + duration - 1ms (Binance convention)."""
    return int(open_time_ms) + timeframe_duration_ms(timeframe) - 1


def timeframe_label(timeframe: str) -> str:
    return TIMEFRAME_LABELS[normalize_timeframe(timeframe)]


def approximate_horizon_label(timeframe: str, horizon_candles: int) -> str:
    """Human-readable approximation: e.g. '5m x 6 = ~30 minutos'."""
    duration_ms = timeframe_duration_ms(timeframe)
    total_ms = duration_ms * int(horizon_candles)
    total_minutes = total_ms // 60_000
    if total_minutes < 60:
        return f"~{total_minutes} minutos"
    total_hours = total_minutes // 60
    if total_hours < 24:
        return f"~{total_hours} horas"
    return f"~{total_hours // 24} dias"


def is_signal_timeframe_allowed_for_real(timeframe: str) -> bool:
    """Return whether ``timeframe`` is slow enough to drive real-money entries."""

    if not is_supported_timeframe(timeframe):
        return False
    return timeframe_duration_ms(timeframe) >= timeframe_duration_ms(MIN_REAL_SIGNAL_TIMEFRAME)
