from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from config.smart_settings import SmartExecutionSettings
from operational_module_modes import OperationalModuleMode, parse_module_mode

SMART_EXECUTION_VERSION = "1.0.0"


@dataclass(frozen=True)
class SmartExecutionInput:
    symbol: str
    side: str
    entry_price: Any
    current_price: Any
    mark_price: Any = None
    bid: Any = None
    ask: Any = None
    estimated_spread_bps: Any = None
    estimated_slippage_bps: Any = None
    limit_entry_enabled: bool | None = None
    limit_entry_timeout_seconds: Any = None
    limit_entry_max_price_drift_bps: Any = None
    limit_entry_fallback_to_market: bool | None = None
    limit_entry_post_only: bool | None = None
    market_entry_require_economic_gate: bool = True
    planned_exit: dict[str, Any] | None = None
    notional: Any = None
    mode: OperationalModuleMode | str = OperationalModuleMode.OFF
    market_data_age_seconds: Any = None
    market_data_stale: bool = False
    settings: SmartExecutionSettings | None = None


@dataclass(frozen=True)
class SmartExecutionDecision:
    mode: OperationalModuleMode
    entry_order_type: str
    limit_price: Decimal | None
    time_in_force: str | None
    post_only: bool
    fallback_allowed: bool
    max_price_drift_bps: Decimal
    blocked: bool
    reason: str
    diagnostics: dict[str, Any] = field(default_factory=dict)
    affected_result: bool = False

    @property
    def smart_execution_used(self) -> bool:
        return self.mode is not OperationalModuleMode.OFF

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "entry_order_type": self.entry_order_type,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "time_in_force": self.time_in_force,
            "post_only": self.post_only,
            "fallback_allowed": self.fallback_allowed,
            "max_price_drift_bps": str(self.max_price_drift_bps),
            "blocked": self.blocked,
            "reason": self.reason,
            "affected_result": self.affected_result,
            "smart_execution_used": self.smart_execution_used,
            "diagnostics": dict(self.diagnostics),
        }


