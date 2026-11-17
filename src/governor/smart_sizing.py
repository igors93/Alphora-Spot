from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from config.smart_settings import (
    FULL_SIZE_MULTIPLIER,
    SmartSizingMinNotionalPolicy,
    SmartSizingSettings,
)
from operational_module_modes import SmartSizingMode, parse_module_mode

SMART_SIZING_VERSION = "1.0.0"


@dataclass(frozen=True)
class SmartSizingInput:
    symbol: str
    side: str
    base_notional_usdt: Any
    entry_price: Any
    planned_exit: dict[str, Any] | None = None
    strategy_score: Any = None
    strategy_priority: str | None = None
    reward_risk_liquido: Any = None
    cost_to_reward_ratio: Any = None
    risk_abs: Any = None
    reward_abs: Any = None
    max_exposure_usdt: Any = None
    max_daily_loss_usdt: Any = None
    consecutive_losses: int = 0
    current_drawdown: Any = None
    # When True, a missing/invalid current_drawdown means "unknown", not "zero":
    # REDUCE_ONLY/ACTIVE block the entry instead of sizing it at full size.
    require_current_drawdown: bool = False
    drawdown_diagnostics: dict[str, Any] | None = None
    symbol_health: str | None = None
    portfolio_context: dict[str, Any] | None = None
    mode: SmartSizingMode | str = SmartSizingMode.OFF
    min_required_notional_usdt: Any = None
    settings: SmartSizingSettings | None = None


@dataclass(frozen=True)
class SmartSizingDecision:
    symbol: str
    side: str
    mode: SmartSizingMode
    base_notional_usdt: Decimal
    recommended_notional_usdt: Decimal
    final_notional_usdt: Decimal
    sizing_multiplier: Decimal
    risk_amount_usdt: Decimal
    risk_pct: Decimal
    max_allowed_notional_usdt: Decimal | None
    min_required_notional_usdt: Decimal | None
    blocked: bool
    reduced: bool
    reason: str
    diagnostics: dict[str, Any] = field(default_factory=dict)
    affected_result: bool = False

    @property
    def smart_sizing_used(self) -> bool:
        return self.mode is not SmartSizingMode.OFF

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "mode": self.mode.value,
            "base_notional_usdt": str(self.base_notional_usdt),
            "recommended_notional_usdt": str(self.recommended_notional_usdt),
            "final_notional_usdt": str(self.final_notional_usdt),
            "sizing_multiplier": str(self.sizing_multiplier),
            "risk_amount_usdt": str(self.risk_amount_usdt),
            "risk_pct": str(self.risk_pct),
            "max_allowed_notional_usdt": (
                str(self.max_allowed_notional_usdt)
                if self.max_allowed_notional_usdt is not None
                else None
            ),
            "min_required_notional_usdt": (
                str(self.min_required_notional_usdt)
                if self.min_required_notional_usdt is not None
                else None
            ),
            "blocked": self.blocked,
            "reduced": self.reduced,
            "reason": self.reason,
            "affected_result": self.affected_result,
            "smart_sizing_used": self.smart_sizing_used,
            "diagnostics": dict(self.diagnostics),
        }


