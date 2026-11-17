from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict

from domain.models import TradeAction
from domain.symbol_rules import round_price_down, round_price_up

STRATEGY_METADATA_EXIT_SOURCE = "STRATEGY_METADATA"
GLOBAL_FALLBACK_EXIT_SOURCE = "GLOBAL_FALLBACK"
INVALID_STRATEGY_PLANNED_EXIT = "INVALID_STRATEGY_PLANNED_EXIT"

RECOMMENDED_STOP_KEY = "recommended_stop_price"
RECOMMENDED_TAKE_KEY = "recommended_take_profit_price"


class _PlannedExitValues(TypedDict):
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    risk_abs: Decimal | None
    reward_abs: Decimal | None
    estimated_cost_abs: Decimal | None
    net_reward_abs: Decimal | None
    net_risk_abs: Decimal | None
    reward_risk_liquido: Decimal | None
    raw_reward_risk: Decimal | None
    cost_to_reward_ratio: Decimal | None
    risk_pct: Decimal | None
    reward_pct: Decimal | None


@dataclass(frozen=True)
class StrategyPlannedExitCandidate:
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    risk_abs: Decimal | None = None
    reward_abs: Decimal | None = None
    estimated_cost_abs: Decimal | None = None
    net_reward_abs: Decimal | None = None
    net_risk_abs: Decimal | None = None
    reward_risk_liquido: Decimal | None = None
    raw_reward_risk: Decimal | None = None
    cost_to_reward_ratio: Decimal | None = None
    risk_pct: Decimal | None = None
    reward_pct: Decimal | None = None
    source: str = STRATEGY_METADATA_EXIT_SOURCE
    valid: bool = False
    rejection_reason: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "stop_price": self.stop_price,
            "take_profit_price": self.take_profit_price,
            "risk_abs": self.risk_abs,
            "reward_abs": self.reward_abs,
            "estimated_cost_abs": self.estimated_cost_abs,
            "net_reward_abs": self.net_reward_abs,
            "net_risk_abs": self.net_risk_abs,
            "reward_risk_liquido": self.reward_risk_liquido,
            "raw_reward_risk": self.raw_reward_risk,
            "cost_to_reward_ratio": self.cost_to_reward_ratio,
            "risk_pct": self.risk_pct,
            "reward_pct": self.reward_pct,
            "valid": self.valid,
            "rejection_reason": self.rejection_reason,
            "diagnostics": dict(self.diagnostics),
        }


def extract_strategy_planned_exit(metadata: dict[str, Any] | None) -> StrategyPlannedExitCandidate:
    if not isinstance(metadata, dict):
        return _invalid("METADATA_NOT_MAPPING", metadata_type=type(metadata).__name__)

    values: _PlannedExitValues = {
        "stop_price": _decimal_or_none(metadata.get(RECOMMENDED_STOP_KEY)),
        "take_profit_price": _decimal_or_none(metadata.get(RECOMMENDED_TAKE_KEY)),
        "risk_abs": _decimal_or_none(metadata.get("risk_abs")),
        "reward_abs": _decimal_or_none(metadata.get("reward_abs")),
        "estimated_cost_abs": _decimal_or_none(metadata.get("estimated_cost_abs")),
        "net_reward_abs": _decimal_or_none(metadata.get("net_reward_abs")),
        "net_risk_abs": _decimal_or_none(metadata.get("net_risk_abs")),
        "reward_risk_liquido": _decimal_or_none(metadata.get("reward_risk_liquido")),
        "raw_reward_risk": _decimal_or_none(metadata.get("raw_reward_risk")),
        "cost_to_reward_ratio": _decimal_or_none(metadata.get("cost_to_reward_ratio")),
        "risk_pct": _decimal_or_none(metadata.get("risk_pct")),
        "reward_pct": _decimal_or_none(metadata.get("reward_pct")),
    }
    diagnostics = {
        "metadata_keys": sorted(str(key) for key in metadata.keys()),
        "setup_math_side": metadata.get("setup_math_side") or metadata.get("setup_side"),
        "metadata_entry_price": metadata.get("entry_price"),
    }

    missing = [
        key
        for key in (
            "stop_price",
            "take_profit_price",
            "risk_abs",
            "reward_abs",
            "reward_risk_liquido",
        )
        if values[key] is None
    ]
    if missing:
        return StrategyPlannedExitCandidate(
            **values,
            valid=False,
            rejection_reason="MISSING_STRATEGY_PLANNED_EXIT_FIELD:" + ",".join(missing),
            diagnostics=diagnostics,
        )

    candidate = StrategyPlannedExitCandidate(
        **values,
        valid=True,
        diagnostics=diagnostics,
    )
    for key in ("stop_price", "take_profit_price", "risk_abs", "reward_abs"):
        value = getattr(candidate, key)
        if value is None or value <= 0:
            return replace(
                candidate,
                valid=False,
                rejection_reason=f"{key.upper()}_NOT_POSITIVE",
            )
    if candidate.reward_risk_liquido is None or candidate.reward_risk_liquido <= 0:
        return replace(
            candidate,
            valid=False,
            rejection_reason="REWARD_RISK_LIQUIDO_NOT_POSITIVE",
        )
    return candidate