class SmartExecutionEngine:
    def __init__(self, settings: SmartExecutionSettings | None = None):
        self.settings = settings or SmartExecutionSettings()

    def evaluate(
        self,
        execution_input: SmartExecutionInput,
    ) -> SmartExecutionDecision:
        mode = _normalize_mode(execution_input.mode)
        settings = execution_input.settings or self.settings
        diagnostics = _diagnostics(execution_input, mode, settings=settings)
        max_drift = (
            _decimal_or_none(execution_input.limit_entry_max_price_drift_bps)
            or settings.max_price_drift_bps
        )
        limit_entry_enabled = _bool_or_default(
            execution_input.limit_entry_enabled,
            settings.limit_entry_enabled,
        )
        limit_entry_fallback_to_market = _bool_or_default(
            execution_input.limit_entry_fallback_to_market,
            settings.limit_entry_fallback_to_market,
        )
        limit_entry_post_only = _bool_or_default(
            execution_input.limit_entry_post_only,
            settings.limit_entry_post_only,
        )

        if mode is OperationalModuleMode.OFF:
            return _decision(
                mode=mode,
                entry_order_type="MARKET",
                reason="SMART_EXECUTION_OFF",
                max_price_drift_bps=max_drift,
                diagnostics=diagnostics,
            )

        current_price = _decimal_or_none(execution_input.current_price)
        entry_price = _decimal_or_none(execution_input.entry_price)
        if current_price is None or current_price <= 0 or entry_price is None or entry_price <= 0:
            blocked = mode.can_affect_execution
            return _decision(
                mode=mode,
                entry_order_type="MARKET",
                reason="SMART_EXECUTION_INVALID_PRICE",
                max_price_drift_bps=max_drift,
                diagnostics=diagnostics,
                blocked=blocked,
                affected_result=blocked,
            )

        if _stale(execution_input, settings=settings):
            blocked = mode.can_affect_execution
            return _decision(
                mode=mode,
                entry_order_type="MARKET",
                reason="SMART_EXECUTION_MARKET_DATA_STALE",
                max_price_drift_bps=max_drift,
                diagnostics=diagnostics,
                blocked=blocked,
                affected_result=blocked,
            )

        spread_bps = _spread_bps(execution_input)
        diagnostics["spread_bps"] = str(spread_bps) if spread_bps is not None else None
        if spread_bps is not None and spread_bps > settings.max_spread_bps:
            blocked = mode.can_affect_execution
            if limit_entry_enabled:
                diagnostics["limit_fill_status"] = "LIMIT_REJECTED_BY_SPREAD"
            return _decision(
                mode=mode,
                entry_order_type="MARKET",
                reason="SMART_EXECUTION_SPREAD_TOO_HIGH",
                max_price_drift_bps=max_drift,
                diagnostics=diagnostics,
                blocked=blocked,
                affected_result=blocked,
            )

        drift_bps = _price_drift_bps(
            current_price=current_price,
            reference_price=entry_price,
        )
        diagnostics["price_drift_reference"] = "entry_price"
        diagnostics["price_drift_bps"] = str(drift_bps) if drift_bps is not None else None
        if drift_bps is not None and drift_bps > max_drift:
            blocked = mode.can_affect_execution
            if limit_entry_enabled:
                diagnostics["limit_fill_status"] = "LIMIT_REJECTED_BY_DRIFT"
            return _decision(
                mode=mode,
                entry_order_type="MARKET",
                reason="SMART_EXECUTION_PRICE_DRIFT_TOO_HIGH",
                max_price_drift_bps=max_drift,
                diagnostics=diagnostics,
                blocked=blocked,
                affected_result=blocked,
            )

        recommended_type = "MARKET"
        limit_price = None
        time_in_force = None
        if limit_entry_enabled:
            limit_price = _limit_price(execution_input, fallback_price=current_price)
            if limit_price is not None and limit_price > 0:
                recommended_type = "LIMIT"
                time_in_force = "GTX" if limit_entry_post_only else "GTC"
            elif limit_entry_fallback_to_market:
                diagnostics["limit_fallback_reason"] = "LIMIT_PRICE_UNAVAILABLE"
            else:
                blocked = mode.can_affect_execution
                return _decision(
                    mode=mode,
                    entry_order_type="MARKET",
                    reason="SMART_EXECUTION_LIMIT_PRICE_UNAVAILABLE",
                    max_price_drift_bps=max_drift,
                    diagnostics=diagnostics,
                    blocked=blocked,
                    affected_result=blocked,
                )

        diagnostics["recommended_entry_order_type"] = recommended_type
        diagnostics["limit_price"] = str(limit_price) if limit_price is not None else None

        if mode in {OperationalModuleMode.OBSERVE, OperationalModuleMode.SHADOW}:
            reason = (
                "SMART_EXECUTION_OBSERVED"
                if mode is OperationalModuleMode.OBSERVE
                else "SMART_EXECUTION_SHADOW"
            )
            return _decision(
                mode=mode,
                entry_order_type="MARKET",
                limit_price=None,
                time_in_force=None,
                post_only=False,
                reason=reason,
                max_price_drift_bps=max_drift,
                diagnostics=diagnostics,
                affected_result=False,
            )

        reason = "SMART_EXECUTION_MARKET_SELECTED"
        affected = False
        if recommended_type == "LIMIT":
            reason = "SMART_EXECUTION_LIMIT_SELECTED"
            affected = True
        return _decision(
            mode=mode,
            entry_order_type=recommended_type,
            limit_price=limit_price,
            time_in_force=time_in_force,
            post_only=bool(limit_entry_post_only and recommended_type == "LIMIT"),
            fallback_allowed=bool(limit_entry_fallback_to_market),
            reason=reason,
            max_price_drift_bps=max_drift,
            diagnostics=diagnostics,
            affected_result=affected,
        )


def _decision(
    *,
    mode: OperationalModuleMode,
    entry_order_type: str,
    reason: str,
    max_price_drift_bps: Decimal,
    diagnostics: dict[str, Any],
    limit_price: Decimal | None = None,
    time_in_force: str | None = None,
    post_only: bool = False,
    fallback_allowed: bool = False,
    blocked: bool = False,
    affected_result: bool = False,
) -> SmartExecutionDecision:
    return SmartExecutionDecision(
        mode=mode,
        entry_order_type=entry_order_type,
        limit_price=limit_price,
        time_in_force=time_in_force,
        post_only=post_only,
        fallback_allowed=fallback_allowed,
        max_price_drift_bps=max_price_drift_bps,
        blocked=blocked,
        reason=reason,
        affected_result=bool(affected_result),
        diagnostics=diagnostics,
    )