class SmartSizingEngine:
    def __init__(self, settings: SmartSizingSettings | None = None):
        self.settings = settings or SmartSizingSettings()

    def evaluate(self, sizing_input: SmartSizingInput) -> SmartSizingDecision:
        mode = _normalize_mode(sizing_input.mode)
        settings = sizing_input.settings or self.settings
        symbol = str(sizing_input.symbol or "").upper().strip()
        side = str(sizing_input.side or "").upper().strip()
        base_notional = _decimal_or_zero(sizing_input.base_notional_usdt)
        entry_price = _decimal_or_zero(sizing_input.entry_price)
        min_required = _decimal_or_none(sizing_input.min_required_notional_usdt)
        max_allowed = _max_allowed_notional(
            base_notional,
            _decimal_or_none(sizing_input.max_exposure_usdt),
        )

        if mode is SmartSizingMode.OFF:
            return _decision(
                symbol=symbol,
                side=side,
                mode=mode,
                base_notional=base_notional,
                recommended_notional=base_notional,
                final_notional=base_notional,
                multiplier=FULL_SIZE_MULTIPLIER,
                entry_price=entry_price,
                risk_abs=_planned_decimal(sizing_input, "risk_abs"),
                min_required=min_required,
                max_allowed=max_allowed,
                reason="SMART_SIZING_OFF",
                affected_result=False,
                diagnostics={
                    "version": SMART_SIZING_VERSION,
                    "mode": mode.value,
                    "settings": settings.to_dict(),
                    "min_notional_policy": SmartSizingMinNotionalPolicy.normalize(
                        settings.min_notional_policy
                    ).value,
                    "min_notional_adjustment": "NONE",
                    "applied_reasons": ["SMART_SIZING_OFF"],
                    "raw_multiplier": str(settings.full_size_multiplier),
                    "capped_by_max_exposure": False,
                    "capped_by_base_notional": False,
                    "would_have_reduced_in_shadow": False,
                    "reduction_amount_usdt": "0",
                    "reduction_pct": "0",
                },
            )

        diagnostics: dict[str, Any] = {
            "version": SMART_SIZING_VERSION,
            "mode": mode.value,
            "settings": settings.to_dict(),
            "strategy_priority": sizing_input.strategy_priority,
            "consecutive_losses": int(sizing_input.consecutive_losses or 0),
            "symbol_health": sizing_input.symbol_health,
            "min_notional_policy": SmartSizingMinNotionalPolicy.normalize(
                settings.min_notional_policy
            ).value,
            "min_notional_adjustment": "NONE",
            "capped_by_max_exposure": False,
            "capped_by_base_notional": False,
        }
        if sizing_input.drawdown_diagnostics is not None:
            diagnostics["drawdown"] = dict(sizing_input.drawdown_diagnostics)
        if base_notional <= 0:
            return _blocked(
                symbol=symbol,
                side=side,
                mode=mode,
                base_notional=base_notional,
                entry_price=entry_price,
                min_required=min_required,
                max_allowed=max_allowed,
                reason="BASE_NOTIONAL_NOT_POSITIVE",
                diagnostics=diagnostics,
            )
        if entry_price <= 0:
            return _blocked(
                symbol=symbol,
                side=side,
                mode=mode,
                base_notional=base_notional,
                entry_price=entry_price,
                min_required=min_required,
                max_allowed=max_allowed,
                reason="ENTRY_PRICE_NOT_POSITIVE",
                diagnostics=diagnostics,
            )
        if (
            mode.can_affect_execution
            and sizing_input.require_current_drawdown
            and _decimal_or_none(sizing_input.current_drawdown) is None
        ):
            return _blocked(
                symbol=symbol,
                side=side,
                mode=mode,
                base_notional=base_notional,
                entry_price=entry_price,
                min_required=min_required,
                max_allowed=max_allowed,
                reason="SMART_SIZING_DRAWDOWN_UNAVAILABLE",
                diagnostics=diagnostics,
            )

        multiplier, reasons, quality_diagnostics = _quality_multiplier(
            sizing_input,
            settings=settings,
        )
        diagnostics.update(quality_diagnostics)
        diagnostics["raw_multiplier"] = str(multiplier)
        diagnostics["reasons"] = reasons
        diagnostics["applied_reasons"] = list(reasons)

        recommended_notional = base_notional * multiplier
        if max_allowed is not None and recommended_notional > max_allowed:
            recommended_notional = max_allowed
            reasons.append("MAX_EXPOSURE_CAP")
            diagnostics["capped_by_max_exposure"] = True
        if recommended_notional > base_notional:
            diagnostics["capped_by_base_notional"] = True
        recommended_notional = min(recommended_notional, base_notional)
        policy_result = _apply_min_notional_policy(
            recommended_notional=recommended_notional,
            base_notional=base_notional,
            min_required=min_required,
            settings=settings,
        )
        diagnostics["applied_reasons"] = list(reasons)
        diagnostics["min_notional_adjustment"] = policy_result.adjustment
        if policy_result.reason:
            diagnostics["applied_reasons"].append(policy_result.reason)
        _add_reduction_diagnostics(
            diagnostics,
            base_notional=base_notional,
            recommended_notional=recommended_notional,
            final_notional=base_notional,
        )

        if mode in {SmartSizingMode.OBSERVE, SmartSizingMode.SHADOW}:
            reason = (
                "SMART_SIZING_OBSERVED"
                if mode is SmartSizingMode.OBSERVE
                else "SMART_SIZING_SHADOW"
            )
            return _decision(
                symbol=symbol,
                side=side,
                mode=mode,
                base_notional=base_notional,
                recommended_notional=policy_result.notional,
                final_notional=base_notional,
                multiplier=settings.full_size_multiplier,
                entry_price=entry_price,
                risk_abs=_planned_decimal(sizing_input, "risk_abs"),
                min_required=min_required,
                max_allowed=max_allowed,
                reason=reason,
                affected_result=False,
                diagnostics=diagnostics,
            )

        if policy_result.blocked:
            _add_reduction_diagnostics(
                diagnostics,
                base_notional=base_notional,
                recommended_notional=recommended_notional,
                final_notional=Decimal("0"),
            )
            return _blocked(
                symbol=symbol,
                side=side,
                mode=mode,
                base_notional=base_notional,
                recommended_notional=recommended_notional,
                entry_price=entry_price,
                min_required=min_required,
                max_allowed=max_allowed,
                reason=policy_result.reason or "SMART_SIZING_BELOW_MIN_NOTIONAL",
                diagnostics=diagnostics,
            )

        final_notional = min(policy_result.notional, base_notional)
        if mode is SmartSizingMode.REDUCE_ONLY and final_notional > base_notional:
            final_notional = base_notional
            reasons.append("REDUCE_ONLY_PREVENTED_INCREASE")
            diagnostics["applied_reasons"] = list(reasons)

        reduced = final_notional < base_notional
        reason = (
            policy_result.reason
            if policy_result.reason
            else "SMART_SIZING_APPLIED"
            if reduced
            else "SMART_SIZING_NO_CHANGE"
        )
        if mode is SmartSizingMode.ACTIVE and not reduced:
            reason = "SMART_SIZING_ACTIVE_CONSERVATIVE_NO_INCREASE"
        affected = reduced and policy_result.adjustment != "FALLBACK_TO_BASE"
        _add_reduction_diagnostics(
            diagnostics,
            base_notional=base_notional,
            recommended_notional=recommended_notional,
            final_notional=final_notional,
        )
        return _decision(
            symbol=symbol,
            side=side,
            mode=mode,
            base_notional=base_notional,
            recommended_notional=policy_result.notional,
            final_notional=final_notional,
            multiplier=_safe_div(final_notional, base_notional),
            entry_price=entry_price,
            risk_abs=_planned_decimal(sizing_input, "risk_abs"),
            min_required=min_required,
            max_allowed=max_allowed,
            reason=reason,
            affected_result=affected,
            diagnostics=diagnostics,
        )


