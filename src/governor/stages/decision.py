"""Decision stages after the heavy snapshot: intent (10) and state reconciliation (11)."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any, Protocol

from config.symbol_context import SymbolTradingConfig
from domain.models import FuturesPosition, MarketSnapshot
from execution.state_reconciliation import (
    ExchangeStateSnapshot,
    ReconciliationDecision,
    StateReconciliationEngine,
    build_exchange_state_snapshot,
)
from futures.risk_guard import FuturesRiskGuard
from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.events import HEAVY_CHECK_FAILED, HEAVY_CHECK_PASSED
from governor.governor_helpers import _enum_value
from governor.governor_trade_helpers import (
    EXECUTION_OPEN_ACTIONS,
    _ai_signal_audit_fields,
    _is_no_action_result,
    _robot_orders_for_symbol,
    _runtime_state_position,
)
from governor.hot_symbol_worker import GuardDecision, GuardOutcome
from governor.stages.ports import StateTransitionRecorder, TracePayload, required
from governor.symbol_evaluation_pipeline import (
    HeavyEvaluation,
    IntentCandidate,
    SymbolEvaluationContext,
    SymbolEvaluationResult,
)
from governor.symbol_state_machine import SymbolOperationalState
from market_data.market_snapshots import HeavyMarketSnapshot
from operational_module_modes import MODULE_STATE_RECONCILIATION, ReconciliationMode
from scanner.signal_score import SignalScore
from trading.intent import TradeIntent
from utils.events import EventChannel, observe_event

# Hot-worker block reasons that also count as symbol health events.
_QUARANTINE_EVENT_BY_REASON = {
    "STALE_DATA": "STALE_DATA_REPEATED",
    "SYMBOL_RULES_FAILED": "EXCHANGE_RULES_MISSING",
    "OPEN_ORDER_CONFLICT": "OPEN_ORDER_CONFLICT",
}


class IntentSource(Protocol):
    @property
    def global_strategy_name(self) -> str: ...

    def intent(
        self,
        symbol_config: SymbolTradingConfig,
        market: MarketSnapshot,
        position: FuturesPosition,
        *,
        score: SignalScore | None = None,
    ) -> TradeIntent: ...


class HotGuard(Protocol):
    def evaluate(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        score: SignalScore,
        intent: TradeIntent,
        heavy_snapshot: HeavyMarketSnapshot,
        paused: bool = False,
        risk_guard: FuturesRiskGuard | None = None,
    ) -> GuardDecision: ...


class QuarantineEventRecorder(Protocol):
    def __call__(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        event: str,
        reason: str,
        now: float | None = None,
    ) -> None: ...


class HotSymbolDecisionStage:
    """Stage 10: build the intent and confirm it with the hot symbol worker."""

    name = "intent_creation_hot_guard"

    def __init__(
        self,
        *,
        outcomes: StageOutcomes,
        strategy: IntentSource,
        hot_worker: HotGuard,
        risk_guard_for_symbol: Callable[[SymbolTradingConfig], FuturesRiskGuard],
        paused_symbols: Collection[str],
        journal: EventJournal,
        trace_payload: TracePayload,
        record_transition: StateTransitionRecorder,
        record_quarantine_event: QuarantineEventRecorder,
    ) -> None:
        self.outcomes = outcomes
        self.strategy = strategy
        self.hot_worker = hot_worker
        self.risk_guard_for_symbol = risk_guard_for_symbol
        self.paused_symbols = paused_symbols
        self.journal = journal
        self.trace_payload = trace_payload
        self.record_transition = record_transition
        self.record_quarantine_event = record_quarantine_event

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        heavy = required(evaluation.heavy, "heavy")
        score = required(evaluation.score, "score")

        risk_guard = self.risk_guard_for_symbol(symbol_config)
        evaluation.hot_result = None
        evaluation.trade_decision = None
        intent = self.strategy.intent(
            symbol_config,
            heavy.market,
            heavy.position,
            score=score,
        )
        evaluation.intent = intent
        hot_result = self.hot_worker.evaluate(
            symbol_config=symbol_config,
            score=score,
            intent=intent,
            heavy_snapshot=heavy,
            paused=symbol_config.symbol in self.paused_symbols,
            risk_guard=risk_guard,
        )
        evaluation.hot_result = hot_result
        if hot_result.directive is not None:
            directive = hot_result.directive
            intent = TradeIntent(
                symbol=symbol_config.symbol,
                action=directive.action.value,
                confidence=intent.confidence,
                score=intent.score,
                priority=intent.priority,
                reason=directive.reason,
                strategy_name=f"operational:{directive.source}",
                market_price=heavy.market.price,
                suggested_next_check_seconds=intent.suggested_next_check_seconds,
                metadata={
                    "operational_directive": {
                        "source": directive.source,
                        "parent_intent_id": directive.parent_intent_id,
                        "outcome": hot_result.outcome.value,
                    }
                },
            )
            evaluation.intent = intent
        if intent.market_price is None:
            intent = intent.with_updates(market_price=heavy.market.price)
            evaluation.intent = intent
        if isinstance(evaluation.heavy_evaluation, HeavyEvaluation):
            evaluation.intent_candidate = IntentCandidate(
                heavy_evaluation=evaluation.heavy_evaluation,
                decision=evaluation.trade_decision,
                intent=intent,
            )
        guard_allows_execution = hot_result.outcome in {
            GuardOutcome.ALLOW,
            GuardOutcome.FORCE_CLOSE,
        }
        event_type = HEAVY_CHECK_PASSED if guard_allows_execution else HEAVY_CHECK_FAILED
        # Explicit severity: a strategy HOLD is a normal outcome, not an error, and the
        # audit summary would otherwise infer ERROR from the "failed" suffix.
        if guard_allows_execution or _is_no_action_result(hot_result.reason):
            event_severity = "INFO"
        else:
            event_severity = "WARNING"
        self.journal.append(
            event_type,
            **self.trace_payload(),
            severity=event_severity,
            symbol=symbol_config.symbol,
            strategy=self.strategy.global_strategy_name,
            intent_id=getattr(intent, "intent_id", None),
            score=score.score,
            priority=required(score.priority, "score.priority").value,
            reason=hot_result.reason,
            outcome=hot_result.outcome.value,
            checks=hot_result.checks,
            **_ai_signal_audit_fields(
                score,
                final_action=intent.action,
                block_reason=(None if guard_allows_execution else hot_result.reason),
            ),
        )
        if not guard_allows_execution:
            self._record_quarantine_event(evaluation, hot_result)
            if _is_no_action_result(hot_result.reason):
                self.outcomes.board.mark_no_action(symbol_config.symbol, hot_result.reason)
                classification = "observacao"
            else:
                self.outcomes.board.mark_blocked(symbol_config.symbol, hot_result.reason)
                classification = "operacional"
                self.record_transition(
                    evaluation,
                    to_state=SymbolOperationalState.BLOCKED,
                    reason=hot_result.reason,
                    stage="HOT_SYMBOL_WORKER",
                )
            return self.outcomes.terminal(
                evaluation,
                stage="HOT_SYMBOL_WORKER",
                classification=classification,
                reason=hot_result.reason,
            )
        self.record_transition(
            evaluation,
            to_state=SymbolOperationalState.SIGNAL_CANDIDATE,
            reason=hot_result.reason,
            stage="HOT_SYMBOL_WORKER",
            action=intent.action,
        )
        return None

    def _record_quarantine_event(
        self,
        evaluation: SymbolEvaluationContext,
        hot_result: GuardDecision,
    ) -> None:
        reason = str(hot_result.reason or "").upper().strip()
        event = _QUARANTINE_EVENT_BY_REASON.get(reason)
        if event is None:
            return
        self.record_quarantine_event(
            symbol_config=evaluation.evaluation_input.symbol_config,
            event=event,
            reason=reason,
            now=evaluation.evaluation_input.now,
        )


class StateReconciliationStage:
    """Stage 11: compare local and exchange state before a new entry."""

    name = "run_state_reconciliation_guard"

    def __init__(
        self,
        *,
        outcomes: StageOutcomes,
        engine: StateReconciliationEngine,
        module_modes: Mapping[str, Any],
        journal: EventJournal,
        trace_payload: TracePayload,
        record_transition: StateTransitionRecorder,
    ) -> None:
        self.outcomes = outcomes
        self.engine = engine
        self.module_modes = module_modes
        self.journal = journal
        self.trace_payload = trace_payload
        self.record_transition = record_transition

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        action = required(evaluation.intent, "intent").action
        if action not in EXECUTION_OPEN_ACTIONS:
            return None

        mode = self.module_modes[MODULE_STATE_RECONCILIATION]
        if mode is ReconciliationMode.OFF:
            return None

        heavy = required(evaluation.heavy, "heavy")
        robot_normal_orders, robot_algo_orders = _robot_orders_for_symbol(
            symbol_config,
            normal_orders=heavy.open_orders,
            algo_orders=heavy.open_algo_orders,
        )
        snapshot = build_exchange_state_snapshot(
            symbol=symbol_config.symbol,
            local_position=_runtime_state_position(
                symbol_config.symbol,
                evaluation.runtime_state,
            ),
            exchange_position=heavy.position,
            normal_orders=heavy.open_orders,
            algo_orders=heavy.open_algo_orders,
            robot_normal_orders=robot_normal_orders,
            robot_algo_orders=robot_algo_orders,
            data_source="GOVERNOR_HEAVY_SNAPSHOT",
            timestamp=evaluation.evaluation_input.now,
            diagnostics={
                "runtime_status": _enum_value(evaluation.status),
                "heavy_stale": bool(getattr(heavy, "stale", False)),
                "user_stream_stale": bool(getattr(heavy, "stale", False)),
            },
        )
        decision = self.engine.evaluate(snapshot, mode=mode)
        evaluation.state_reconciliation_decision = decision
        self._record_decision(
            symbol=symbol_config.symbol,
            action=action,
            snapshot=snapshot,
            decision=decision,
            stage="PRE_ENTRY",
        )
        if decision.should_block_new_entries:
            self.outcomes.board.mark_blocked(symbol_config.symbol, "RECONCILIATION_BLOCK")
            self.record_transition(
                evaluation,
                to_state=SymbolOperationalState.BLOCKED,
                reason="RECONCILIATION_BLOCK",
                stage="STATE_RECONCILIATION",
                action=action,
                diagnostics=decision.to_dict(),
            )
            return self.outcomes.terminal(
                evaluation,
                stage="STATE_RECONCILIATION",
                classification="seguranca",
                reason="RECONCILIATION_BLOCK",
                action=action,
            )
        return None

    def _record_decision(
        self,
        *,
        symbol: str,
        action: str,
        snapshot: ExchangeStateSnapshot,
        decision: ReconciliationDecision,
        stage: str,
    ) -> None:
        payload = decision.to_dict()
        report_path = (
            payload.get("diagnostics", {}).get("reconciliation_report_path")
            if isinstance(payload.get("diagnostics"), dict)
            else None
        )
        self.journal.append(
            "reconciliation_snapshot_created",
            **self.trace_payload(),
            symbol=symbol,
            action=action,
            stage=stage,
            mode=decision.mode.value,
            snapshot=snapshot.to_dict(),
            status=decision.status,
            issues_count=len(decision.issues),
            should_block_new_entries=decision.should_block_new_entries,
            diagnostics=payload,
            reconciliation_report_path=report_path,
            execution_changed=decision.affected_result,
            order_mutation=False,
        )
        for issue in decision.issues:
            issue_payload = issue.to_dict()
            issue_payload.pop("symbol", None)
            self.journal.append(
                "reconciliation_issue_detected",
                **self.trace_payload(),
                symbol=symbol,
                action=action,
                stage=stage,
                **issue_payload,
                mode=decision.mode.value,
                should_block_new_entries=decision.should_block_new_entries,
                execution_changed=decision.affected_result,
                order_mutation=False,
            )
        if decision.should_block_new_entries:
            self.journal.append(
                "reconciliation_block_applied",
                **self.trace_payload(),
                symbol=symbol,
                action=action,
                stage=stage,
                mode=decision.mode.value,
                status=decision.status,
                issues_count=len(decision.issues),
                issues=[issue.to_dict() for issue in decision.issues],
                should_block_new_entries=True,
                reconciliation_report_path=report_path,
                execution_changed=True,
                order_mutation=False,
            )
        observe_event(
            "state_reconciliation_evaluated",
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            symbol=symbol,
            action=action,
            stage=stage,
            mode=decision.mode.value,
            status=decision.status,
            issues_count=len(decision.issues),
            should_block_new_entries=decision.should_block_new_entries,
        )
