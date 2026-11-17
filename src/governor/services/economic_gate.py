"""Economic gate of a trade plan: initial, intelligent-exit candidate and final validations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from config.symbol_context import SymbolTradingConfig
from futures.planning.economic_gate import EconomicGateDecision, EconomicTradeGate
from futures.planning.trade_plan_execution_validator import (
    TradePlanExecutionValidation,
    TradePlanExecutionValidator,
)
from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.events import (
    CANDIDATE_NET_GAIN_BELOW_ECONOMIC_TRADE_PLAN,
    CANDIDATE_REWARD_RISK_BELOW_ECONOMIC_TRADE_PLAN,
    ECONOMIC_GATE_EVALUATED_FINAL,
    ECONOMIC_GATE_EVALUATED_INITIAL,
    INTELLIGENT_EXIT_CANDIDATE_EVALUATED,
    INTELLIGENT_EXIT_CANDIDATE_REJECTED_BY_ECONOMIC_GATE,
    PLANNED_EXIT_RESTORED_TO_ECONOMIC_TRADE_PLAN,
    TRADE_BLOCKED_BY_FINAL_ECONOMIC_GATE,
)
from governor.governor_trade_helpers import (
    EXECUTION_OPEN_ACTIONS,
    _ai_signal_audit_fields,
    _decimal_or_none,
    _intelligent_activation_context,
    _price_distance_bps,
)

TracePayload = Callable[[], Mapping[str, str]]
PlannedExit = dict[str, Any] | None

RESTORED_AFTER_INTELLIGENT_EXIT_REJECTED = (
    "ECONOMIC_TRADE_PLAN_RESTORED_AFTER_INTELLIGENT_EXIT_REJECTED"
)


def economic_gate_event_payload(
    decision: EconomicGateDecision,
    validation: TradePlanExecutionValidation | None = None,
) -> dict[str, Any]:
    """The journal payload of an economic decision, with the execution validation if any."""

    diagnostics = dict(decision.diagnostics)
    if validation is not None:
        diagnostics = {
            **diagnostics,
            "execution_validation": validation.diagnostics,
        }
    raw_exit_diagnostics = diagnostics.get("exit_diagnostics", {})
    exit_diagnostics = raw_exit_diagnostics if isinstance(raw_exit_diagnostics, dict) else {}
    final_take_bps = exit_diagnostics.get("final_take_bps")
    final_stop_bps = exit_diagnostics.get("final_stop_bps")
    if validation is not None and validation.effective_plan is not None:
        entry_price = validation.effective_plan.entry_reference_price
        final_take_bps = (
            _price_distance_bps(validation.final_take_profit_price, entry_price) or final_take_bps
        )
        final_stop_bps = (
            _price_distance_bps(validation.final_stop_price, entry_price) or final_stop_bps
        )
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "block_reason": diagnostics.get("plan_block_reason"),
        "plan_id": decision.plan_id,
        "trade_plan_symbol": decision.symbol,
        "original_stop_price": validation.original_stop_price if validation is not None else None,
        "original_take_profit_price": (
            validation.original_take_profit_price if validation is not None else None
        ),
        "final_stop_price": validation.final_stop_price if validation is not None else None,
        "final_take_profit_price": (
            validation.final_take_profit_price if validation is not None else None
        ),
        "original_quantity": validation.original_quantity if validation is not None else None,
        "final_quantity": validation.final_quantity if validation is not None else None,
        "expected_net_gain": decision.expected_net_gain,
        "expected_net_gain_bps": decision.expected_net_gain_bps,
        "expected_net_loss": decision.expected_net_loss,
        "reward_risk": decision.reward_risk,
        "round_trip_cost": decision.round_trip_cost,
        "round_trip_cost_bps": decision.round_trip_cost_bps,
        "minimum_profitable_move": decision.minimum_profitable_move,
        "configured_stop_bps": exit_diagnostics.get("configured_stop_bps"),
        "configured_take_bps": exit_diagnostics.get("configured_take_bps"),
        "required_take_bps": exit_diagnostics.get("required_take_bps"),
        "final_take_bps": final_take_bps,
        "final_stop_bps": final_stop_bps,
        "min_net_reward_risk": exit_diagnostics.get("min_net_reward_risk"),
        "min_net_gain_bps": exit_diagnostics.get("min_net_gain_bps"),
        "exit_model_requested": exit_diagnostics.get("exit_model_requested"),
        "exit_model_effective": exit_diagnostics.get("exit_model_effective"),
        "economic_exit_adjusted": exit_diagnostics.get("economic_exit_adjusted"),
        "economic_exit_adjustment_reason": exit_diagnostics.get("economic_exit_adjustment_reason"),
        "economic_exit_blocked_unrealistic_take": exit_diagnostics.get(
            "economic_exit_blocked_unrealistic_take"
        ),
        "diagnostics": diagnostics,
    }


def intelligent_exit_candidate_rejection_reason(
    *,
    candidate_validation: TradePlanExecutionValidation,
    baseline_validation: TradePlanExecutionValidation,
) -> str | None:
    """Why an intelligent exit must not replace the economic trade plan exit, if it must not."""

    if not candidate_validation.decision.allowed:
        return candidate_validation.decision.reason

    if not baseline_validation.decision.allowed:
        return None

    candidate_reward_risk = _decimal_or_none(candidate_validation.decision.reward_risk)
    baseline_reward_risk = _decimal_or_none(baseline_validation.decision.reward_risk)
    if (
        candidate_reward_risk is None
        or baseline_reward_risk is None
        or candidate_reward_risk < baseline_reward_risk
    ):
        return CANDIDATE_REWARD_RISK_BELOW_ECONOMIC_TRADE_PLAN

    candidate_gain_bps = _decimal_or_none(candidate_validation.decision.expected_net_gain_bps)
    baseline_gain_bps = _decimal_or_none(baseline_validation.decision.expected_net_gain_bps)
    if (
        candidate_gain_bps is not None
        and baseline_gain_bps is not None
        and candidate_gain_bps < baseline_gain_bps
    ):
        return CANDIDATE_NET_GAIN_BELOW_ECONOMIC_TRADE_PLAN

    return None


def _plan_of(trade_pipeline_result: Any) -> Any:
    return trade_pipeline_result.plan if trade_pipeline_result is not None else None


def _price(planned_exit: PlannedExit, key: str) -> Any:
    return planned_exit.get(key) if planned_exit is not None else None


class EconomicGateService:
    """Run the economic gate and journal every decision it takes on a trade plan.

    The gate and its execution validator come from the composition root; when a
    test replaces the gate, a validator bound to that gate is built on the fly.
    """

    def __init__(
        self,
        *,
        config: Any,
        gate: EconomicTradeGate | None,
        validator: TradePlanExecutionValidator | None,
        journal: EventJournal,
        outcomes: StageOutcomes,
        logger: Any = None,
        trace_payload: TracePayload,
    ) -> None:
        self.config = config
        self.gate = gate if gate is not None else EconomicTradeGate.from_config(config)
        if validator is None or getattr(validator, "gate", None) is not self.gate:
            validator = TradePlanExecutionValidator(self.gate)
        self.validator = validator
        self.journal = journal
        self.outcomes = outcomes
        self.logger = logger
        self.trace_payload = trace_payload

    def evaluate(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        trade_pipeline_result: Any,
        stage: str,
    ) -> EconomicGateDecision:
        decision = self.gate.evaluate(_plan_of(trade_pipeline_result), action)
        payload = economic_gate_event_payload(decision)
        stage = str(stage or "initial").lower().strip()
        event_type = {
            "initial": ECONOMIC_GATE_EVALUATED_INITIAL,
            "final": ECONOMIC_GATE_EVALUATED_FINAL,
        }.get(stage, f"economic_gate_evaluated_{stage}")
        self.journal.append(
            event_type,
            **self.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            **payload,
        )
        self.journal.append(
            "economic_gate_evaluated",
            **self.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            stage=stage,
            **payload,
        )

        if action in EXECUTION_OPEN_ACTIONS and decision.allowed:
            self.journal.append(
                "trade_allowed_by_economic_gate",
                symbol=symbol_config.symbol,
                action=action,
                **payload,
            )

        return decision

    def validate_execution_payload(
        self,
        *,
        trade_pipeline_result: Any,
        action: str,
        planned_quantity: Any,
        planned_exit: PlannedExit,
    ) -> TradePlanExecutionValidation:
        return self.validator.evaluate(
            plan=_plan_of(trade_pipeline_result),
            action=action,
            planned_quantity=planned_quantity,
            planned_exit=planned_exit,
        )

    # --- intelligent exit candidate ---------------------------------------------------

    def apply_intelligent_exit_candidate_if_economic(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        trade_pipeline_result: Any,
        planned_quantity: Any,
        current_planned_exit: PlannedExit,
        current_planned_exit_reason: str,
        candidate_exit: PlannedExit,
        candidate_reason: str,
        candidate_source: str,
        activation_decision: Any,
    ) -> tuple[PlannedExit, str]:
        """Keep the intelligent exit only if it is at least as economic as the plan's exit."""

        if action not in EXECUTION_OPEN_ACTIONS or candidate_exit is None:
            return current_planned_exit, current_planned_exit_reason

        candidate = self.validate_execution_payload(
            trade_pipeline_result=trade_pipeline_result,
            action=action,
            planned_quantity=planned_quantity,
            planned_exit=candidate_exit,
        )
        baseline = self.validate_execution_payload(
            trade_pipeline_result=trade_pipeline_result,
            action=action,
            planned_quantity=planned_quantity,
            planned_exit=current_planned_exit,
        )
        rejection_reason = intelligent_exit_candidate_rejection_reason(
            candidate_validation=candidate,
            baseline_validation=baseline,
        )
        comparison = _CandidateComparison(
            symbol=symbol_config.symbol,
            action=action,
            candidate_exit=candidate_exit,
            candidate_source=candidate_source,
            candidate=candidate,
            baseline=baseline,
            activation=_intelligent_activation_context(activation_decision),
        )
        self._record_candidate_evaluated(
            comparison,
            candidate_reason=candidate_reason,
            current_planned_exit=current_planned_exit,
            current_planned_exit_reason=current_planned_exit_reason,
            rejection_reason=rejection_reason,
        )

        if rejection_reason is None:
            return candidate_exit, candidate_reason

        self._record_candidate_rejected(comparison, rejection_reason=rejection_reason)
        if baseline.decision.allowed:
            self._record_exit_restored(
                comparison,
                rejection_reason=rejection_reason,
                current_planned_exit=current_planned_exit,
                current_planned_exit_reason=current_planned_exit_reason,
            )
            return current_planned_exit, RESTORED_AFTER_INTELLIGENT_EXIT_REJECTED

        return candidate_exit, candidate_reason

    def _record_candidate_evaluated(
        self,
        comparison: _CandidateComparison,
        *,
        candidate_reason: str,
        current_planned_exit: PlannedExit,
        current_planned_exit_reason: str,
        rejection_reason: str | None,
    ) -> None:
        candidate = comparison.candidate.decision
        baseline = comparison.baseline.decision
        self.journal.append(
            INTELLIGENT_EXIT_CANDIDATE_EVALUATED,
            **self.trace_payload(),
            symbol=comparison.symbol,
            action=comparison.action,
            candidate_source=comparison.candidate_source,
            candidate_reason=candidate_reason,
            candidate_stop_price=_price(comparison.candidate_exit, "stop_price"),
            candidate_take_profit_price=_price(comparison.candidate_exit, "take_profit_price"),
            candidate_allowed=candidate.allowed,
            candidate_block_reason=candidate.reason,
            candidate_expected_net_gain=candidate.expected_net_gain,
            candidate_expected_net_gain_bps=candidate.expected_net_gain_bps,
            candidate_expected_net_loss=candidate.expected_net_loss,
            candidate_reward_risk=candidate.reward_risk,
            baseline_reason=current_planned_exit_reason,
            baseline_stop_price=_price(current_planned_exit, "stop_price"),
            baseline_take_profit_price=_price(current_planned_exit, "take_profit_price"),
            baseline_allowed=baseline.allowed,
            baseline_block_reason=baseline.reason,
            baseline_expected_net_gain=baseline.expected_net_gain,
            baseline_expected_net_gain_bps=baseline.expected_net_gain_bps,
            baseline_expected_net_loss=baseline.expected_net_loss,
            baseline_reward_risk=baseline.reward_risk,
            accepted=rejection_reason is None,
            rejection_reason=rejection_reason,
            **comparison.activation_fields(),
            diagnostics={
                "candidate": comparison.candidate_payload(),
                "baseline": comparison.baseline_payload(),
                "activation": comparison.activation,
            },
        )

    def _record_candidate_rejected(
        self,
        comparison: _CandidateComparison,
        *,
        rejection_reason: str,
    ) -> None:
        candidate = comparison.candidate.decision
        baseline = comparison.baseline.decision
        self.journal.append(
            INTELLIGENT_EXIT_CANDIDATE_REJECTED_BY_ECONOMIC_GATE,
            **self.trace_payload(),
            symbol=comparison.symbol,
            action=comparison.action,
            reason=rejection_reason,
            candidate_source=comparison.candidate_source,
            candidate_stop_price=_price(comparison.candidate_exit, "stop_price"),
            candidate_take_profit_price=_price(comparison.candidate_exit, "take_profit_price"),
            candidate_allowed=candidate.allowed,
            candidate_block_reason=candidate.reason,
            candidate_reward_risk=candidate.reward_risk,
            candidate_expected_net_gain=candidate.expected_net_gain,
            candidate_expected_net_loss=candidate.expected_net_loss,
            baseline_allowed=baseline.allowed,
            baseline_block_reason=baseline.reason,
            baseline_reward_risk=baseline.reward_risk,
            **comparison.activation_fields(),
            diagnostics={
                "candidate": comparison.candidate_payload(),
                "baseline": comparison.baseline_payload(),
                "activation": comparison.activation,
            },
        )

    def _record_exit_restored(
        self,
        comparison: _CandidateComparison,
        *,
        rejection_reason: str,
        current_planned_exit: PlannedExit,
        current_planned_exit_reason: str,
    ) -> None:
        baseline = comparison.baseline.decision
        self.journal.append(
            PLANNED_EXIT_RESTORED_TO_ECONOMIC_TRADE_PLAN,
            **self.trace_payload(),
            symbol=comparison.symbol,
            action=comparison.action,
            reason=RESTORED_AFTER_INTELLIGENT_EXIT_REJECTED,
            rejected_reason=rejection_reason,
            candidate_source=comparison.candidate_source,
            candidate_stop_price=_price(comparison.candidate_exit, "stop_price"),
            candidate_take_profit_price=_price(comparison.candidate_exit, "take_profit_price"),
            restored_stop_price=_price(current_planned_exit, "stop_price"),
            restored_take_profit_price=_price(current_planned_exit, "take_profit_price"),
            restored_exit_reason=current_planned_exit_reason,
            restored_reward_risk=baseline.reward_risk,
            restored_expected_net_gain=baseline.expected_net_gain,
            restored_expected_net_loss=baseline.expected_net_loss,
            **comparison.activation_fields(),
            diagnostics={
                "candidate": comparison.candidate_payload(),
                "restored": comparison.baseline_payload(),
                "activation": comparison.activation,
            },
        )

    # --- final gate (stage 18) ---------------------------------------------------------

    def evaluate_final(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        score: Any,
        trade_pipeline_result: Any,
        planned_quantity: Any,
        planned_exit: PlannedExit,
    ) -> TradePlanExecutionValidation:
        validation = self.validate_execution_payload(
            trade_pipeline_result=trade_pipeline_result,
            action=action,
            planned_quantity=planned_quantity,
            planned_exit=planned_exit,
        )
        allowed = validation.decision.allowed
        self.journal.append(
            ECONOMIC_GATE_EVALUATED_FINAL,
            **self.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            **economic_gate_event_payload(validation.decision, validation=validation),
            **_ai_signal_audit_fields(
                score,
                final_action=action if allowed else "BLOCKED",
                block_reason=None if allowed else validation.decision.reason,
            ),
        )
        return validation

    def record_final_block(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str,
        score: Any,
        validation: TradePlanExecutionValidation,
    ) -> None:
        self.journal.append(
            TRADE_BLOCKED_BY_FINAL_ECONOMIC_GATE,
            **self.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            **economic_gate_event_payload(validation.decision, validation=validation),
            **_ai_signal_audit_fields(
                score,
                final_action="BLOCKED",
                block_reason=validation.decision.reason,
            ),
        )
        self.logger.warning(
            "Trade bloqueado pelo Economic Gate final | symbol=%s | action=%s | motivo=%s",
            symbol_config.symbol,
            action,
            validation.decision.reason,
        )
        self.outcomes.board.mark_blocked(symbol_config.symbol, validation.decision.reason)
        self.outcomes.record_flow_decision(
            symbol=symbol_config.symbol,
            stage="FINAL_ECONOMIC_GATE",
            classification="economico",
            reason=validation.decision.reason,
            terminal=True,
            execution_will_run=False,
            action=action,
        )


class _CandidateComparison:
    """An intelligent exit candidate validated next to the trade plan's own exit."""

    def __init__(
        self,
        *,
        symbol: str,
        action: str,
        candidate_exit: PlannedExit,
        candidate_source: str,
        candidate: TradePlanExecutionValidation,
        baseline: TradePlanExecutionValidation,
        activation: dict[str, Any],
    ) -> None:
        self.symbol = symbol
        self.action = action
        self.candidate_exit = candidate_exit
        self.candidate_source = candidate_source
        self.candidate = candidate
        self.baseline = baseline
        self.activation = activation

    def candidate_payload(self) -> dict[str, Any]:
        return economic_gate_event_payload(self.candidate.decision, validation=self.candidate)

    def baseline_payload(self) -> dict[str, Any]:
        return economic_gate_event_payload(self.baseline.decision, validation=self.baseline)

    def activation_fields(self) -> dict[str, Any]:
        return {
            "exit_source": self.activation.get("exit_source"),
            "used_adaptive_exit": self.activation.get("used_adaptive_exit"),
            "adaptive_exit_applied": self.activation.get("adaptive_exit_applied"),
        }
