"""Learning layer: reports, advisory and the shadow, paper and testnet execution policies."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from config.symbol_context import SymbolTradingConfig
from governor.evaluation.outcomes import EventJournal
from trading.learning_advisory import LearningAdvisoryEvaluation
from trading.learning_execution_shadow import LearningExecutionShadowDecision
from trading.learning_report import load_latest_learning_report
from trading.learning_shadow_impact_report import load_latest_learning_shadow_impact_report
from trading.learning_trade_plan import (
    attach_learning_advisory_to_trade_plan,
    attach_learning_guard_to_trade_plan,
)


@dataclass
class LearningReportState:
    """What the learning reports last saw, kept across cycles by the Governor."""

    last_report_total_outcomes: int = 0
    last_shadow_impact_total_decisions: int = 0
    last_shadow_impact_matched_outcomes: int = 0
    latest_report_payload: dict[str, Any] | None = None
    latest_shadow_impact_report_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class LearningPolicies:
    """The optional learning collaborators; ``None`` turns that layer off."""

    reporter: Any = None
    shadow_impact_reporter: Any = None
    advisory: Any = None
    plan_guard: Any = None
    execution_shadow_policy: Any = None
    paper_execution_policy: Any = None
    testnet_observation_policy: Any = None
    testnet_execution_policy: Any = None


class LearningService:
    """Record the learning reports and evaluate the learning policies of one trade.

    Every evaluation is fail-safe: an error is journaled and the trade continues
    without the learning layer. Only the paper and testnet execution policies can
    reduce or block a trade, and only where their activation settings allow it.
    """

    def __init__(
        self,
        *,
        journal: EventJournal,
        policies: LearningPolicies,
        state: LearningReportState,
    ) -> None:
        self.journal = journal
        self.policies = policies
        self.state = state

    def _journal_path(self) -> Any:
        return getattr(self.journal, "path", None)

    # --- cycle reports ------------------------------------------------------------------

    def record_recommendations(self) -> None:
        reporter = self.policies.reporter
        if reporter is None:
            return

        try:
            reporter.output_journal = self.journal
            report = reporter.record_if_changed(
                previous_total_outcomes=self.state.last_report_total_outcomes,
            )
            if report is None:
                return

            self.state.last_report_total_outcomes = report.total_outcomes
            self.state.latest_report_payload = {
                "status": report.status,
                "total_outcomes": report.total_outcomes,
                "can_use_for_execution": report.can_use_for_execution,
                "recommendations": [
                    asdict(recommendation) for recommendation in report.recommendations
                ],
                "diagnostics": report.diagnostics,
            }
        except Exception as error:
            self.journal.append(
                "learning_recommendation_report_failed",
                reason=str(error),
            )

    def record_shadow_impact_report(self) -> None:
        reporter = self.policies.shadow_impact_reporter
        if reporter is None:
            return

        journal_path = self._journal_path()
        if journal_path is None:
            return

        try:
            reporter.source_journal_path = journal_path
            reporter.output_journal = self.journal
            summary = reporter.record_if_changed(
                previous_total_shadow_decisions=self.state.last_shadow_impact_total_decisions,
                previous_matched_outcomes=self.state.last_shadow_impact_matched_outcomes,
            )
            if summary is None:
                return

            self.state.last_shadow_impact_total_decisions = summary.total_shadow_decisions
            self.state.last_shadow_impact_matched_outcomes = summary.matched_outcomes
            self.state.latest_shadow_impact_report_payload = None
        except Exception as error:
            self.journal.append(
                "learning_shadow_impact_report_failed",
                reason=str(error),
            )

    def latest_report_for_advisory(self) -> dict[str, Any] | None:
        if self.state.latest_report_payload is not None:
            return self.state.latest_report_payload

        journal_path = self._journal_path()
        if journal_path is None:
            return None

        self.state.latest_report_payload = load_latest_learning_report(journal_path)
        return self.state.latest_report_payload

    def latest_shadow_impact_report(self) -> dict[str, Any] | None:
        if self.state.latest_shadow_impact_report_payload is not None:
            return self.state.latest_shadow_impact_report_payload

        journal_path = self._journal_path()
        if journal_path is None:
            return None

        self.state.latest_shadow_impact_report_payload = load_latest_learning_shadow_impact_report(
            journal_path
        )
        return self.state.latest_shadow_impact_report_payload

    # --- advisory -----------------------------------------------------------------------

    def record_advisory_shadow(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        action: str | None,
    ) -> LearningAdvisoryEvaluation | None:
        advisory = self.policies.advisory
        if advisory is None:
            return None

        try:
            evaluation: LearningAdvisoryEvaluation = advisory.evaluate(
                symbol=symbol_config.symbol,
                action=action,
                report_payload=self.latest_report_for_advisory(),
            )
            self.journal.append(
                "learning_advisory_shadow_evaluated",
                symbol=symbol_config.symbol,
                action=action,
                status=evaluation.status,
                severity=evaluation.severity,
                reason=evaluation.reason,
                report_status=evaluation.report_status,
                total_outcomes=evaluation.total_outcomes,
                recommendations_count=evaluation.recommendations_count,
                relevant_recommendations_count=evaluation.relevant_recommendations_count,
                would_block_trade=evaluation.would_block_trade,
                would_reduce_priority=evaluation.would_reduce_priority,
                can_use_for_execution=evaluation.can_use_for_execution,
                shadow_only=evaluation.shadow_only,
                advisory=evaluation,
                diagnostics=evaluation.diagnostics,
            )
            return evaluation
        except Exception as error:
            self.journal.append(
                "learning_advisory_shadow_failed",
                symbol=symbol_config.symbol,
                action=action,
                reason=str(error),
            )
            return None

    def attach_advisory_to_trade_plan(
        self,
        *,
        result: Any,
        learning_advisory: LearningAdvisoryEvaluation | None,
    ) -> Any:
        if learning_advisory is None:
            return result

        updated_plan = attach_learning_advisory_to_trade_plan(
            plan=result.plan,
            advisory=learning_advisory,
        )

        learning_guard = None
        plan_guard = self.policies.plan_guard
        if plan_guard is not None:
            learning_guard = plan_guard.evaluate(plan=updated_plan, advisory=learning_advisory)
            updated_plan = attach_learning_guard_to_trade_plan(
                plan=updated_plan,
                guard=learning_guard,
            )

        updated_result = replace(result, plan=updated_plan)
        self.journal.append(
            "trade_plan_learning_advisory_attached",
            symbol=updated_plan.symbol,
            action=updated_plan.action,
            plan_id=updated_plan.plan_id,
            advisory_id=learning_advisory.advisory_id,
            advisory_status=learning_advisory.status,
            advisory_severity=learning_advisory.severity,
            would_block_trade=learning_advisory.would_block_trade,
            would_reduce_priority=learning_advisory.would_reduce_priority,
            can_use_for_execution=learning_advisory.can_use_for_execution,
            shadow_only=learning_advisory.shadow_only,
            diagnostics=updated_plan.diagnostics.get("learning_advisory", {}),
        )

        if learning_guard is not None:
            self.journal.append(
                "trade_plan_learning_guard_shadow_evaluated",
                symbol=updated_plan.symbol,
                action=updated_plan.action,
                plan_id=updated_plan.plan_id,
                guard_id=learning_guard.guard_id,
                guard_status=learning_guard.status,
                suggested_action=learning_guard.suggested_action,
                reason=learning_guard.reason,
                would_block_trade=learning_guard.would_block_trade,
                would_reduce_size=learning_guard.would_reduce_size,
                suggested_size_multiplier=learning_guard.suggested_size_multiplier,
                can_use_for_execution=learning_guard.can_use_for_execution,
                shadow_only=learning_guard.shadow_only,
                diagnostics=updated_plan.diagnostics.get("learning_guard", {}),
            )

        return updated_result

    # --- execution policies -------------------------------------------------------------

    def record_execution_shadow(
        self,
        *,
        trade_pipeline_result: Any,
        action: str,
        planned_quantity: Any,
    ) -> LearningExecutionShadowDecision | None:
        policy = self.policies.execution_shadow_policy
        if policy is None:
            return None

        plan = trade_pipeline_result.plan if trade_pipeline_result is not None else None

        try:
            decision: LearningExecutionShadowDecision = policy.evaluate(
                plan=plan,
                action=action,
                planned_quantity=planned_quantity,
            )
            self.journal.append(
                "learning_execution_shadow_evaluated",
                symbol=decision.symbol,
                action=decision.action,
                plan_id=decision.plan_id,
                decision_id=decision.decision_id,
                status=decision.status,
                suggested_action=decision.suggested_action,
                reason=decision.reason,
                guard_id=decision.guard_id,
                guard_status=decision.guard_status,
                original_quantity=decision.original_quantity,
                shadow_quantity=decision.shadow_quantity,
                suggested_size_multiplier=decision.suggested_size_multiplier,
                would_block_trade=decision.would_block_trade,
                would_reduce_size=decision.would_reduce_size,
                can_use_for_execution=decision.can_use_for_execution,
                shadow_only=decision.shadow_only,
                diagnostics=decision.diagnostics,
            )
            return decision
        except Exception as error:
            self.journal.append(
                "learning_execution_shadow_failed",
                action=action,
                reason=str(error),
            )
            return None

    def evaluate_paper_execution(
        self,
        *,
        learning_execution_decision: LearningExecutionShadowDecision | None,
    ) -> Any:
        policy = self.policies.paper_execution_policy
        if policy is None:
            return None

        try:
            decision = policy.evaluate(
                shadow_decision=learning_execution_decision,
                activation_report_payload=self.latest_shadow_impact_report(),
            )
            self.journal.append(
                "learning_paper_execution_evaluated",
                symbol=decision.symbol,
                action=decision.trade_action,
                plan_id=decision.plan_id,
                decision_id=decision.decision_id,
                shadow_decision_id=decision.shadow_decision_id,
                status=decision.status,
                paper_action=decision.action,
                reason=decision.reason,
                original_quantity=decision.original_quantity,
                final_quantity=decision.final_quantity,
                applied=decision.applied,
                reduced=decision.reduced,
                blocked=decision.blocked,
                can_use_for_execution=decision.can_use_for_execution,
                paper_only=decision.paper_only,
                testnet_only=False,
                can_activate_testnet=decision.can_activate_testnet,
                can_activate_real=decision.can_activate_real,
                diagnostics=decision.diagnostics,
            )
            return decision
        except Exception as error:
            self.journal.append(
                "learning_paper_execution_failed",
                reason=str(error),
            )
            return None

    def record_testnet_observation(
        self,
        *,
        learning_execution_decision: LearningExecutionShadowDecision | None,
    ) -> Any:
        policy = self.policies.testnet_observation_policy
        if policy is None:
            return None

        try:
            evaluation = policy.evaluate(
                shadow_decision=learning_execution_decision,
                activation_report_payload=self.latest_shadow_impact_report(),
            )
            self.journal.append(
                "learning_testnet_observation_evaluated",
                symbol=evaluation.symbol,
                action=evaluation.trade_action,
                plan_id=evaluation.plan_id,
                observation_id=evaluation.observation_id,
                shadow_decision_id=evaluation.shadow_decision_id,
                shadow_status=evaluation.shadow_status,
                status=evaluation.status,
                observed_action=evaluation.observed_action,
                reason=evaluation.reason,
                original_quantity=evaluation.original_quantity,
                shadow_quantity=evaluation.shadow_quantity,
                activation_report_available=evaluation.activation_report_available,
                report_can_apply_reduction_in_paper=(
                    evaluation.report_can_apply_reduction_in_paper
                ),
                report_can_apply_block_in_paper=evaluation.report_can_apply_block_in_paper,
                would_observe_reduction_signal=evaluation.would_observe_reduction_signal,
                would_observe_block_signal=evaluation.would_observe_block_signal,
                can_use_for_execution=evaluation.can_use_for_execution,
                observation_only=evaluation.observation_only,
                testnet_only=evaluation.testnet_only,
                can_activate_testnet=evaluation.can_activate_testnet,
                can_activate_real=evaluation.can_activate_real,
                diagnostics=evaluation.diagnostics,
            )
            return evaluation
        except Exception as error:
            self.journal.append(
                "learning_testnet_observation_failed",
                reason=str(error),
            )
            return None

    def evaluate_testnet_execution(
        self,
        *,
        learning_execution_decision: LearningExecutionShadowDecision | None,
    ) -> Any:
        policy = self.policies.testnet_execution_policy
        if policy is None:
            return None

        try:
            decision = policy.evaluate(
                shadow_decision=learning_execution_decision,
                activation_report_payload=self.latest_shadow_impact_report(),
            )
            self.journal.append(
                "learning_testnet_execution_evaluated",
                symbol=decision.symbol,
                action=decision.trade_action,
                plan_id=decision.plan_id,
                decision_id=decision.decision_id,
                shadow_decision_id=decision.shadow_decision_id,
                status=decision.status,
                testnet_action=decision.action,
                reason=decision.reason,
                original_quantity=decision.original_quantity,
                final_quantity=decision.final_quantity,
                applied=decision.applied,
                reduced=decision.reduced,
                blocked=decision.blocked,
                can_use_for_execution=decision.can_use_for_execution,
                testnet_only=decision.testnet_only,
                can_activate_testnet=decision.can_activate_testnet,
                can_activate_real=decision.can_activate_real,
                diagnostics=decision.diagnostics,
            )
            return decision
        except Exception as error:
            self.journal.append(
                "learning_testnet_execution_failed",
                reason=str(error),
            )
            return None