def _diagnostics(
    execution_input: SmartExecutionInput,
    mode: OperationalModuleMode,
    *,
    settings: SmartExecutionSettings,
) -> dict[str, Any]:
    limit_entry_enabled = _bool_or_default(
        execution_input.limit_entry_enabled,
        settings.limit_entry_enabled,
    )
    limit_entry_fallback_to_market = _bool_or_default(
        execution_input.limit_entry_fallback_to_market,
        settings.limit_entry_fallback_to_market,
    )
    limit_entry_post_only = _bool_or_default(
        execution_input.limit_entry_post_only,
        settings.limit_entry_post_only,
    )
    limit_timeout = (
        _decimal_or_none(execution_input.limit_entry_timeout_seconds)
        or settings.limit_entry_timeout_seconds
    )
    return {
        "version": SMART_EXECUTION_VERSION,
        "mode": mode.value,
        "settings": settings.to_dict(),
        "symbol": str(execution_input.symbol or "").upper().strip(),
        "side": str(execution_input.side or "").upper().strip(),
        "entry_price": str(execution_input.entry_price),
        "current_price": str(execution_input.current_price),
        "mark_price": (
            str(execution_input.mark_price) if execution_input.mark_price is not None else None
        ),
        "bid": str(execution_input.bid) if execution_input.bid is not None else None,
        "ask": str(execution_input.ask) if execution_input.ask is not None else None,
        "estimated_spread_bps": (
            str(execution_input.estimated_spread_bps)
            if execution_input.estimated_spread_bps is not None
            else None
        ),
        "estimated_slippage_bps": (
            str(execution_input.estimated_slippage_bps)
            if execution_input.estimated_slippage_bps is not None
            else None
        ),
        "limit_entry_enabled": bool(limit_entry_enabled),
        "limit_entry_timeout_seconds": str(limit_timeout),
        "limit_entry_fallback_to_market": bool(limit_entry_fallback_to_market),
        "limit_entry_post_only": bool(limit_entry_post_only),
        "max_spread_bps": str(settings.max_spread_bps),
        "max_price_drift_bps": str(settings.max_price_drift_bps),
        "max_market_data_age_seconds": str(settings.max_market_data_age_seconds),
        "market_data_age_seconds": (
            str(execution_input.market_data_age_seconds)
            if execution_input.market_data_age_seconds is not None
            else None
        ),
        "market_data_stale": bool(execution_input.market_data_stale),
    }


def _spread_bps(execution_input: SmartExecutionInput) -> Decimal | None:
    bid = _decimal_or_none(execution_input.bid)
    ask = _decimal_or_none(execution_input.ask)
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / Decimal("2")
        if mid > 0:
            return (ask - bid) / mid * Decimal("10000")
    return _decimal_or_none(execution_input.estimated_spread_bps)


def _price_drift_bps(
    *,
    current_price: Decimal,
    reference_price: Decimal,
) -> Decimal | None:
    if current_price <= 0 or reference_price <= 0:
        return None
    return abs(current_price - reference_price) / reference_price * Decimal("10000")


def _limit_price(
    execution_input: SmartExecutionInput,
    *,
    fallback_price: Decimal,
) -> Decimal | None:
    side = str(execution_input.side or "").upper().strip()
    bid = _decimal_or_none(execution_input.bid)
    ask = _decimal_or_none(execution_input.ask)
    if side in {"OPEN_LONG", "LONG", "BUY"} and bid is not None:
        return bid
    if side in {"OPEN_SHORT", "SHORT", "SELL"} and ask is not None:
        return ask
    return fallback_price


def _stale(
    execution_input: SmartExecutionInput,
    *,
    settings: SmartExecutionSettings,
) -> bool:
    if bool(execution_input.market_data_stale):
        return True
    age = _decimal_or_none(execution_input.market_data_age_seconds)
    return age is not None and age > settings.max_market_data_age_seconds


def _normalize_mode(value: OperationalModuleMode | str) -> OperationalModuleMode:
    return parse_module_mode(value, OperationalModuleMode, name="smart_execution_mode")  # type: ignore[return-value]


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not decimal.is_finite():
        return None
    return decimal


def _bool_or_default(value: bool | None, default: bool) -> bool:
    if value is None:
        return bool(default)
    return bool(value)


__all__ = [
    "SmartExecutionDecision",
    "SmartExecutionEngine",
    "SmartExecutionInput",
    "SmartExecutionSettings",
]
