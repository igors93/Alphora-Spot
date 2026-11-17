from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from trading.plan import TradePlan

INTELLIGENT_EXIT_PLAN_VERSION = "0.2.0"
INTELLIGENT_EXIT_PLAN_SOURCE_SHADOW = "SHADOW"

INTELLIGENT_EXIT_STATUS_NO_TRADE_PLAN = "NO_TRADE_PLAN"
INTELLIGENT_EXIT_STATUS_NOT_ENTRY_ACTION = "NOT_ENTRY_ACTION"
INTELLIGENT_EXIT_STATUS_ENTRY_PRICE_INVALID = "ENTRY_PRICE_INVALID"
INTELLIGENT_EXIT_STATUS_EXIT_NOT_AVAILABLE = "EXIT_NOT_AVAILABLE"
INTELLIGENT_EXIT_STATUS_EXIT_INVALID = "EXIT_INVALID"
INTELLIGENT_EXIT_STATUS_SHADOW_READY = "SHADOW_READY"

EXIT_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}

MINIMUM_REWARD_RISK = Decimal("2")
MINIMUM_STOP_DISTANCE_PCT = Decimal("0.005")
PROTECTION_QUALITY_INVALID = "INVALID"
PROTECTION_QUALITY_INCOMPLETE = "INCOMPLETE"
PROTECTION_QUALITY_TOO_TIGHT = "TOO_TIGHT"
PROTECTION_QUALITY_WEAK = "WEAK"
PROTECTION_QUALITY_ACCEPTABLE = "ACCEPTABLE"
PROTECTION_QUALITY_STRONG = "STRONG"

RECOMMENDATION_MODE_KEEP_EXISTING = "KEEP_EXISTING"
RECOMMENDATION_MODE_ADAPT_STOP_DISTANCE = "ADAPT_STOP_DISTANCE"
RECOMMENDATION_MODE_ADAPT_REWARD_RISK = "ADAPT_REWARD_RISK"
RECOMMENDATION_MODE_ADAPT_STOP_AND_REWARD = "ADAPT_STOP_AND_REWARD"


def _new_intelligent_exit_plan_id() -> str:
    return f"iexit_{uuid4().hex}"


@dataclass(frozen=True)
class IntelligentExitPlan:
    symbol: str
    action: str
    status: str
    reason: str
    trade_plan_id: str | None = None
    entry_reference_price: Decimal | None = None
    recommended_stop_price: Decimal | None = None
    recommended_take_profit_price: Decimal | None = None
    current_stop_price: Decimal | None = None
    current_take_profit_price: Decimal | None = None
    stop_distance_pct: Decimal | None = None
    take_profit_distance_pct: Decimal | None = None
    risk_reward: Decimal | None = None
    adaptive_stop_price: Decimal | None = None
    adaptive_take_profit_price: Decimal | None = None
    adaptive_stop_distance_pct: Decimal | None = None
    adaptive_take_profit_distance_pct: Decimal | None = None
    adaptive_risk_reward: Decimal | None = None
    minimum_reward_risk: Decimal = MINIMUM_REWARD_RISK
    minimum_stop_distance_pct: Decimal = MINIMUM_STOP_DISTANCE_PCT
    recommendation_mode: str = RECOMMENDATION_MODE_KEEP_EXISTING
    protection_quality: str = PROTECTION_QUALITY_INCOMPLETE
    adaptive_candidate_available: bool = False
    adaptive_changed: bool = False
    stop_valid: bool = False
    take_profit_valid: bool = False
    protection_complete: bool = False
    shadow_only: bool = True
    can_use_for_execution: bool = False
    plan_id: str = field(default_factory=_new_intelligent_exit_plan_id)
    version: str = INTELLIGENT_EXIT_PLAN_VERSION
    source: str = INTELLIGENT_EXIT_PLAN_SOURCE_SHADOW
    diagnostics: dict[str, Any] = field(default_factory=dict)


