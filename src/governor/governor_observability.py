import time
from typing import Any

from market_data.snapshot_provider import wire_names


def monotonic_now() -> float:
    return time.perf_counter()


def elapsed_ms(start: float, end: float | None = None) -> float:
    final = time.perf_counter() if end is None else end
    return round(max(0.0, final - start) * 1000.0, 3)


def build_cycle_metrics_payload(
    *,
    cycle_started_at: float,
    cycle_duration_ms: float,
    due_symbols: list[str],
    dirty_symbols: list[str],
    open_symbols: set[str],
    shock_events: list[dict[str, Any]],
    symbol_timings: list[dict[str, Any]],
    fast_lane_symbols: list[str] | None = None,
    request_budget_summary: dict[str, Any] | None = None,
    defensive_mode_summary: dict[str, Any] | None = None,
    defensive_blocks: list[dict[str, Any]] | None = None,
    market_health: dict[str, Any] | None = None,
    snapshot_diagnostics: dict[str, Any] | None = None,
    rest_weight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_health = market_health or {}
    slowest = _slowest_symbol(symbol_timings)
    fast_lane_symbols = fast_lane_symbols or []
    request_budget_summary = request_budget_summary or {"enabled": False}
    defensive_mode_summary = defensive_mode_summary or {
        "enabled": False,
        "mode": "DISABLED",
    }
    defensive_blocks = defensive_blocks or []
    defensive_blocked_symbols = sorted(
        {
            str(item.get("symbol", "")).upper().strip()
            for item in defensive_blocks
            if str(item.get("symbol", "")).strip()
        }
    )

    return {
        "cycle_started_at": cycle_started_at,
        "cycle_duration_ms": cycle_duration_ms,
        "evaluated_symbols": len(symbol_timings),
        "due_symbols_count": len(due_symbols),
        "dirty_symbols_count": len(dirty_symbols),
        "open_symbols_count": len(open_symbols),
        "shock_events_count": len(shock_events),
        "fast_lane_symbols_count": len(fast_lane_symbols),
        "fast_lane_symbols": fast_lane_symbols,
        "request_budget": request_budget_summary,
        "defensive_mode": defensive_mode_summary,
        "defensive_blocks_count": len(defensive_blocks),
        "defensive_blocked_symbols": defensive_blocked_symbols,
        "slowest_symbol": slowest.get("symbol", ""),
        "slowest_symbol_duration_ms": slowest.get("duration_ms", 0.0),
        "symbol_timings": symbol_timings,
        "market_ws_enabled": bool(market_health.get("enabled", False)),
        "market_ws_running": bool(market_health.get("running", False)),
        "market_ws_connected": bool(market_health.get("connected", False)),
        "market_ws_known_prices": int(market_health.get("known_prices", 0) or 0),
        "market_ws_stale_symbols": int(market_health.get("stale_symbols", 0) or 0),
        "market_ws_largest_price_age_seconds": float(
            market_health.get("largest_price_age_seconds", 0.0) or 0.0
        ),
        "market_ws_pending_shock_events": int(
            market_health.get("market_shock_pending_events", 0) or 0
        ),
        wire_names.PAYLOAD_KEY: snapshot_diagnostics or {"enabled": False},
        "rest_weight": rest_weight or {"enabled": False},
    }


def _slowest_symbol(symbol_timings: list[dict[str, Any]]) -> dict[str, Any]:
    if not symbol_timings:
        return {}

    return max(
        symbol_timings,
        key=lambda item: float(item.get("duration_ms", 0.0) or 0.0),
    )