def validate_strategy_planned_exit(
    side: str | TradeAction,
    entry_price: Decimal,
    candidate: StrategyPlannedExitCandidate,
    price_tick: Decimal | None = None,
    min_liquid_rr: Decimal | None = None,
) -> StrategyPlannedExitCandidate:
    diagnostics = dict(candidate.diagnostics)
    diagnostics["entry_price"] = str(entry_price)
    diagnostics["side"] = str(getattr(side, "value", side))
    diagnostics["price_tick"] = str(price_tick) if price_tick is not None else None
    diagnostics["raw_stop_price"] = (
        str(candidate.stop_price) if candidate.stop_price is not None else None
    )
    diagnostics["raw_take_profit_price"] = (
        str(candidate.take_profit_price) if candidate.take_profit_price is not None else None
    )

    if candidate.source != STRATEGY_METADATA_EXIT_SOURCE:
        return replace(
            candidate,
            valid=False,
            rejection_reason="SOURCE_NOT_STRATEGY_METADATA",
            diagnostics=diagnostics,
        )
    if not candidate.valid:
        return replace(candidate, diagnostics=diagnostics)

    action = _normalize_open_action(side)
    if action is None:
        return replace(
            candidate,
            valid=False,
            rejection_reason="ACTION_NOT_OPEN",
            diagnostics=diagnostics,
        )
    if entry_price <= 0:
        return replace(
            candidate,
            valid=False,
            rejection_reason="ENTRY_PRICE_NOT_POSITIVE",
            diagnostics=diagnostics,
        )
    if candidate.stop_price is None or candidate.take_profit_price is None:
        return replace(
            candidate,
            valid=False,
            rejection_reason="STOP_OR_TAKE_MISSING",
            diagnostics=diagnostics,
        )
    if candidate.stop_price <= 0 or candidate.take_profit_price <= 0:
        return replace(
            candidate,
            valid=False,
            rejection_reason="STOP_OR_TAKE_NOT_POSITIVE",
            diagnostics=diagnostics,
        )
    if candidate.risk_abs is None or candidate.risk_abs <= 0:
        return replace(
            candidate,
            valid=False,
            rejection_reason="RISK_ABS_NOT_POSITIVE",
            diagnostics=diagnostics,
        )
    if candidate.reward_abs is None or candidate.reward_abs <= 0:
        return replace(
            candidate,
            valid=False,
            rejection_reason="REWARD_ABS_NOT_POSITIVE",
            diagnostics=diagnostics,
        )
    if candidate.reward_risk_liquido is None or candidate.reward_risk_liquido <= 0:
        return replace(
            candidate,
            valid=False,
            rejection_reason="REWARD_RISK_LIQUIDO_NOT_POSITIVE",
            diagnostics=diagnostics,
        )
    if min_liquid_rr is not None and candidate.reward_risk_liquido < min_liquid_rr:
        return replace(
            candidate,
            valid=False,
            rejection_reason="LIQUID_REWARD_RISK_BELOW_MIN",
            diagnostics=diagnostics,
        )

    stop = candidate.stop_price
    take = candidate.take_profit_price
    if price_tick is not None:
        tick = Decimal(str(price_tick))
        if tick <= 0:
            return replace(
                candidate,
                valid=False,
                rejection_reason="PRICE_TICK_NOT_POSITIVE",
                diagnostics=diagnostics,
            )
        if action == TradeAction.OPEN_LONG:
            stop = round_price_down(stop, tick)
            take = round_price_up(take, tick)
        else:
            stop = round_price_up(stop, tick)
            take = round_price_down(take, tick)
        diagnostics["normalized_stop_price"] = str(stop)
        diagnostics["normalized_take_profit_price"] = str(take)

    if stop <= 0 or take <= 0:
        return replace(
            candidate,
            stop_price=stop,
            take_profit_price=take,
            valid=False,
            rejection_reason="NORMALIZED_STOP_OR_TAKE_NOT_POSITIVE",
            diagnostics=diagnostics,
        )
    if action == TradeAction.OPEN_LONG:
        if stop >= entry_price:
            return replace(
                candidate,
                stop_price=stop,
                take_profit_price=take,
                valid=False,
                rejection_reason="LONG_STOP_NOT_BELOW_ENTRY",
                diagnostics=diagnostics,
            )
        if take <= entry_price:
            return replace(
                candidate,
                stop_price=stop,
                take_profit_price=take,
                valid=False,
                rejection_reason="LONG_TAKE_NOT_ABOVE_ENTRY",
                diagnostics=diagnostics,
            )
    elif action == TradeAction.OPEN_SHORT:
        if stop <= entry_price:
            return replace(
                candidate,
                stop_price=stop,
                take_profit_price=take,
                valid=False,
                rejection_reason="SHORT_STOP_NOT_ABOVE_ENTRY",
                diagnostics=diagnostics,
            )
        if take >= entry_price:
            return replace(
                candidate,
                stop_price=stop,
                take_profit_price=take,
                valid=False,
                rejection_reason="SHORT_TAKE_NOT_BELOW_ENTRY",
                diagnostics=diagnostics,
            )

    return replace(
        candidate,
        stop_price=stop,
        take_profit_price=take,
        valid=True,
        rejection_reason=None,
        diagnostics=diagnostics,
    )