class IntelligentExitBuilder:
    """Monta um plano inteligente de protecao em modo shadow.

    Fase 5E:
    - calcula qualidade de stop/take
    - cria candidato adaptativo em campos separados
    - nao altera recommended_stop_price/recommended_take_profit_price
    - nao altera execucao
    """

    def build_shadow_plan(
        self,
        *,
        trade_plan: TradePlan | None,
        action: str,
        planned_exit: dict[str, Decimal] | None,
        architecture_mode: str,
        intelligent_activation_level: str,
        dynamic_exit_enabled: bool,
    ) -> IntelligentExitPlan:
        if trade_plan is None:
            return IntelligentExitPlan(
                symbol="",
                action=action,
                status=INTELLIGENT_EXIT_STATUS_NO_TRADE_PLAN,
                reason="TRADE_PLAN_NOT_AVAILABLE",
                protection_quality=PROTECTION_QUALITY_INCOMPLETE,
                diagnostics=_diagnostics(
                    architecture_mode=architecture_mode,
                    intelligent_activation_level=intelligent_activation_level,
                    dynamic_exit_enabled=dynamic_exit_enabled,
                    safety_note="Sem TradePlan disponivel para montar protecao.",
                ),
            )

        if action not in EXIT_ENTRY_ACTIONS:
            return IntelligentExitPlan(
                symbol=trade_plan.symbol,
                action=action,
                status=INTELLIGENT_EXIT_STATUS_NOT_ENTRY_ACTION,
                reason="INTELLIGENT_EXIT_ONLY_EVALUATES_ENTRY_ACTIONS",
                trade_plan_id=trade_plan.plan_id,
                entry_reference_price=trade_plan.entry_reference_price,
                protection_quality=PROTECTION_QUALITY_INCOMPLETE,
                diagnostics=_diagnostics(
                    architecture_mode=architecture_mode,
                    intelligent_activation_level=intelligent_activation_level,
                    dynamic_exit_enabled=dynamic_exit_enabled,
                    trade_plan_status=trade_plan.status,
                    safety_note="Saidas nao recebem stop/take novo nesta fase.",
                ),
            )

        entry = trade_plan.entry_reference_price
        if entry is None or entry <= 0:
            return IntelligentExitPlan(
                symbol=trade_plan.symbol,
                action=action,
                status=INTELLIGENT_EXIT_STATUS_ENTRY_PRICE_INVALID,
                reason="ENTRY_REFERENCE_PRICE_INVALID",
                trade_plan_id=trade_plan.plan_id,
                entry_reference_price=entry,
                protection_quality=PROTECTION_QUALITY_INVALID,
                diagnostics=_diagnostics(
                    architecture_mode=architecture_mode,
                    intelligent_activation_level=intelligent_activation_level,
                    dynamic_exit_enabled=dynamic_exit_enabled,
                    trade_plan_status=trade_plan.status,
                    safety_note="Preco de entrada invalido para calcular protecao.",
                ),
            )

        current_stop = trade_plan.stop_price
        current_take = trade_plan.take_profit_price
        if planned_exit is not None:
            current_stop = planned_exit.get("stop_price")
            current_take = planned_exit.get("take_profit_price")

        if current_stop is None or current_take is None:
            return IntelligentExitPlan(
                symbol=trade_plan.symbol,
                action=action,
                status=INTELLIGENT_EXIT_STATUS_EXIT_NOT_AVAILABLE,
                reason="STOP_OR_TAKE_PROFIT_NOT_AVAILABLE",
                trade_plan_id=trade_plan.plan_id,
                entry_reference_price=entry,
                current_stop_price=current_stop,
                current_take_profit_price=current_take,
                protection_quality=PROTECTION_QUALITY_INCOMPLETE,
                diagnostics=_diagnostics(
                    architecture_mode=architecture_mode,
                    intelligent_activation_level=intelligent_activation_level,
                    dynamic_exit_enabled=dynamic_exit_enabled,
                    trade_plan_status=trade_plan.status,
                    safety_note="Stop ou take profit ausente no plano atual.",
                ),
            )

        stop_valid = _valid_stop(action=action, entry=entry, stop=current_stop)
        take_valid = _valid_take(action=action, entry=entry, take=current_take)

        stop_distance_pct = _distance_pct(entry, current_stop)
        take_profit_distance_pct = _distance_pct(entry, current_take)
        risk_reward = _risk_reward(
            stop_distance_pct=stop_distance_pct,
            take_profit_distance_pct=take_profit_distance_pct,
        )

        if not stop_valid or not take_valid:
            return IntelligentExitPlan(
                symbol=trade_plan.symbol,
                action=action,
                status=INTELLIGENT_EXIT_STATUS_EXIT_INVALID,
                reason="STOP_OR_TAKE_PROFIT_DIRECTION_INVALID",
                trade_plan_id=trade_plan.plan_id,
                entry_reference_price=entry,
                recommended_stop_price=current_stop,
                recommended_take_profit_price=current_take,
                current_stop_price=current_stop,
                current_take_profit_price=current_take,
                stop_distance_pct=stop_distance_pct,
                take_profit_distance_pct=take_profit_distance_pct,
                risk_reward=risk_reward,
                stop_valid=stop_valid,
                take_profit_valid=take_valid,
                protection_complete=False,
                protection_quality=PROTECTION_QUALITY_INVALID,
                diagnostics=_diagnostics(
                    architecture_mode=architecture_mode,
                    intelligent_activation_level=intelligent_activation_level,
                    dynamic_exit_enabled=dynamic_exit_enabled,
                    trade_plan_status=trade_plan.status,
                    safety_note="Direcao do stop/take parece invalida.",
                ),
            )

        adaptive = _build_adaptive_candidate(
            action=action,
            entry=entry,
            current_stop=current_stop,
            current_take=current_take,
            stop_distance_pct=stop_distance_pct,
            risk_reward=risk_reward,
        )

        return IntelligentExitPlan(
            symbol=trade_plan.symbol,
            action=action,
            status=INTELLIGENT_EXIT_STATUS_SHADOW_READY,
            reason="INTELLIGENT_EXIT_ADAPTIVE_SHADOW_ONLY",
            trade_plan_id=trade_plan.plan_id,
            entry_reference_price=entry,
            recommended_stop_price=current_stop,
            recommended_take_profit_price=current_take,
            current_stop_price=current_stop,
            current_take_profit_price=current_take,
            stop_distance_pct=stop_distance_pct,
            take_profit_distance_pct=take_profit_distance_pct,
            risk_reward=risk_reward,
            adaptive_stop_price=adaptive["adaptive_stop_price"],
            adaptive_take_profit_price=adaptive["adaptive_take_profit_price"],
            adaptive_stop_distance_pct=adaptive["adaptive_stop_distance_pct"],
            adaptive_take_profit_distance_pct=adaptive["adaptive_take_profit_distance_pct"],
            adaptive_risk_reward=adaptive["adaptive_risk_reward"],
            minimum_reward_risk=MINIMUM_REWARD_RISK,
            minimum_stop_distance_pct=MINIMUM_STOP_DISTANCE_PCT,
            recommendation_mode=adaptive["recommendation_mode"],
            protection_quality=adaptive["protection_quality"],
            adaptive_candidate_available=True,
            adaptive_changed=adaptive["adaptive_changed"],
            stop_valid=True,
            take_profit_valid=True,
            protection_complete=True,
            shadow_only=True,
            can_use_for_execution=False,
            diagnostics=_diagnostics(
                architecture_mode=architecture_mode,
                intelligent_activation_level=intelligent_activation_level,
                dynamic_exit_enabled=dynamic_exit_enabled,
                trade_plan_status=trade_plan.status,
                stop_distance_pct=_decimal_or_none(stop_distance_pct),
                take_profit_distance_pct=_decimal_or_none(take_profit_distance_pct),
                risk_reward=_decimal_or_none(risk_reward),
                adaptive_stop_price=_decimal_or_none(adaptive["adaptive_stop_price"]),
                adaptive_take_profit_price=_decimal_or_none(adaptive["adaptive_take_profit_price"]),
                adaptive_risk_reward=_decimal_or_none(adaptive["adaptive_risk_reward"]),
                minimum_reward_risk=str(MINIMUM_REWARD_RISK),
                minimum_stop_distance_pct=str(MINIMUM_STOP_DISTANCE_PCT),
                recommendation_mode=adaptive["recommendation_mode"],
                protection_quality=adaptive["protection_quality"],
                adaptive_candidate_available=True,
                adaptive_changed=adaptive["adaptive_changed"],
                safety_note=(
                    "Fase 5E: candidato adaptativo registrado em shadow. "
                    "Nao altera stop, take profit ou execucao."
                ),
            ),
        )