def _quality_multiplier(
    sizing_input: SmartSizingInput,
    *,
    settings: SmartSizingSettings,
) -> tuple[Decimal, list[str], dict[str, Any]]:
    multiplier = settings.full_size_multiplier
    reasons: list[str] = []
    reward_risk = _planned_decimal(sizing_input, "reward_risk_liquido")
    cost_ratio = _planned_decimal(sizing_input, "cost_to_reward_ratio")
    risk_abs = _planned_decimal(sizing_input, "risk_abs")
    reward_abs = _planned_decimal(sizing_input, "reward_abs")
    score = _decimal_or_none(sizing_input.strategy_score)
    drawdown = _decimal_or_none(sizing_input.current_drawdown)
    health = str(sizing_input.symbol_health or "").upper().strip()

    if reward_risk is None:
        reasons.append("REWARD_RISK_UNAVAILABLE")
    elif reward_risk <= 0:
        multiplier = min(multiplier, settings.low_quality_multiplier)
        reasons.append("REWARD_RISK_INVALID")
    elif reward_risk < settings.low_reward_risk:
        multiplier = min(multiplier, settings.low_quality_multiplier)
        reasons.append("LOW_REWARD_RISK")
    elif reward_risk < settings.medium_reward_risk:
        multiplier = min(multiplier, settings.medium_quality_multiplier)
        reasons.append("MEDIUM_REWARD_RISK")

    if cost_ratio is not None:
        if cost_ratio > settings.high_cost_to_reward_ratio:
            multiplier = min(multiplier, settings.low_quality_multiplier)
            reasons.append("HIGH_COST_TO_REWARD_RATIO")
        elif cost_ratio > settings.elevated_cost_to_reward_ratio:
            multiplier = min(multiplier, settings.medium_quality_multiplier)
            reasons.append("ELEVATED_COST_TO_REWARD_RATIO")

    losses = int(sizing_input.consecutive_losses or 0)
    if settings.high_consecutive_losses and losses >= settings.high_consecutive_losses:
        multiplier = min(multiplier, settings.low_quality_multiplier)
        reasons.append("HIGH_CONSECUTIVE_LOSSES")
    elif settings.elevated_consecutive_losses and losses >= settings.elevated_consecutive_losses:
        multiplier = min(multiplier, settings.medium_quality_multiplier)
        reasons.append("ELEVATED_CONSECUTIVE_LOSSES")

    if drawdown is not None:
        if drawdown >= settings.high_drawdown:
            multiplier = min(multiplier, settings.high_risk_multiplier)
            reasons.append("HIGH_DRAWDOWN")
        elif drawdown >= settings.elevated_drawdown:
            multiplier = min(multiplier, settings.medium_quality_multiplier)
            reasons.append("ELEVATED_DRAWDOWN")
    elif sizing_input.require_current_drawdown:
        reasons.append("DRAWDOWN_UNAVAILABLE")

    if score is not None:
        if score < settings.low_score_threshold:
            multiplier = min(multiplier, settings.low_quality_multiplier)
            reasons.append("LOW_STRATEGY_SCORE")
        elif score < settings.medium_score_threshold:
            multiplier = min(multiplier, settings.medium_quality_multiplier)
            reasons.append("MEDIUM_STRATEGY_SCORE")

    if health in {"BLOCKED", "QUARANTINE", "QUARANTINED"}:
        multiplier = Decimal("0")
        reasons.append("SYMBOL_HEALTH_BLOCKED")
    elif health in {"ALERT", "WARNING", "DEGRADED"}:
        multiplier = min(multiplier, settings.low_quality_multiplier)
        reasons.append("SYMBOL_HEALTH_ALERT")

    if not reasons:
        reasons.append("QUALITY_ACCEPTABLE")

    return (
        multiplier,
        reasons,
        {
            "reward_risk_liquido": str(reward_risk) if reward_risk is not None else None,
            "cost_to_reward_ratio": str(cost_ratio) if cost_ratio is not None else None,
            "risk_abs": str(risk_abs) if risk_abs is not None else None,
            "reward_abs": str(reward_abs) if reward_abs is not None else None,
            "strategy_score": str(score) if score is not None else None,
            "current_drawdown": str(drawdown) if drawdown is not None else None,
            "recommended_multiplier": str(multiplier),
        },
    )