def resolve_strategy_planned_exit(
    metadata: dict[str, Any] | None,
    *,
    action: str | TradeAction,
    entry_price: Decimal,
    price_tick: Decimal,
) -> tuple[bool, StrategyPlannedExitCandidate, StrategyPlannedExitCandidate]:
    """Validate one strategy exit identically in live and historical planning."""
    present = has_strategy_planned_exit_metadata(metadata)
    extracted = extract_strategy_planned_exit(metadata)
    validated = validate_strategy_planned_exit(
        action, entry_price, extracted, price_tick=price_tick
    )
    return present, extracted, validated


def planned_exit_payload_from_candidate(
    candidate: StrategyPlannedExitCandidate,
) -> dict[str, Any] | None:
    if not candidate.valid or candidate.stop_price is None or candidate.take_profit_price is None:
        return None
    return candidate.as_payload()


def has_strategy_planned_exit_metadata(metadata: dict[str, Any] | None) -> bool:
    return isinstance(metadata, dict) and (
        RECOMMENDED_STOP_KEY in metadata or RECOMMENDED_TAKE_KEY in metadata
    )


def _invalid(reason: str, **diagnostics: Any) -> StrategyPlannedExitCandidate:
    return StrategyPlannedExitCandidate(
        valid=False,
        rejection_reason=reason,
        diagnostics=diagnostics,
    )


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


def _normalize_open_action(side: str | TradeAction) -> TradeAction | None:
    if isinstance(side, TradeAction):
        return side if side in {TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT} else None
    normalized = str(side or "").upper().strip()
    if normalized in {"LONG", "OPEN_LONG", "BUY"}:
        return TradeAction.OPEN_LONG
    if normalized in {"SHORT", "OPEN_SHORT", "SELL"}:
        return TradeAction.OPEN_SHORT
    return None


__all__ = [
    "GLOBAL_FALLBACK_EXIT_SOURCE",
    "INVALID_STRATEGY_PLANNED_EXIT",
    "STRATEGY_METADATA_EXIT_SOURCE",
    "StrategyPlannedExitCandidate",
    "extract_strategy_planned_exit",
    "has_strategy_planned_exit_metadata",
    "planned_exit_payload_from_candidate",
    "validate_strategy_planned_exit",
]
