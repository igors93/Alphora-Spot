from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from futures.protection import ExpectedProtection

OPEN_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}
PROTECTED_ENTRY_BLOCK_STATES = {
    "MANUAL_RECONCILE_REQUIRED",
    "PROTECTIONS_PENDING",
    "POSITION_OPEN_UNPROTECTED",
}


@dataclass(frozen=True)
class PreExecutionGuardDecision:
    allowed: bool
    reason: str
    motivo_pt: str
    como_corrigir: str
    severity: str
    symbol: str
    action: str
    trade_plan_id: str | None
    planned_quantity: Any
    planned_stop_price: Any
    planned_take_profit_price: Any
    economic_gate_allowed: bool | None
    portfolio_allowed: bool | None
    position_state: str
    dynamic_exit_enabled: bool
    dynamic_sizing_enabled: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)


class PreExecutionGuard:
    def evaluate(
        self,
        *,
        symbol: str,
        action: str,
        trade_plan_id: str | None = None,
        planned_quantity=None,
        planned_exit: dict[str, Any] | None = None,
        final_economic_validation=None,
        portfolio_decision=None,
        position=None,
        runtime_status=None,
        dynamic_exit_enabled: bool = False,
        dynamic_sizing_enabled: bool = False,
        order_execution_plan=None,
        learning_changed_execution_payload: bool = False,
        learning_payload_revalidated: bool = False,
        market_price=None,
        mark_price=None,
        protection_working_type: str | None = None,
        expected_protection: ExpectedProtection | None = None,
        max_price_drift_bps: Decimal = Decimal("10"),
        protection_preview_error: str | None = None,
    ) -> PreExecutionGuardDecision:
        normalized_action = str(action or "").upper().strip()
        planned_stop = _planned_exit_value(planned_exit, "stop_price")
        planned_take = _planned_exit_value(planned_exit, "take_profit_price")
        economic_gate_allowed = _decision_allowed(final_economic_validation)
        portfolio_allowed = _decision_allowed(portfolio_decision)
        position_state = _position_state(position=position, runtime_status=runtime_status)
        diagnostics = {
            "guard_version": "1.0.0",
            "runtime_status": _enum_value(runtime_status),
            "position_is_open": bool(getattr(position, "is_open", False)),
            "order_execution_plan_allowed": _decision_allowed(order_execution_plan),
            "learning_changed_execution_payload": learning_changed_execution_payload,
            "learning_payload_revalidated": learning_payload_revalidated,
            "pre_entry_protection": _pre_entry_protection_diagnostics(
                action=normalized_action,
                market_price=market_price,
                mark_price=mark_price,
                protection_working_type=protection_working_type,
                expected_protection=expected_protection,
                max_price_drift_bps=max_price_drift_bps,
                protection_preview_error=protection_preview_error,
            ),
            "observational_inputs": {
                "planned_exit_available": planned_exit is not None,
                "final_economic_validation_available": (final_economic_validation is not None),
                "portfolio_decision_available": portfolio_decision is not None,
                "order_execution_plan_available": order_execution_plan is not None,
            },
        }

        if normalized_action == "CLOSE_POSITION":
            return self._decision(
                allowed=True,
                reason="CLOSE_POSITION_BYPASSES_PRE_EXECUTION_OPEN_GUARDS",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics={
                    **diagnostics,
                    "close_position_bypass": True,
                },
            )

        if normalized_action not in OPEN_ACTIONS:
            return self._decision(
                allowed=True,
                reason="ACTION_NOT_OPEN",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        runtime_status_value = _enum_value(runtime_status)
        if runtime_status_value in PROTECTED_ENTRY_BLOCK_STATES:
            return self._blocked(
                reason=runtime_status_value,
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if bool(getattr(position, "is_open", False)):
            return self._blocked(
                reason="POSITION_ALREADY_OPEN",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if learning_changed_execution_payload and not learning_payload_revalidated:
            return self._blocked(
                reason="LEARNING_PAYLOAD_REVALIDATION_REQUIRED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if final_economic_validation is None:
            return self._blocked(
                reason="FINAL_ECONOMIC_VALIDATION_REQUIRED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if economic_gate_allowed is not True:
            return self._blocked(
                reason="FINAL_ECONOMIC_GATE_NOT_APPROVED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if dynamic_exit_enabled and planned_exit is None:
            return self._blocked(
                reason="PLANNED_EXIT_REQUIRED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if dynamic_exit_enabled and planned_stop is None:
            return self._blocked(
                reason="PLANNED_STOP_PRICE_REQUIRED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if dynamic_exit_enabled and planned_take is None:
            return self._blocked(
                reason="PLANNED_TAKE_PROFIT_PRICE_REQUIRED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if dynamic_sizing_enabled and planned_quantity is None:
            return self._blocked(
                reason="PLANNED_QUANTITY_REQUIRED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if order_execution_plan is not None and _decision_allowed(order_execution_plan) is not True:
            return self._blocked(
                reason="ORDER_EXECUTION_PLAN_NOT_ALLOWED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if portfolio_decision is None:
            return self._blocked(
                reason="PORTFOLIO_DECISION_REQUIRED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        if portfolio_allowed is not True:
            return self._blocked(
                reason="PORTFOLIO_NOT_APPROVED",
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=planned_stop,
                planned_take_profit_price=planned_take,
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        protection_block_reason = _pre_entry_protection_block_reason(
            action=normalized_action,
            market_price=market_price,
            mark_price=mark_price,
            protection_working_type=protection_working_type,
            expected_protection=expected_protection,
            max_price_drift_bps=max_price_drift_bps,
            protection_preview_error=protection_preview_error,
        )
        if protection_block_reason is not None:
            return self._blocked(
                reason=protection_block_reason,
                symbol=symbol,
                action=normalized_action,
                trade_plan_id=trade_plan_id,
                planned_quantity=planned_quantity,
                planned_stop_price=(
                    expected_protection.stop_price
                    if expected_protection is not None
                    else planned_stop
                ),
                planned_take_profit_price=(
                    expected_protection.take_profit_price
                    if expected_protection is not None
                    else planned_take
                ),
                economic_gate_allowed=economic_gate_allowed,
                portfolio_allowed=portfolio_allowed,
                position_state=position_state,
                dynamic_exit_enabled=dynamic_exit_enabled,
                dynamic_sizing_enabled=dynamic_sizing_enabled,
                diagnostics=diagnostics,
            )

        return self._decision(
            allowed=True,
            reason="PRE_EXECUTION_GUARD_APPROVED",
            symbol=symbol,
            action=normalized_action,
            trade_plan_id=trade_plan_id,
            planned_quantity=planned_quantity,
            planned_stop_price=planned_stop,
            planned_take_profit_price=planned_take,
            economic_gate_allowed=economic_gate_allowed,
            portfolio_allowed=portfolio_allowed,
            position_state=position_state,
            dynamic_exit_enabled=dynamic_exit_enabled,
            dynamic_sizing_enabled=dynamic_sizing_enabled,
            diagnostics=diagnostics,
        )

    def _blocked(self, **payload: Any) -> PreExecutionGuardDecision:
        return self._decision(allowed=False, **payload)

    def _decision(self, **payload: Any) -> PreExecutionGuardDecision:
        reason = str(payload.get("reason", "") or "")
        allowed = bool(payload.get("allowed", False))
        details = _reason_details(reason=reason, allowed=allowed)
        payload.setdefault("motivo_pt", details["motivo_pt"])
        payload.setdefault("como_corrigir", details["como_corrigir"])
        payload.setdefault("severity", details["severity"])
        return PreExecutionGuardDecision(**payload)


def _decimal_or_none(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _price_drift_bps(*, market_price: Decimal | None, mark_price: Decimal | None) -> Decimal | None:
    if market_price is None or mark_price is None or market_price <= 0 or mark_price <= 0:
        return None
    return abs(mark_price - market_price) / market_price * Decimal("10000")


def _breached_protection_triggers(
    *,
    action: str,
    current_price: Decimal,
    expected_protection: ExpectedProtection,
) -> list[str]:
    breached: list[str] = []
    if action == "OPEN_LONG":
        if current_price <= expected_protection.stop_price:
            breached.append("STOP_MARKET")
        if current_price >= expected_protection.take_profit_price:
            breached.append("TAKE_PROFIT_MARKET")
    elif action == "OPEN_SHORT":
        if current_price >= expected_protection.stop_price:
            breached.append("STOP_MARKET")
        if current_price <= expected_protection.take_profit_price:
            breached.append("TAKE_PROFIT_MARKET")
    return breached


def _pre_entry_protection_diagnostics(
    *,
    action: str,
    market_price,
    mark_price,
    protection_working_type: str | None,
    expected_protection: ExpectedProtection | None,
    max_price_drift_bps: Decimal,
    protection_preview_error: str | None,
) -> dict[str, Any]:
    market = _decimal_or_none(market_price)
    mark = _decimal_or_none(mark_price)
    max_drift = _decimal_or_none(max_price_drift_bps)
    working_type = _enum_value(
        expected_protection.working_type
        if expected_protection is not None
        else protection_working_type
    )
    trigger_price = mark if working_type == "MARK_PRICE" else market
    breached = (
        _breached_protection_triggers(
            action=action,
            current_price=trigger_price,
            expected_protection=expected_protection,
        )
        if expected_protection is not None and trigger_price is not None and trigger_price > 0
        else []
    )
    return {
        "preview_available": expected_protection is not None,
        "preview_error": protection_preview_error,
        "working_type": working_type or None,
        "market_price": market,
        "mark_price": mark,
        "trigger_reference_price": trigger_price,
        "stop_price": (expected_protection.stop_price if expected_protection is not None else None),
        "take_profit_price": (
            expected_protection.take_profit_price if expected_protection is not None else None
        ),
        "protection_model": (
            expected_protection.model if expected_protection is not None else None
        ),
        "protection_source": (
            expected_protection.source if expected_protection is not None else None
        ),
        "price_drift_bps": _price_drift_bps(market_price=market, mark_price=mark),
        "max_price_drift_bps": max_drift,
        "breached_triggers": breached,
    }


def _pre_entry_protection_block_reason(
    *,
    action: str,
    market_price,
    mark_price,
    protection_working_type: str | None,
    expected_protection: ExpectedProtection | None,
    max_price_drift_bps: Decimal,
    protection_preview_error: str | None,
) -> str | None:
    if action not in OPEN_ACTIONS:
        return None
    if protection_preview_error or expected_protection is None:
        return "PRE_ENTRY_PROTECTION_UNAVAILABLE"

    market = _decimal_or_none(market_price)
    mark = _decimal_or_none(mark_price)
    if market is None or market <= 0:
        return "PROTECTION_TRIGGER_REFERENCE_PRICE_UNAVAILABLE"

    configured_working_type = _enum_value(protection_working_type)
    effective_working_type = _enum_value(expected_protection.working_type)
    if configured_working_type and configured_working_type != effective_working_type:
        return "PROTECTION_WORKING_TYPE_MISMATCH"
    if effective_working_type not in {"MARK_PRICE", "CONTRACT_PRICE"}:
        return "PROTECTION_WORKING_TYPE_UNSUPPORTED"

    trigger_price = mark if effective_working_type == "MARK_PRICE" else market
    if trigger_price is None or trigger_price <= 0:
        return "PROTECTION_TRIGGER_REFERENCE_PRICE_UNAVAILABLE"

    stop = expected_protection.stop_price
    take = expected_protection.take_profit_price
    if action == "OPEN_LONG" and not (stop < market < take):
        return "PROTECTION_PRICES_INVALID_FOR_ENTRY"
    if action == "OPEN_SHORT" and not (take < market < stop):
        return "PROTECTION_PRICES_INVALID_FOR_ENTRY"

    if _breached_protection_triggers(
        action=action,
        current_price=trigger_price,
        expected_protection=expected_protection,
    ):
        return "PROTECTION_TRIGGER_ALREADY_BREACHED_PRE_ENTRY"

    if effective_working_type == "MARK_PRICE":
        drift = _price_drift_bps(market_price=market, mark_price=mark)
        max_drift = _decimal_or_none(max_price_drift_bps)
        if drift is None or max_drift is None or max_drift < 0:
            return "MARK_CONTRACT_PRICE_DRIFT_UNAVAILABLE"
        if drift > max_drift:
            return "MARK_CONTRACT_PRICE_DRIFT_TOO_HIGH"

    return None


def _decision_allowed(value) -> bool | None:
    if value is None:
        return None
    decision = getattr(value, "decision", None)
    if decision is not None:
        return bool(getattr(decision, "allowed", False))
    if hasattr(value, "allowed"):
        return bool(value.allowed)
    return None


def _planned_exit_value(planned_exit: dict[str, Any] | None, key: str):
    if planned_exit is None:
        return None
    return planned_exit.get(key)


def _position_state(*, position, runtime_status) -> str:
    status = _enum_value(runtime_status)
    if status:
        return status
    if position is None:
        return "UNKNOWN"
    direction = _enum_value(getattr(position, "direction", None))
    if bool(getattr(position, "is_open", False)):
        return f"OPEN_{direction or 'UNKNOWN'}"
    return "FLAT"


def _enum_value(value) -> str:
    return str(getattr(value, "value", value) or "").upper().strip()


def _reason_details(*, reason: str, allowed: bool) -> dict[str, str]:
    messages = {
        "FINAL_ECONOMIC_VALIDATION_REQUIRED": (
            "A validacao economica final nao foi encontrada antes da execucao.",
            "Verifique se o Economic Gate final foi executado depois das camadas inteligentes e antes da execucao.",
            "ERROR",
        ),
        "FINAL_ECONOMIC_GATE_NOT_APPROVED": (
            "O plano final nao foi aprovado pelo Economic Gate.",
            "Verifique reward/risk liquido, custos, stop, take e diagnostico economico.",
            "WARNING",
        ),
        "PLANNED_EXIT_REQUIRED": (
            "A operacao de entrada nao possui plano de saida.",
            "Verifique se o TradePlan gerou stop e take antes da execucao.",
            "WARNING",
        ),
        "PLANNED_STOP_PRICE_REQUIRED": (
            "A entrada nao possui stop planejado.",
            "Verifique ExitPlanner, dynamic_exit_enabled e criacao de protecoes.",
            "WARNING",
        ),
        "PLANNED_TAKE_PROFIT_PRICE_REQUIRED": (
            "A entrada nao possui take profit planejado.",
            "Verifique ExitPlanner, calculo economico e limites de take.",
            "WARNING",
        ),
        "PLANNED_QUANTITY_REQUIRED": (
            "A entrada nao possui quantidade planejada.",
            "Verifique PositionSizer, dynamic_sizing_enabled e regras da Binance.",
            "WARNING",
        ),
        "PORTFOLIO_DECISION_REQUIRED": (
            "A decisao de risco do portfolio nao foi encontrada.",
            "Verifique PortfolioRiskEngine antes da execucao.",
            "ERROR",
        ),
        "PORTFOLIO_NOT_APPROVED": (
            "O portfolio nao aprovou a operacao.",
            "Verifique exposicao, limite de risco, saldo e numero maximo de posicoes.",
            "WARNING",
        ),
        "POSITION_ALREADY_OPEN": (
            "Ja existe posicao aberta para este simbolo.",
            "Aguarde fechamento ou reconcilie o estado antes de nova entrada.",
            "WARNING",
        ),
        "MANUAL_RECONCILE_REQUIRED": (
            "O simbolo exige reconciliacao manual.",
            "Abra o status/runtime e resolva divergencias antes de operar.",
            "WARNING",
        ),
        "PROTECTIONS_PENDING": (
            "Ha protecoes pendentes para este simbolo.",
            "Aguarde criacao de stop/take ou verifique falha de protecao.",
            "WARNING",
        ),
        "POSITION_OPEN_UNPROTECTED": (
            "Existe posicao aberta sem protecao completa.",
            "Corrija stop/take antes de permitir nova entrada.",
            "WARNING",
        ),
        "LEARNING_PAYLOAD_REVALIDATION_REQUIRED": (
            "O learning alterou o payload, mas ele nao foi revalidado economicamente.",
            "Revalide o Economic Gate apos qualquer alteracao de quantidade ou saida.",
            "ERROR",
        ),
        "ORDER_EXECUTION_PLAN_NOT_ALLOWED": (
            "O plano de execucao de ordem nao permitiu executar agora.",
            "Verifique stale data, price drift e politica de execucao.",
            "WARNING",
        ),
        "PRE_ENTRY_PROTECTION_UNAVAILABLE": (
            "A protecao efetiva nao pôde ser calculada antes da entrada.",
            "Verifique regras do simbolo, modelo de saida e configuracao de stop/take.",
            "ERROR",
        ),
        "PROTECTION_TRIGGER_REFERENCE_PRICE_UNAVAILABLE": (
            "O preco de referencia da protecao nao esta disponivel.",
            "Atualize market price/mark price antes de enviar a ordem.",
            "ERROR",
        ),
        "PROTECTION_WORKING_TYPE_MISMATCH": (
            "O working type calculado diverge da configuracao efetiva.",
            "Alinhe MARK_PRICE ou CONTRACT_PRICE entre Governor e executor.",
            "ERROR",
        ),
        "PROTECTION_WORKING_TYPE_UNSUPPORTED": (
            "O working type da protecao nao e suportado pelo guard.",
            "Use MARK_PRICE ou CONTRACT_PRICE.",
            "ERROR",
        ),
        "PROTECTION_PRICES_INVALID_FOR_ENTRY": (
            "Stop ou take esta no lado incorreto do preco de entrada.",
            "Revise o calculo e arredondamento da protecao efetiva.",
            "ERROR",
        ),
        "PROTECTION_TRIGGER_ALREADY_BREACHED_PRE_ENTRY": (
            "A protecao ja estaria disparada antes da ordem de entrada.",
            "Aguarde convergencia dos precos ou recalcule stop/take com dados atuais.",
            "WARNING",
        ),
        "MARK_CONTRACT_PRICE_DRIFT_UNAVAILABLE": (
            "Nao foi possivel validar a divergencia entre contract price e mark price.",
            "Atualize os dois precos antes da entrada.",
            "ERROR",
        ),
        "MARK_CONTRACT_PRICE_DRIFT_TOO_HIGH": (
            "A divergencia entre contract price e mark price excede o limite seguro.",
            "Aguarde estabilidade dos precos antes de abrir a posicao.",
            "WARNING",
        ),
        "PRE_EXECUTION_GUARD_APPROVED": (
            "Payload final aprovado para execucao.",
            "",
            "INFO",
        ),
        "CLOSE_POSITION_BYPASSES_PRE_EXECUTION_OPEN_GUARDS": (
            "Fechamento permitido sem travas de abertura.",
            "",
            "INFO",
        ),
    }
    if reason in messages:
        motivo_pt, como_corrigir, severity = messages[reason]
        return {
            "motivo_pt": motivo_pt,
            "como_corrigir": como_corrigir,
            "severity": severity,
        }

    return {
        "motivo_pt": (
            "Payload final aprovado para execucao."
            if allowed
            else "O PreExecutionGuard bloqueou a execucao."
        ),
        "como_corrigir": "" if allowed else "Verifique os diagnosticos do guard.",
        "severity": "INFO" if allowed else "WARNING",
    }