@dataclass(frozen=True)
class _MinNotionalPolicyResult:
    notional: Decimal
    blocked: bool
    reason: str | None
    adjustment: str


def _apply_min_notional_policy(
    *,
    recommended_notional: Decimal,
    base_notional: Decimal,
    min_required: Decimal | None,
    settings: SmartSizingSettings,
) -> _MinNotionalPolicyResult:
    if min_required is None or min_required <= 0:
        return _MinNotionalPolicyResult(
            notional=recommended_notional,
            blocked=False,
            reason=None,
            adjustment="NONE",
        )
    if recommended_notional >= min_required:
        return _MinNotionalPolicyResult(
            notional=recommended_notional,
            blocked=False,
            reason=None,
            adjustment="NONE",
        )

    policy = SmartSizingMinNotionalPolicy.normalize(settings.min_notional_policy)
    if policy is SmartSizingMinNotionalPolicy.BLOCK:
        return _MinNotionalPolicyResult(
            notional=recommended_notional,
            blocked=True,
            reason="SMART_SIZING_BELOW_MIN_NOTIONAL",
            adjustment="BLOCK",
        )
    if policy is SmartSizingMinNotionalPolicy.FLOOR_TO_MIN:
        if min_required <= base_notional:
            return _MinNotionalPolicyResult(
                notional=min_required,
                blocked=False,
                reason="SMART_SIZING_FLOORED_TO_MIN_NOTIONAL",
                adjustment="FLOOR_TO_MIN",
            )
        return _MinNotionalPolicyResult(
            notional=recommended_notional,
            blocked=True,
            reason="SMART_SIZING_BELOW_MIN_NOTIONAL",
            adjustment="BLOCK_MIN_ABOVE_BASE",
        )
    if policy is SmartSizingMinNotionalPolicy.FALLBACK_TO_BASE:
        return _MinNotionalPolicyResult(
            notional=base_notional,
            blocked=False,
            reason="SMART_SIZING_MIN_NOTIONAL_FALLBACK_TO_BASE",
            adjustment="FALLBACK_TO_BASE",
        )

    return _MinNotionalPolicyResult(
        notional=recommended_notional,
        blocked=True,
        reason="SMART_SIZING_INVALID_MIN_NOTIONAL_POLICY",
        adjustment="BLOCK",
    )