def _build_adaptive_candidate(
    *,
    action: str,
    entry: Decimal,
    current_stop: Decimal,
    current_take: Decimal,
    stop_distance_pct: Decimal | None,
    risk_reward: Decimal | None,
) -> dict[str, Any]:
    minimum_stop_distance = entry * MINIMUM_STOP_DISTANCE_PCT

    adaptive_stop = current_stop
    stop_was_adapted = False

    if stop_distance_pct is None or stop_distance_pct < MINIMUM_STOP_DISTANCE_PCT:
        adaptive_stop = _price_from_distance(
            action=action,
            entry=entry,
            distance=minimum_stop_distance,
            kind="stop",
        )
        stop_was_adapted = True

    adaptive_stop_distance = abs(entry - adaptive_stop)
    adaptive_take = current_take
    reward_was_adapted = False

    if risk_reward is None or risk_reward < MINIMUM_REWARD_RISK or stop_was_adapted:
        adaptive_take = _price_from_distance(
            action=action,
            entry=entry,
            distance=adaptive_stop_distance * MINIMUM_REWARD_RISK,
            kind="take",
        )
        reward_was_adapted = True

    adaptive_stop_distance_pct = _distance_pct(entry, adaptive_stop)
    adaptive_take_profit_distance_pct = _distance_pct(entry, adaptive_take)
    adaptive_risk_reward = _risk_reward(
        stop_distance_pct=adaptive_stop_distance_pct,
        take_profit_distance_pct=adaptive_take_profit_distance_pct,
    )

    adaptive_changed = adaptive_stop != current_stop or adaptive_take != current_take
    recommendation_mode = _recommendation_mode(
        stop_was_adapted=stop_was_adapted,
        reward_was_adapted=reward_was_adapted,
    )
    protection_quality = _protection_quality(
        stop_distance_pct=stop_distance_pct,
        risk_reward=risk_reward,
    )

    return {
        "adaptive_stop_price": adaptive_stop,
        "adaptive_take_profit_price": adaptive_take,
        "adaptive_stop_distance_pct": adaptive_stop_distance_pct,
        "adaptive_take_profit_distance_pct": adaptive_take_profit_distance_pct,
        "adaptive_risk_reward": adaptive_risk_reward,
        "recommendation_mode": recommendation_mode,
        "protection_quality": protection_quality,
        "adaptive_changed": adaptive_changed,
    }


