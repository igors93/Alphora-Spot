from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DefensiveModeEvaluation:
    mode: str
    reason: str
    severity: str
    shadow_only: bool
    would_block_new_entries: bool
    would_reduce_new_entries: bool
    would_prioritize_protection: bool
    would_allow_closing: bool
    would_allow_stop_take_adjustment: bool
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "severity": self.severity,
            "shadow_only": self.shadow_only,
            "would_block_new_entries": self.would_block_new_entries,
            "would_reduce_new_entries": self.would_reduce_new_entries,
            "would_prioritize_protection": self.would_prioritize_protection,
            "would_allow_closing": self.would_allow_closing,
            "would_allow_stop_take_adjustment": self.would_allow_stop_take_adjustment,
            "diagnostics": self.diagnostics,
        }


class DefensiveModeEvaluator:
    def __init__(
        self,
        *,
        shadow_only: bool = True,
        min_shock_symbols: int = 2,
        max_stale_symbols: int = 0,
        max_price_age_seconds: float = 5.0,
        budget_pressure_enabled: bool = True,
    ):
        self.shadow_only = bool(shadow_only)
        self.min_shock_symbols = max(1, int(min_shock_symbols))
        self.max_stale_symbols = max(0, int(max_stale_symbols))
        self.max_price_age_seconds = max(0.0, float(max_price_age_seconds))
        self.budget_pressure_enabled = bool(budget_pressure_enabled)

    def evaluate(
        self,
        *,
        shock_events: list[dict[str, Any]],
        fast_lane_symbols: list[str] | set[str],
        market_health: dict[str, Any],
        request_budget_summary: dict[str, Any],
        open_symbols: set[str],
        dirty_symbols: list[str] | set[str],
    ) -> DefensiveModeEvaluation:
        shock_symbols = _unique_symbols_from_events(shock_events)
        fast_lane = _unique_symbols(fast_lane_symbols)
        dirty = _unique_symbols(dirty_symbols)

        market_ws_enabled = bool(market_health.get("enabled", False))
        market_ws_connected = bool(market_health.get("connected", False))
        stale_symbols = int(market_health.get("stale_symbols", 0) or 0)
        largest_price_age_seconds = float(
            market_health.get("largest_price_age_seconds", 0.0) or 0.0
        )

        data_unstable = market_ws_enabled and (
            not market_ws_connected
            or stale_symbols > self.max_stale_symbols
            or (
                self.max_price_age_seconds > 0
                and largest_price_age_seconds > self.max_price_age_seconds
            )
        )

        denied_heavy_checks = int(request_budget_summary.get("denied_heavy_checks", 0) or 0)
        budget_pressure = (
            self.budget_pressure_enabled
            and bool(request_budget_summary.get("enabled", False))
            and denied_heavy_checks > 0
        )

        diagnostics = {
            "shock_symbols": sorted(shock_symbols),
            "shock_symbols_count": len(shock_symbols),
            "fast_lane_symbols": sorted(fast_lane),
            "fast_lane_symbols_count": len(fast_lane),
            "open_symbols_count": len(open_symbols),
            "dirty_symbols_count": len(dirty),
            "market_ws_enabled": market_ws_enabled,
            "market_ws_connected": market_ws_connected,
            "stale_symbols": stale_symbols,
            "largest_price_age_seconds": largest_price_age_seconds,
            "request_budget_enabled": bool(request_budget_summary.get("enabled", False)),
            "denied_heavy_checks": denied_heavy_checks,
            "data_unstable": data_unstable,
            "budget_pressure": budget_pressure,
            "safety_note": (
                "Fase 10F: Defensive Mode esta em shadow. "
                "Nao bloqueia trades, nao fecha posicoes e nao altera ordens."
            ),
        }

        if data_unstable and shock_symbols:
            return self._evaluation(
                mode="DEFENSIVE_SHADOW",
                reason="SHOCK_AND_DATA_UNSTABLE",
                severity="CRITICAL",
                would_block_new_entries=True,
                would_reduce_new_entries=True,
                would_prioritize_protection=True,
                diagnostics=diagnostics,
            )

        if data_unstable:
            return self._evaluation(
                mode="DATA_UNSTABLE",
                reason="MARKET_DATA_UNSTABLE",
                severity="HIGH",
                would_block_new_entries=True,
                would_reduce_new_entries=True,
                would_prioritize_protection=True,
                diagnostics=diagnostics,
            )

        if len(shock_symbols) >= self.min_shock_symbols or len(fast_lane) >= self.min_shock_symbols:
            return self._evaluation(
                mode="MARKET_SHOCK",
                reason="MULTI_SYMBOL_SHOCK_DETECTED",
                severity="HIGH",
                would_block_new_entries=True,
                would_reduce_new_entries=True,
                would_prioritize_protection=True,
                diagnostics=diagnostics,
            )

        if shock_symbols:
            return self._evaluation(
                mode="VOLATILE",
                reason="SINGLE_SYMBOL_SHOCK_DETECTED",
                severity="MEDIUM",
                would_block_new_entries=False,
                would_reduce_new_entries=True,
                would_prioritize_protection=True,
                diagnostics=diagnostics,
            )

        if budget_pressure:
            return self._evaluation(
                mode="REQUEST_BUDGET_PRESSURE",
                reason="HEAVY_CHECK_BUDGET_PRESSURE",
                severity="MEDIUM",
                would_block_new_entries=False,
                would_reduce_new_entries=True,
                would_prioritize_protection=False,
                diagnostics=diagnostics,
            )

        return self._evaluation(
            mode="NORMAL",
            reason="NO_DEFENSIVE_CONDITION",
            severity="LOW",
            would_block_new_entries=False,
            would_reduce_new_entries=False,
            would_prioritize_protection=False,
            diagnostics=diagnostics,
        )

    def _evaluation(
        self,
        *,
        mode: str,
        reason: str,
        severity: str,
        would_block_new_entries: bool,
        would_reduce_new_entries: bool,
        would_prioritize_protection: bool,
        diagnostics: dict[str, Any],
    ) -> DefensiveModeEvaluation:
        return DefensiveModeEvaluation(
            mode=mode,
            reason=reason,
            severity=severity,
            shadow_only=self.shadow_only,
            would_block_new_entries=would_block_new_entries,
            would_reduce_new_entries=would_reduce_new_entries,
            would_prioritize_protection=would_prioritize_protection,
            would_allow_closing=True,
            would_allow_stop_take_adjustment=True,
            diagnostics=diagnostics,
        )


def _unique_symbols_from_events(events: list[dict[str, Any]]) -> set[str]:
    symbols = set()
    for event in events:
        symbol = str(event.get("symbol", "")).upper().strip()
        if symbol:
            symbols.add(symbol)
    return symbols


def _unique_symbols(symbols: list[str] | set[str]) -> set[str]:
    return {str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()}