def _add_reduction_diagnostics(
    diagnostics: dict[str, Any],
    *,
    base_notional: Decimal,
    recommended_notional: Decimal,
    final_notional: Decimal,
) -> None:
    recommended_reduction = max(base_notional - recommended_notional, Decimal("0"))
    actual_reduction = max(base_notional - final_notional, Decimal("0"))
    diagnostics["would_have_reduced_in_shadow"] = recommended_reduction > 0
    diagnostics["recommended_reduction_amount_usdt"] = str(recommended_reduction)
    diagnostics["recommended_reduction_pct"] = str(_safe_div(recommended_reduction, base_notional))
    diagnostics["reduction_amount_usdt"] = str(actual_reduction)
    diagnostics["reduction_pct"] = str(_safe_div(actual_reduction, base_notional))


def _decision(
    *,
    symbol: str,
    side: str,
    mode: SmartSizingMode,
    base_notional: Decimal,
    recommended_notional: Decimal,
    final_notional: Decimal,
    multiplier: Decimal,
    entry_price: Decimal,
    risk_abs: Decimal | None,
    min_required: Decimal | None,
    max_allowed: Decimal | None,
    reason: str,
    affected_result: bool,
    diagnostics: dict[str, Any],
) -> SmartSizingDecision:
    risk_pct = _safe_div(risk_abs or Decimal("0"), entry_price)
    quantity = _safe_div(final_notional, entry_price)
    risk_amount = (risk_abs or Decimal("0")) * quantity
    reduced = final_notional < base_notional
    return SmartSizingDecision(
        symbol=symbol,
        side=side,
        mode=mode,
        base_notional_usdt=base_notional,
        recommended_notional_usdt=recommended_notional,
        final_notional_usdt=final_notional,
        sizing_multiplier=multiplier,
        risk_amount_usdt=risk_amount,
        risk_pct=risk_pct,
        max_allowed_notional_usdt=max_allowed,
        min_required_notional_usdt=min_required,
        blocked=False,
        reduced=reduced,
        reason=reason,
        affected_result=bool(affected_result),
        diagnostics=diagnostics,
    )


def _blocked(
    *,
    symbol: str,
    side: str,
    mode: SmartSizingMode,
    base_notional: Decimal,
    entry_price: Decimal,
    min_required: Decimal | None,
    max_allowed: Decimal | None,
    reason: str,
    diagnostics: dict[str, Any],
    recommended_notional: Decimal | None = None,
) -> SmartSizingDecision:
    return SmartSizingDecision(
        symbol=symbol,
        side=side,
        mode=mode,
        base_notional_usdt=base_notional,
        recommended_notional_usdt=recommended_notional or Decimal("0"),
        final_notional_usdt=Decimal("0"),
        sizing_multiplier=Decimal("0"),
        risk_amount_usdt=Decimal("0"),
        risk_pct=Decimal("0"),
        max_allowed_notional_usdt=max_allowed,
        min_required_notional_usdt=min_required,
        blocked=True,
        reduced=False,
        reason=reason,
        affected_result=mode.can_affect_execution,
        diagnostics=diagnostics,
    )


def _planned_decimal(sizing_input: SmartSizingInput, key: str) -> Decimal | None:
    value = getattr(sizing_input, key, None)
    if value is None and isinstance(sizing_input.planned_exit, dict):
        value = sizing_input.planned_exit.get(key)
    return _decimal_or_none(value)


def _max_allowed_notional(
    base_notional: Decimal,
    max_exposure_usdt: Decimal | None,
) -> Decimal | None:
    if max_exposure_usdt is None or max_exposure_usdt <= 0:
        return None
    if base_notional <= 0:
        return max_exposure_usdt
    return min(base_notional, max_exposure_usdt)


def _normalize_mode(value: SmartSizingMode | str) -> SmartSizingMode:
    return parse_module_mode(value, SmartSizingMode, name="smart_sizing_mode")


def _decimal_or_zero(value: Any) -> Decimal:
    return _decimal_or_none(value) or Decimal("0")


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


def _safe_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return numerator / denominator


__all__ = [
    "SmartSizingDecision",
    "SmartSizingEngine",
    "SmartSizingInput",
    "SmartSizingMinNotionalPolicy",
    "SmartSizingSettings",
]