def _recommendation_mode(
    *,
    stop_was_adapted: bool,
    reward_was_adapted: bool,
) -> str:
    if stop_was_adapted and reward_was_adapted:
        return RECOMMENDATION_MODE_ADAPT_STOP_AND_REWARD
    if stop_was_adapted:
        return RECOMMENDATION_MODE_ADAPT_STOP_DISTANCE
    if reward_was_adapted:
        return RECOMMENDATION_MODE_ADAPT_REWARD_RISK
    return RECOMMENDATION_MODE_KEEP_EXISTING


def _protection_quality(
    *,
    stop_distance_pct: Decimal | None,
    risk_reward: Decimal | None,
) -> str:
    if stop_distance_pct is None or risk_reward is None:
        return PROTECTION_QUALITY_INCOMPLETE
    if stop_distance_pct < MINIMUM_STOP_DISTANCE_PCT:
        return PROTECTION_QUALITY_TOO_TIGHT
    if risk_reward < MINIMUM_REWARD_RISK:
        return PROTECTION_QUALITY_WEAK
    if risk_reward >= Decimal("3"):
        return PROTECTION_QUALITY_STRONG
    return PROTECTION_QUALITY_ACCEPTABLE


def _price_from_distance(
    *,
    action: str,
    entry: Decimal,
    distance: Decimal,
    kind: str,
) -> Decimal:
    if action == "OPEN_LONG":
        if kind == "stop":
            return entry - distance
        return entry + distance

    if action == "OPEN_SHORT":
        if kind == "stop":
            return entry + distance
        return entry - distance

    return entry


def _valid_stop(*, action: str, entry: Decimal, stop: Decimal) -> bool:
    if action == "OPEN_LONG":
        return stop < entry
    if action == "OPEN_SHORT":
        return stop > entry
    return False


def _valid_take(*, action: str, entry: Decimal, take: Decimal) -> bool:
    if action == "OPEN_LONG":
        return take > entry
    if action == "OPEN_SHORT":
        return take < entry
    return False


def _distance_pct(entry: Decimal, price: Decimal) -> Decimal | None:
    try:
        return abs((price - entry) / entry)
    except (InvalidOperation, ZeroDivisionError):
        return None


def _risk_reward(
    *,
    stop_distance_pct: Decimal | None,
    take_profit_distance_pct: Decimal | None,
) -> Decimal | None:
    if stop_distance_pct is None or take_profit_distance_pct is None:
        return None
    if stop_distance_pct <= 0:
        return None
    return take_profit_distance_pct / stop_distance_pct


def _diagnostics(**items) -> dict[str, Any]:
    return {
        "shadow_only": True,
        "can_use_for_execution": False,
        **items,
    }


def _decimal_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)
