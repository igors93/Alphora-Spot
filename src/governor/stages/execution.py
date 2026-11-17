"""Stage 22: revalidate the snapshot, check the price, run the pre-execution guard and dispatch.

This is the only stage that sends orders. It stays one stage (``pre_execution_execution``)
so the stage trail is unchanged, but each step is its own object or method:

    ExecutionRevalidation   fresh private snapshot, readiness, state and portfolio again
    PreDispatchPriceCheck   price drift since the plan; replans stages 15–21 when needed
    ExecutionStage          approval trail, pre-execution guard, dispatch and its result

The final dispatch is delegated to the ``ExecutionCoordinator``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from time import monotonic
from typing import Any, Protocol

from config.symbol_context import SymbolTradingConfig
from domain.models import TradeAction
from futures.exit_models import build_exit_model
from futures.position_execution import PositionRecoveryStatus
from futures.protection import ExpectedProtection, build_expected_protection_for_entry
from futures.risk_manager import FuturesRiskManager
from governor.cycle_context import CycleContext
from governor.evaluation.outcomes import EventJournal, StageOutcomes
from governor.evaluation.stage import StageCallable
from governor.event_payloads import (
    planned_exit_stop,
    planned_exit_take,
    pre_execution_guard_payload,
)
from governor.events import (
    CLOSE_POSITION_REQUESTED,
    CLOSE_POSITION_SENT,
    COMMAND_DISPATCHED,
    EXECUTION_APPROVED,
    EXECUTION_BLOCKED,
    EXECUTION_CONFIRMED,
    EXECUTION_DRY_RUN,
    EXECUTION_FAILED,
    EXECUTION_PAYLOAD_SENT,
    EXECUTION_ROLLED_BACK,
    EXECUTION_SKIPPED,
    EXECUTION_UNCERTAIN,
    TRADE_APPROVED,
)
from governor.execution_engine import ExecutionCommand, ExecutionResult, ExecutionStatus
from governor.governor_trade_helpers import (
    EXECUTION_OPEN_ACTIONS,
    INTELLIGENT_FINAL_STATUS_READY_FOR_EXECUTION,
    _ai_signal_audit_fields,
    _notional_from_planned_quantity,
    _price_distance_bps,
)
from governor.pre_execution_guard import PreExecutionGuard
from governor.services.trade_events import TradeEventRecorder
from governor.services.trend_context import attach_trend_context
from governor.stages.ports import StateTransitionRecorder, required
from governor.symbol_evaluation_pipeline import (
    ExecutableTrade,
    HeavyEvaluation,
    IntentCandidate,
    PlannedTrade,
    SymbolEvaluationContext,
    SymbolEvaluationResult,
    SymbolEvaluationStageResult,
    ValidatedTrade,
)
from governor.symbol_state_machine import SymbolOperationalState
from market_data.market_snapshots import HeavyMarketSnapshot
from market_data.snapshot_provider import (
    SnapshotReadiness,
    SnapshotTransportBudgetExceeded,
    snapshot_error_diagnostics_payload,
    snapshot_error_failure_summary,
    snapshot_error_failure_summary_text,
    wire_names,
)
from trading.enums import (
    TradeActionCategory,
    action_increases_risk,
    action_reduces_or_repairs_risk,
    classify_trade_action,
)
from trading.intent import TradeIntent, metadata_to_plain_dict
from utils.events import EventChannel, observe_error, observe_event

TracePayload = Callable[[], Mapping[str, str]]
Port = Callable[..., Any]

OPEN_POSITION_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}
CLOSE_POSITION_ACTION = "CLOSE_POSITION"


# --- pure helpers ---------------------------------------------------------------------------


def build_pre_entry_expected_protection(
    *,
    symbol_config: SymbolTradingConfig,
    action: str,
    intent: Any,
    heavy_snapshot: Any,
    planned_exit: dict[str, Any] | None,
    trade_plan_id: str | None,
    dynamic_exit_enabled: bool,
) -> ExpectedProtection | None:
    """The stop and take the executor will place after an entry, previewed before sending it."""

    if action not in EXECUTION_OPEN_ACTIONS:
        return None
    if heavy_snapshot.exchange_rules is None:
        raise ValueError("exchange_rules indisponiveis para preview de protecao")

    entry_price = (
        Decimal(str(intent.market_price))
        if getattr(intent, "market_price", None) is not None
        else Decimal(str(heavy_snapshot.market.price))
    )
    risk_manager = FuturesRiskManager(
        position_notional_usdt=symbol_config.position_notional_usdt,
        stop_loss_pct=symbol_config.stop_loss_pct,
        take_profit_pct=symbol_config.take_profit_pct,
    )
    use_plan = dynamic_exit_enabled and planned_exit is not None
    return build_expected_protection_for_entry(
        action=TradeAction(action),
        entry_price=entry_price,
        risk_manager=risk_manager,
        rules=heavy_snapshot.exchange_rules,
        working_type=symbol_config.protection_working_type.value,
        exit_model=build_exit_model(symbol_config),
        planned_stop_price=planned_exit_stop(planned_exit) if use_plan else None,
        planned_take_profit_price=planned_exit_take(planned_exit) if use_plan else None,
        trade_plan_id=trade_plan_id if dynamic_exit_enabled else None,
    )


def pre_entry_max_price_drift_bps(config: Any) -> Decimal:
    settings = getattr(config, "smart_execution_settings", None)
    value = getattr(settings, "max_price_drift_bps", None)
    if value is None:
        value = getattr(config, "limit_entry_max_price_drift_bps", Decimal("10"))
    return Decimal(str(value))


def pre_dispatch_price_drift_limits(config: Any) -> tuple[Decimal, Decimal]:
    soft = Decimal(str(getattr(config, "pre_dispatch_soft_price_drift_bps", 10.0)))
    hard = Decimal(str(getattr(config, "pre_dispatch_hard_price_drift_bps", 30.0)))
    return max(Decimal("0"), soft), max(soft, hard)


def execution_event_type(result: ExecutionResult) -> str:
    if result.recovery_status is PositionRecoveryStatus.ROLLED_BACK:
        return EXECUTION_ROLLED_BACK
    return {
        ExecutionStatus.CONFIRMED: EXECUTION_CONFIRMED,
        ExecutionStatus.DRY_RUN: EXECUTION_DRY_RUN,
        ExecutionStatus.BLOCKED: EXECUTION_BLOCKED,
        ExecutionStatus.SKIPPED: EXECUTION_SKIPPED,
        ExecutionStatus.IGNORED: EXECUTION_SKIPPED,
        ExecutionStatus.UNCERTAIN: EXECUTION_UNCERTAIN,
        ExecutionStatus.FAILED: EXECUTION_FAILED,
    }[result.status]


def preserve_opened_at_from_previous_position(position: Any, previous_position: Any) -> Any:
    """Keep the local ``opened_at`` when a refreshed exchange view of the same position lacks it."""

    if getattr(position, "opened_at", None) is not None:
        return position
    opened_at = getattr(previous_position, "opened_at", None)
    if opened_at is None:
        return position
    if not bool(getattr(position, "is_open", False)):
        return position
    if not bool(getattr(previous_position, "is_open", False)):
        return position
    if getattr(position, "symbol", None) != getattr(previous_position, "symbol", None):
        return position
    if getattr(position, "direction", None) != getattr(previous_position, "direction", None):
        return position
    return replace(position, opened_at=opened_at)


def _is_budget_exceeded(error: Exception) -> bool:
    if isinstance(error, SnapshotTransportBudgetExceeded):
        return True
    nested = getattr(error, "errors", None) or {}
    return any(
        isinstance(getattr(value, "__cause__", None), SnapshotTransportBudgetExceeded)
        or "SnapshotTransportBudgetExceeded" in str(type(value).__name__)
        for value in nested.values()
    )


# --- ports ----------------------------------------------------------------------------------


class ExecutionDispatcher(Protocol):
    def execute(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        command: ExecutionCommand,
    ) -> ExecutionResult: ...

    def execute_trade(
        self,
        *,
        symbol_config: SymbolTradingConfig,
        heavy_snapshot: HeavyMarketSnapshot,
        trade: ExecutableTrade,
    ) -> ExecutionResult: ...


@dataclass(frozen=True)
class ExecutionPorts:
    """What stage 22 reads, notifies or calls. The coordinator dispatches mutations."""

    config: Any
    journal: EventJournal
    trace_payload: TracePayload
    logger: Any
    record_transition: StateTransitionRecorder
    portfolio_account_snapshot: Callable[..., dict[str, Any]]
    portfolio_risk_engine: Any
    symbol_configs: Sequence[SymbolTradingConfig]
    state_reconciliation: StageCallable
    plan_stages: Callable[[], Sequence[StageCallable]]
    trade_events: TradeEventRecorder
    record_finalized: Port
    pre_execution_guard: PreExecutionGuard
    execution_engine: Any
    execution_coordinator: ExecutionDispatcher
    invalidate_private_state: Port
    count_new_position: Callable[[], None]


class _Blocker:
    """Block the symbol on the board, record the transition and stop the pipeline."""

    def __init__(self, outcomes: StageOutcomes, ports: ExecutionPorts) -> None:
        self.outcomes = outcomes
        self.ports = ports

    def __call__(
        self,
        evaluation: SymbolEvaluationContext,
        *,
        reason: str,
        stage: str,
        classification: str,
        action: str,
        diagnostics: dict[str, Any] | None = None,
        mark_board: bool = True,
    ) -> SymbolEvaluationResult:
        if mark_board:
            self.outcomes.board.mark_blocked(
                evaluation.evaluation_input.symbol_config.symbol,
                reason,
            )
        self.ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.BLOCKED,
            reason=reason,
            stage=stage,
            action=action,
            diagnostics=diagnostics,
        )
        return self.outcomes.terminal(
            evaluation,
            stage=stage,
            classification=classification,
            reason=reason,
            action=action,
        )


# --- revalidation ---------------------------------------------------------------------------


class ExecutionRevalidation:
    """Re-read the symbol's private state right before the order and re-run the risk checks.

    New entries are fail-closed: any failure blocks them. Exits may fall back to a degraded
    snapshot that still allows a reduce-only order, because staying in a position is the
    riskier outcome.
    """

    def __init__(self, outcomes: StageOutcomes, ports: ExecutionPorts) -> None:
        self.outcomes = outcomes
        self.ports = ports
        self.block = _Blocker(outcomes, ports)

    def revalidate(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        context = evaluation.cycle_context
        action = required(evaluation.intent, "intent").action
        session = getattr(context, "snapshot_session", None) if context is not None else None
        if session is None:
            return self._without_session(evaluation, action)

        started = monotonic()
        try:
            return self._with_session(evaluation, session, action)
        except Exception as error:
            return self._on_failure(evaluation, session, action, error, monotonic() - started)

    def _without_session(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
    ) -> SymbolEvaluationResult | None:
        if classify_trade_action(action) is TradeActionCategory.RISK_INCREASING:
            return self.block(
                evaluation,
                reason=wire_names.REASON_SESSION_UNAVAILABLE,
                stage=wire_names.STAGE_ENTRY_GUARD,
                classification="bloqueado",
                action=action,
                mark_board=False,
            )
        if action_reduces_or_repairs_risk(action):
            self.ports.journal.append(
                wire_names.EXIT_DEGRADED_WITHOUT_SESSION,
                **self.ports.trace_payload(),
                symbol=evaluation.evaluation_input.symbol_config.symbol,
                action=action,
                readiness=SnapshotReadiness.UNAVAILABLE.name,
                reason=wire_names.REASON_SESSION_UNAVAILABLE_EXIT_DEGRADED,
                order_mutation=False,
            )
        return None

    def _with_session(
        self,
        evaluation: SymbolEvaluationContext,
        session: Any,
        action: str,
    ) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        context = evaluation.cycle_context
        exit_only = action_reduces_or_repairs_risk(action)
        snapshot = session.revalidate_for_execution(symbol_config.symbol, exit_only=exit_only)
        if context is not None:
            context.operational_snapshot = snapshot
        provider = session.provider
        readiness = provider.readiness_diagnostic(
            snapshot,
            profile="exit" if exit_only else "entry",
            symbol=symbol_config.symbol,
        )
        if context is not None:
            context.snapshot_execution_diagnostics[symbol_config.symbol] = readiness

        blocked = self._readiness_block(evaluation, action, readiness)
        if blocked is not None:
            return blocked

        router_heavy = provider.heavy_snapshot_from_snapshot(
            snapshot,
            symbol_config.symbol,
            now=evaluation.evaluation_input.now,
        )
        refreshed_heavy = self._refreshed_heavy(evaluation, router_heavy, exit_only=exit_only)
        self._adopt_heavy(evaluation, router_heavy, refreshed_heavy)

        state_result = self.ports.state_reconciliation(evaluation)
        if state_result is not None and state_result.terminal:
            return state_result

        blocked = self._portfolio_block(evaluation, action)
        if blocked is not None:
            return blocked

        portfolio_decision = required(evaluation.portfolio_decision, "portfolio_decision")
        self.ports.journal.append(
            wire_names.PRE_EXECUTION_REVALIDATED,
            **self.ports.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            snapshot_id=snapshot.snapshot_id,
            readiness=readiness.readiness.name,
            readiness_reason=readiness.reason,
            readiness_diagnostics=readiness.to_dict(),
            position_is_open=bool(getattr(refreshed_heavy.position, "is_open", False)),
            open_orders_count=len(refreshed_heavy.open_orders),
            open_algo_orders_count=len(refreshed_heavy.open_algo_orders),
            account_available=bool(refreshed_heavy.account_info),
            portfolio_allowed=portfolio_decision.allowed,
        )
        return None

    def _readiness_block(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
        readiness: Any,
    ) -> SymbolEvaluationResult | None:
        if action_increases_risk(action) and not readiness.readiness.allows_entry:
            return self.block(
                evaluation,
                reason=wire_names.REASON_ENTRY_NOT_SAFE,
                stage=wire_names.STAGE_ENTRY_READINESS,
                classification="seguranca",
                action=action,
                diagnostics=readiness.to_dict(),
            )
        if action_reduces_or_repairs_risk(action) and not readiness.readiness.allows_exit:
            return self.block(
                evaluation,
                reason=wire_names.REASON_EXIT_DATA_UNAVAILABLE,
                stage=wire_names.STAGE_EXIT_READINESS,
                classification="seguranca",
                action=action,
                diagnostics=readiness.to_dict(),
            )
        return None

    def _refreshed_heavy(
        self,
        evaluation: SymbolEvaluationContext,
        router_heavy: HeavyMarketSnapshot,
        *,
        exit_only: bool,
    ) -> HeavyMarketSnapshot:
        symbol_config = evaluation.evaluation_input.symbol_config
        previous_position = required(evaluation.heavy, "heavy").position
        refreshed_heavy = router_heavy
        if (
            exit_only
            and bool(getattr(previous_position, "is_open", False))
            and not bool(getattr(refreshed_heavy.position, "is_open", False))
            and getattr(previous_position, "symbol", None)
            == getattr(refreshed_heavy.position, "symbol", None)
        ):
            # Risk-reducing commands may proceed from the last confirmed open view when a
            # just-in-time REST read already reports flat. Sending a close for an already-flat
            # position is safe and lets the execution layer reconcile the race; replacing the
            # position with flat here would silently discard the requested exit and its local
            # lifecycle metadata. New entries remain strictly fail-closed.
            refreshed_heavy = replace(refreshed_heavy, position=previous_position)
            self.ports.journal.append(
                wire_names.EXIT_POSITION_VIEW_PRESERVED,
                **self.ports.trace_payload(),
                symbol=symbol_config.symbol,
                reason="REST_FLAT_WHILE_LOCAL_POSITION_OPEN",
                previous_direction=getattr(
                    getattr(previous_position, "direction", None),
                    "value",
                    None,
                ),
                previous_quantity=getattr(previous_position, "quantity", None),
            )

        sync_position = getattr(self.ports.execution_engine, "sync_position_state", None)
        if callable(sync_position):
            refreshed_heavy = replace(
                refreshed_heavy,
                position=sync_position(symbol_config, refreshed_heavy.position),
            )
        refreshed_heavy = replace(
            refreshed_heavy,
            position=preserve_opened_at_from_previous_position(
                refreshed_heavy.position,
                previous_position,
            ),
        )
        if evaluation.trend_context is not None:
            refreshed_heavy = replace(
                refreshed_heavy,
                market=attach_trend_context(refreshed_heavy.market, evaluation.trend_context),
            )
        return refreshed_heavy

    def _adopt_heavy(
        self,
        evaluation: SymbolEvaluationContext,
        router_heavy: HeavyMarketSnapshot,
        refreshed_heavy: HeavyMarketSnapshot,
    ) -> None:
        """Make the refreshed snapshot the one every later step and frozen hand-off sees."""

        context = evaluation.cycle_context
        evaluation.router_heavy = router_heavy
        evaluation.heavy = refreshed_heavy
        if context is not None:
            symbol = evaluation.evaluation_input.symbol_config.symbol
            context.positions[symbol] = refreshed_heavy.position
            context.account_info = dict(refreshed_heavy.account_info)
            context.account_info_loaded = True

        evaluation.account_snapshot = self.ports.portfolio_account_snapshot(context=context)

        if isinstance(evaluation.heavy_evaluation, HeavyEvaluation):
            evaluation.heavy_evaluation = replace(
                evaluation.heavy_evaluation,
                heavy=refreshed_heavy,
            )
        if isinstance(evaluation.intent_candidate, IntentCandidate) and isinstance(
            evaluation.heavy_evaluation,
            HeavyEvaluation,
        ):
            evaluation.intent_candidate = replace(
                evaluation.intent_candidate,
                heavy_evaluation=evaluation.heavy_evaluation,
            )
        if isinstance(evaluation.planned_trade, PlannedTrade) and isinstance(
            evaluation.intent_candidate,
            IntentCandidate,
        ):
            evaluation.planned_trade = replace(
                evaluation.planned_trade,
                candidate=evaluation.intent_candidate,
            )

    def _portfolio_block(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
    ) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        intent = required(evaluation.intent, "intent")
        decision = self.ports.portfolio_risk_engine.evaluate_trade_intent(
            intent,
            self.ports.symbol_configs,
            evaluation.account_snapshot,
            override_notional=_notional_from_planned_quantity(evaluation.planned_quantity, intent),
        )
        evaluation.portfolio_decision = decision
        if decision.allowed:
            return None

        motivo_pt = decision.checks.get("motivo_pt", decision.reason)
        self.ports.logger.warning(
            "Trade bloqueado pelo governador após revalidação de snapshots | "
            "symbol=%s | action=%s | motivo=%s",
            symbol_config.symbol,
            action,
            motivo_pt,
        )
        self.ports.journal.append(
            wire_names.TRADE_BLOCKED_AFTER_REVALIDATION,
            **self.ports.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            reason=decision.reason,
            motivo_pt=motivo_pt,
            decision=decision,
        )
        return self.block(
            evaluation,
            reason=motivo_pt,
            stage=wire_names.STAGE_PORTFOLIO_AFTER_REVALIDATION,
            classification="operacional",
            action=action,
        )

    # --- failure ----------------------------------------------------------------------------

    def _on_failure(
        self,
        evaluation: SymbolEvaluationContext,
        session: Any,
        action: str,
        error: Exception,
        elapsed: float,
    ) -> SymbolEvaluationResult | None:
        self._record_failure(evaluation, action, error, elapsed)
        if action_reduces_or_repairs_risk(action) and session is not None:
            if self._use_degraded_exit_snapshot(evaluation, session, action):
                return None

        if action_increases_risk(action):
            observe_event(
                wire_names.DEGRADED_SNAPSHOT_REJECTED_FOR_ENTRY,
                component=__name__,
                category="governor",
                channel=EventChannel.AUDIT,
                symbol=evaluation.evaluation_input.symbol_config.symbol,
                action=action,
                reason="ENTRY_FAIL_CLOSED",
            )
        reason = (
            wire_names.REASON_EXIT_REVALIDATION_FAILED
            if action_reduces_or_repairs_risk(action)
            else wire_names.REASON_PRE_EXECUTION_REVALIDATION_FAILED
        )
        return self.block(
            evaluation,
            reason=reason,
            stage=wire_names.STAGE_PRE_EXECUTION_REVALIDATION,
            classification="seguranca",
            action=action,
        )

    def _record_failure(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
        error: Exception,
        elapsed: float,
    ) -> None:
        symbol = evaluation.evaluation_input.symbol_config.symbol
        error_type = type(error).__name__
        diagnostics = snapshot_error_diagnostics_payload(error)
        failure_summary = snapshot_error_failure_summary(error)
        failure_summary_text = snapshot_error_failure_summary_text(error)
        is_budget_exceeded = _is_budget_exceeded(error)
        skew = getattr(error, "observation_skew_seconds", None)
        logger = self.ports.logger
        if skew is not None:
            logger.warning(
                "Revalidação de snapshots pre-execução falhou por skew de consistência "
                "| symbol=%s | action=%s | elapsed=%.2fs | skew=%.2fs (max=%.2fs) "
                "| oldest=%s | newest=%s",
                symbol,
                action,
                elapsed,
                skew,
                getattr(error, "max_observation_skew_seconds", None) or 0.0,
                getattr(error, "oldest_key", None),
                getattr(error, "newest_key", None),
            )
        elif is_budget_exceeded:
            logger.warning(
                "Revalidação de snapshots pre-execução falhou por timeout REST Binance "
                "| symbol=%s | action=%s | elapsed=%.2fs "
                "| causa=SnapshotTransportBudgetExceeded "
                "| dica=REST Testnet lento; ajuste ALPHORA_SNAPSHOT_REST_READ_TIMEOUT_SECONDS "
                "| falhas=%s",
                symbol,
                action,
                elapsed,
                failure_summary_text or "-",
            )
        else:
            logger.warning(
                "Revalidação de snapshots pre-execução falhou | symbol=%s | action=%s "
                "| elapsed=%.2fs | error=%s: %s | falhas=%s",
                symbol,
                action,
                elapsed,
                error_type,
                str(error)[:200],
                failure_summary_text or "-",
            )
        observe_error(
            wire_names.PRE_EXECUTION_REVALIDATION_FAILED,
            error=error,
            component="governor.trade_pipeline",
            category="governor",
            symbol=symbol,
            action=action,
            error_type=error_type,
            elapsed_seconds=elapsed,
            is_budget_exceeded=is_budget_exceeded,
            failure_summary=failure_summary,
            diagnostics=diagnostics,
        )
        self.ports.journal.append(
            wire_names.PRE_EXECUTION_REVALIDATION_FAILED,
            **self.ports.trace_payload(),
            symbol=symbol,
            action=action,
            reason=str(error),
            error_type=error_type,
            elapsed_seconds=elapsed,
            is_budget_exceeded=is_budget_exceeded,
            failure_summary=failure_summary,
            failure_summary_text=failure_summary_text,
            diagnostics=diagnostics,
        )

    def _use_degraded_exit_snapshot(
        self,
        evaluation: SymbolEvaluationContext,
        session: Any,
        action: str,
    ) -> bool:
        """Let an exit continue on a degraded snapshot that still allows reducing risk."""

        symbol = evaluation.evaluation_input.symbol_config.symbol
        context = evaluation.cycle_context
        provider: Any = getattr(session, "provider", None)
        try_risk = getattr(provider, "try_risk_reduction_snapshot", None)
        if not callable(try_risk):
            return False
        try:
            now = float(getattr(evaluation.evaluation_input, "now", 0.0) or 0.0)
            risk_result = try_risk(symbol, cycle_id=getattr(context, "cycle_id", ""), now=now)
            observe_event(
                wire_names.RISK_REDUCTION_TRY_RESOLVED,
                component=__name__,
                category="governor",
                channel=EventChannel.AUDIT,
                symbol=symbol,
                action=action,
                state=risk_result.state.value,
                reason=risk_result.reason,
                allowed_actions=list(risk_result.allowed_actions),
                diagnostics=risk_result.to_dict(),
            )
            allowed = set(risk_result.allowed_actions)
            if not (
                risk_result.degraded
                and ("reduce_only_exit" in allowed or "cancel_orders" in allowed)
            ):
                return False
            self.ports.journal.append(
                wire_names.DEGRADED_SNAPSHOT_USED,
                **self.ports.trace_payload(),
                symbol=symbol,
                action=action,
                state=risk_result.state.value,
                allowed_actions=list(risk_result.allowed_actions),
                reason="RISK_REDUCTION_DEGRADED_FALLBACK",
            )
            if not provider.missing_heavy_snapshot_keys(risk_result.snapshot, symbol):
                self._adopt_degraded_heavy(evaluation, provider, risk_result.snapshot, now)
            return True
        except Exception as risk_error:
            self.ports.journal.append(
                wire_names.RISK_REDUCTION_FALLBACK_FAILED,
                **self.ports.trace_payload(),
                symbol=symbol,
                action=action,
                reason=str(risk_error),
                error_type=type(risk_error).__name__,
            )
            return False

    def _adopt_degraded_heavy(
        self,
        evaluation: SymbolEvaluationContext,
        provider: Any,
        snapshot: Any,
        now: float,
    ) -> None:
        symbol = evaluation.evaluation_input.symbol_config.symbol
        context = evaluation.cycle_context
        try:
            router_heavy = provider.heavy_snapshot_from_snapshot(snapshot, symbol, now=now)
            evaluation.router_heavy = router_heavy
            evaluation.heavy = router_heavy
            if context is not None:
                context.operational_snapshot = snapshot
                context.positions[symbol] = router_heavy.position
                context.account_info = dict(router_heavy.account_info)
                context.account_info_loaded = True
            evaluation.account_snapshot = self.ports.portfolio_account_snapshot(context=context)
        except Exception as error:
            # The exit still runs on the last confirmed heavy snapshot; say so.
            self.ports.logger.warning(
                "Snapshot degradado nao montou o heavy da saida; segue com o ultimo confirmado "
                "| symbol=%s | error=%s: %s",
                symbol,
                type(error).__name__,
                error,
            )


# --- pre-dispatch price check -------------------------------------------------------------


class PreDispatchPriceCheck:
    """Compare the dispatch price with the plan's reference price.

    Above the hard limit the entry is blocked; between the soft and the hard limits the
    plan (stages 15–21) is rebuilt at the current price before the order goes out.
    """

    def __init__(self, outcomes: StageOutcomes, ports: ExecutionPorts) -> None:
        self.outcomes = outcomes
        self.ports = ports
        self.block = _Blocker(outcomes, ports)

    def check(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult | None:
        intent = required(evaluation.intent, "intent")
        action = intent.action
        if action not in EXECUTION_OPEN_ACTIONS:
            return None

        heavy = required(evaluation.heavy, "heavy")
        current_price = Decimal(str(heavy.market.price))
        plan = (
            evaluation.trade_pipeline_result.plan
            if evaluation.trade_pipeline_result is not None
            else None
        )
        planned_reference = Decimal(
            str(
                getattr(plan, "entry_reference_price", None) or intent.market_price or current_price
            )
        )
        drift_bps = _price_distance_bps(current_price, planned_reference) or Decimal("0")
        soft_limit, hard_limit = pre_dispatch_price_drift_limits(self.ports.config)
        enabled = bool(getattr(self.ports.config, "pre_dispatch_price_check_enabled", True))
        decision: dict[str, Any] = {
            "enabled": enabled,
            "planned_reference_price": planned_reference,
            "current_contract_price": current_price,
            "mark_price": getattr(heavy, "mark_price", None),
            "price_drift_bps": drift_bps,
            "soft_price_drift_bps": soft_limit,
            "hard_price_drift_bps": hard_limit,
            "replan_required": False,
            "blocked": False,
        }
        evaluation.pre_dispatch_price_decision = decision
        symbol = evaluation.evaluation_input.symbol_config.symbol
        self.ports.journal.append(
            "pre_dispatch_price_checked",
            **self.ports.trace_payload(),
            symbol=symbol,
            action=action,
            trade_plan_id=evaluation.trade_plan_id,
            **decision,
        )
        if not enabled:
            return None

        if drift_bps > hard_limit:
            reason = "PRE_DISPATCH_PRICE_DRIFT_TOO_HIGH"
            decision["blocked"] = True
            decision["reason"] = reason
            self.ports.logger.warning(
                "Trade bloqueado por drift antes do dispatch | symbol=%s | action=%s "
                "| planejado=%s | atual=%s | drift_bps=%s | limite=%s",
                symbol,
                action,
                planned_reference,
                current_price,
                drift_bps,
                hard_limit,
            )
            return self.block(
                evaluation,
                reason=reason,
                stage="PRE_DISPATCH_PRICE_CHECK",
                classification="seguranca",
                action=action,
                diagnostics=decision,
            )

        self._reprice_intent(evaluation, intent, planned_reference, current_price, drift_bps)

        if drift_bps <= soft_limit:
            decision["reason"] = "PRE_DISPATCH_PRICE_ACCEPTED"
            return None

        decision["replan_required"] = True
        decision["reason"] = "PRE_DISPATCH_REPLAN_REQUIRED"
        return self._replan(evaluation, action, planned_reference, current_price, drift_bps)

    @staticmethod
    def _reprice_intent(
        evaluation: SymbolEvaluationContext,
        intent: TradeIntent,
        planned_reference: Decimal,
        current_price: Decimal,
        drift_bps: Decimal,
    ) -> None:
        metadata = metadata_to_plain_dict(intent.metadata)
        metadata["pre_dispatch_planned_reference_price"] = str(planned_reference)
        metadata["pre_dispatch_current_price"] = str(current_price)
        metadata["pre_dispatch_price_drift_bps"] = str(drift_bps)
        evaluation.intent = intent.with_updates(market_price=current_price, metadata=metadata)
        if isinstance(evaluation.intent_candidate, IntentCandidate) and isinstance(
            evaluation.heavy_evaluation,
            HeavyEvaluation,
        ):
            evaluation.intent_candidate = replace(
                evaluation.intent_candidate,
                intent=evaluation.intent,
                heavy_evaluation=evaluation.heavy_evaluation,
            )

    def _replan(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
        planned_reference: Decimal,
        current_price: Decimal,
        drift_bps: Decimal,
    ) -> SymbolEvaluationResult | None:
        symbol = evaluation.evaluation_input.symbol_config.symbol
        old_trade_plan_id = evaluation.trade_plan_id
        self.ports.journal.append(
            "pre_dispatch_replan_started",
            **self.ports.trace_payload(),
            symbol=symbol,
            action=action,
            supersedes_trade_plan_id=old_trade_plan_id,
            planned_reference_price=planned_reference,
            current_contract_price=current_price,
            price_drift_bps=drift_bps,
        )

        evaluation.reset_plan_for_replan()

        for stage in self.ports.plan_stages():
            result = stage(evaluation)
            if result is not None and result.terminal:
                return result

        self.ports.journal.append(
            "pre_dispatch_replan_completed",
            **self.ports.trace_payload(),
            symbol=symbol,
            action=action,
            supersedes_trade_plan_id=old_trade_plan_id,
            trade_plan_id=evaluation.trade_plan_id,
            current_contract_price=current_price,
            planned_stop_price=planned_exit_stop(evaluation.planned_exit),
            planned_take_profit_price=planned_exit_take(evaluation.planned_exit),
        )
        return None


# --- the stage ------------------------------------------------------------------------------


class ExecutionStage:
    """Stage 22: the last checks and the dispatch of an approved intent."""

    name = "pre_execution_execution"

    def __init__(self, *, outcomes: StageOutcomes, ports: ExecutionPorts) -> None:
        self.outcomes = outcomes
        self.ports = ports
        self.block = _Blocker(outcomes, ports)
        self.revalidation = ExecutionRevalidation(outcomes, ports)
        self.price_check = PreDispatchPriceCheck(outcomes, ports)

    def __call__(self, evaluation: SymbolEvaluationContext) -> SymbolEvaluationResult:
        action = required(evaluation.intent, "intent").action

        result = self.revalidation.revalidate(evaluation)
        if result is not None and result.terminal:
            return result

        result = self.price_check.check(evaluation)
        if result is not None and result.terminal:
            return result

        self._record_approval_trail(evaluation, action)

        result = self._pre_execution_guard(evaluation, action)
        if result is not None:
            return result

        result = self._dispatch_readiness_block(evaluation, action)
        if result is not None:
            return result

        command = self._approve(evaluation, action)
        execution_result = self._dispatch(evaluation, action, command)
        return self._record_execution_result(evaluation, action, command, execution_result)

    # --- before the guard -------------------------------------------------------------------

    def _record_approval_trail(self, evaluation: SymbolEvaluationContext, action: str) -> None:
        symbol_config = evaluation.evaluation_input.symbol_config
        trade_events = self.ports.trade_events
        trade_events.record_portfolio_trade_approved(evaluation, action=action)
        trade_events.record_trade_plan_execution_ready(
            symbol_config=symbol_config,
            action=action,
            trade_pipeline_result=evaluation.trade_pipeline_result,
            planned_quantity=evaluation.planned_quantity,
            reason=evaluation.planned_quantity_reason,
        )
        trade_events.record_trade_plan_exit_result(
            symbol_config=symbol_config,
            action=action,
            trade_pipeline_result=evaluation.trade_pipeline_result,
            planned_exit=evaluation.planned_exit,
            reason=evaluation.planned_exit_reason,
        )
        if action == CLOSE_POSITION_ACTION:
            intent = required(evaluation.intent, "intent")
            self._record_close_event(evaluation, CLOSE_POSITION_REQUESTED)
            self.ports.record_transition(
                evaluation,
                to_state=SymbolOperationalState.CLOSE_REQUESTED,
                reason=intent.reason,
                stage="CLOSE_POSITION_REQUEST",
                action=action,
            )

    def _record_close_event(self, evaluation: SymbolEvaluationContext, event_type: str) -> None:
        self.ports.trade_events.record_close_position_event(
            event_type,
            symbol_config=evaluation.evaluation_input.symbol_config,
            intent=evaluation.intent,
            heavy_snapshot=evaluation.heavy,
            now=evaluation.evaluation_input.now,
            planned_quantity=evaluation.planned_quantity,
            trade_plan_id=evaluation.trade_plan_id,
            planned_exit=evaluation.planned_exit,
        )

    # --- pre-execution guard ----------------------------------------------------------------

    def _expected_protection(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
    ) -> tuple[ExpectedProtection | None, str | None]:
        symbol_config = evaluation.evaluation_input.symbol_config
        try:
            protection = build_pre_entry_expected_protection(
                symbol_config=symbol_config,
                action=action,
                intent=evaluation.intent,
                heavy_snapshot=evaluation.heavy,
                planned_exit=evaluation.planned_exit,
                trade_plan_id=evaluation.trade_plan_id,
                dynamic_exit_enabled=self.ports.config.dynamic_exit_enabled,
            )
            return protection, None
        except Exception as error:
            self.ports.journal.append(
                "pre_entry_protection_preview_failed",
                **self.ports.trace_payload(),
                symbol=symbol_config.symbol,
                action=action,
                trade_plan_id=evaluation.trade_plan_id,
                reason=str(error),
                error_type=type(error).__name__,
            )
            return None, f"{type(error).__name__}: {error}"

    def _pre_execution_guard(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
    ) -> SymbolEvaluationResult | None:
        symbol_config = evaluation.evaluation_input.symbol_config
        intent = required(evaluation.intent, "intent")
        heavy = required(evaluation.heavy, "heavy")
        config = self.ports.config
        expected_protection, protection_preview_error = self._expected_protection(
            evaluation, action
        )
        evaluation.effective_protection_plan = expected_protection
        decision = self.ports.pre_execution_guard.evaluate(
            symbol=symbol_config.symbol,
            action=action,
            trade_plan_id=evaluation.trade_plan_id,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            final_economic_validation=evaluation.final_economic_validation,
            portfolio_decision=evaluation.portfolio_decision,
            position=heavy.position,
            runtime_status=evaluation.status,
            dynamic_exit_enabled=config.dynamic_exit_enabled,
            dynamic_sizing_enabled=config.dynamic_sizing_enabled,
            order_execution_plan=evaluation.order_execution_plan,
            learning_changed_execution_payload=evaluation.learning_changed_execution_payload,
            learning_payload_revalidated=evaluation.learning_payload_revalidated,
            market_price=(
                intent.market_price if intent.market_price is not None else heavy.market.price
            ),
            mark_price=heavy.mark_price,
            protection_working_type=symbol_config.protection_working_type.value,
            expected_protection=expected_protection,
            max_price_drift_bps=pre_entry_max_price_drift_bps(config),
            protection_preview_error=protection_preview_error,
        )
        evaluation.pre_execution_guard_decision = decision
        self.ports.trade_events.record_pre_execution_guard_decision(
            decision,
            intent_id=intent.intent_id,
        )
        if decision.allowed:
            return None
        return self.block(
            evaluation,
            reason=decision.reason,
            stage="PRE_EXECUTION_GUARD",
            classification="seguranca",
            action=action,
        )

    def _dispatch_readiness_block(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
    ) -> SymbolEvaluationResult | None:
        context: CycleContext | None = evaluation.cycle_context
        readiness = None
        if context is not None:
            readiness = context.snapshot_execution_diagnostics.get(
                evaluation.evaluation_input.symbol_config.symbol
            )
        if action_increases_risk(action) and (
            readiness is None or not readiness.readiness.allows_entry
        ):
            return self.block(
                evaluation,
                reason=wire_names.REASON_ENTRY_NOT_SAFE_AT_DISPATCH,
                stage=wire_names.STAGE_DISPATCH_READINESS,
                classification="seguranca",
                action=action,
                diagnostics={} if readiness is None else readiness.to_dict(),
            )
        if (
            action_reduces_or_repairs_risk(action)
            and readiness is not None
            and not readiness.readiness.allows_exit
        ):
            return self.block(
                evaluation,
                reason=wire_names.REASON_EXIT_NOT_SAFE_AT_DISPATCH,
                stage=wire_names.STAGE_DISPATCH_READINESS,
                classification="seguranca",
                action=action,
                diagnostics=readiness.to_dict(),
            )
        return None

    # --- approval and dispatch --------------------------------------------------------------

    def _approve(self, evaluation: SymbolEvaluationContext, action: str) -> ExecutionCommand:
        """Freeze the trade, record its approval and build the command that will be sent."""

        symbol_config = evaluation.evaluation_input.symbol_config
        intent = required(evaluation.intent, "intent")
        portfolio_decision = required(evaluation.portfolio_decision, "portfolio_decision")
        guard_decision = required(
            evaluation.pre_execution_guard_decision, "pre_execution_guard_decision"
        )
        if isinstance(evaluation.planned_trade, PlannedTrade):
            evaluation.validated_trade = ValidatedTrade(
                planned_trade=evaluation.planned_trade,
                portfolio_decision=portfolio_decision,
                final_economic_validation=evaluation.final_economic_validation,
            )
        self.ports.journal.append(
            TRADE_APPROVED,
            **self.ports.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            intent_id=intent.intent_id,
            reason=portfolio_decision.reason,
            decision=portfolio_decision,
            trade_plan_id=evaluation.trade_plan_id,
            planned_quantity=evaluation.planned_quantity,
            planned_stop_price=planned_exit_stop(evaluation.planned_exit),
            planned_take_profit_price=planned_exit_take(evaluation.planned_exit),
            pre_execution_guard=pre_execution_guard_payload(guard_decision),
            **_ai_signal_audit_fields(evaluation.score, final_action=action),
        )
        self.ports.record_finalized(
            symbol_config=symbol_config,
            action=action,
            activation_decision=evaluation.intelligent_order_activation,
            governor_decision=portfolio_decision,
            final_status=INTELLIGENT_FINAL_STATUS_READY_FOR_EXECUTION,
            final_reason=portfolio_decision.reason,
            planned_quantity=evaluation.planned_quantity,
            planned_exit=evaluation.planned_exit,
            planned_quantity_reason=evaluation.planned_quantity_reason,
            planned_exit_reason=evaluation.planned_exit_reason,
            execution_will_run=True,
        )
        self.outcomes.record_flow_decision(
            symbol=symbol_config.symbol,
            stage="EXECUTION_APPROVED",
            classification="operacional",
            reason=portfolio_decision.reason,
            terminal=False,
            execution_will_run=True,
            action=action,
        )
        self.ports.record_transition(
            evaluation,
            to_state=SymbolOperationalState.EXECUTION_APPROVED,
            reason=guard_decision.reason,
            stage="PRE_EXECUTION_GUARD",
            action=action,
        )
        planned_exit = evaluation.planned_exit
        command = ExecutionCommand.from_intent(
            intent=intent,
            heavy_snapshot=required(evaluation.heavy, "heavy"),
            planned_quantity=evaluation.planned_quantity,
            trade_plan_id=evaluation.trade_plan_id,
            planned_stop_price=planned_exit["stop_price"] if planned_exit is not None else None,
            planned_take_profit_price=(
                planned_exit["take_profit_price"] if planned_exit is not None else None
            ),
            planned_exit_source=(
                planned_exit.get("source") if isinstance(planned_exit, dict) else None
            ),
            protection_plan=evaluation.effective_protection_plan,
        )
        evaluation.execution_command = command
        if evaluation.validated_trade is not None:
            evaluation.executable_trade = ExecutableTrade(
                validated_trade=evaluation.validated_trade,
                execution_command=command,
            )
        return command

    def _dispatch(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
        command: ExecutionCommand,
    ) -> ExecutionResult:
        symbol_config = evaluation.evaluation_input.symbol_config
        intent = required(evaluation.intent, "intent")
        guard_decision = required(
            evaluation.pre_execution_guard_decision, "pre_execution_guard_decision"
        )
        self.ports.trade_events.record_execution_payload_sent(
            symbol_config=symbol_config,
            intent=intent,
            execution_command=command,
            heavy_snapshot=evaluation.heavy,
            score=evaluation.score,
            planned_quantity=evaluation.planned_quantity,
            trade_plan_id=evaluation.trade_plan_id,
            planned_exit=evaluation.planned_exit,
            final_economic_validation=required(
                evaluation.final_economic_validation, "final_economic_validation"
            ),
        )
        self.ports.journal.append(
            EXECUTION_APPROVED,
            **self.ports.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            execution_command_id=command.command_id,
            intent_id=intent.intent_id,
            trade_plan_id=evaluation.trade_plan_id,
            reason=guard_decision.reason,
            position_changed=False,
        )
        if action in OPEN_POSITION_ACTIONS:
            self.ports.record_transition(
                evaluation,
                to_state=SymbolOperationalState.ENTRY_SENT,
                reason=EXECUTION_PAYLOAD_SENT,
                stage="EXECUTION_PAYLOAD",
                action=action,
            )
        if action == CLOSE_POSITION_ACTION:
            self._record_close_event(evaluation, CLOSE_POSITION_SENT)
            self.ports.record_transition(
                evaluation,
                to_state=SymbolOperationalState.CLOSE_SENT,
                reason=intent.reason,
                stage="CLOSE_POSITION_SENT",
                action=action,
            )
        protection = evaluation.effective_protection_plan
        self.ports.journal.append(
            COMMAND_DISPATCHED,
            **self.ports.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            execution_command_id=command.command_id,
            intent_id=intent.intent_id,
            trade_plan_id=evaluation.trade_plan_id,
            planned_quantity=evaluation.planned_quantity,
            planned_stop_price=planned_exit_stop(evaluation.planned_exit),
            planned_take_profit_price=planned_exit_take(evaluation.planned_exit),
            effective_stop_price=protection.stop_price if protection is not None else None,
            effective_take_profit_price=(
                protection.take_profit_price if protection is not None else None
            ),
            protection_working_type=protection.working_type if protection is not None else None,
        )
        execute_trade = getattr(self.ports.execution_coordinator, "execute_trade", None)
        execution_result: ExecutionResult
        if evaluation.executable_trade is not None and callable(execute_trade):
            execution_result = execute_trade(
                symbol_config=symbol_config,
                heavy_snapshot=required(evaluation.heavy, "heavy"),
                trade=evaluation.executable_trade,
            )
        else:
            execution_result = self.ports.execution_coordinator.execute(
                symbol_config=symbol_config,
                heavy_snapshot=required(evaluation.heavy, "heavy"),
                command=command,
            )
        return execution_result

    # --- after the dispatch -----------------------------------------------------------------

    def _record_execution_result(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
        command: ExecutionCommand,
        execution_result: ExecutionResult,
    ) -> SymbolEvaluationResult:
        symbol_config = evaluation.evaluation_input.symbol_config
        self.ports.journal.append(
            execution_event_type(execution_result),
            **self.ports.trace_payload(),
            symbol=symbol_config.symbol,
            action=action,
            execution_command_id=command.command_id,
            intent_id=required(evaluation.intent, "intent").intent_id,
            trade_plan_id=evaluation.trade_plan_id,
            status=execution_result.status.value,
            position_changed=execution_result.position_changed,
            reason=execution_result.reason,
            order_ids=execution_result.order_ids,
            diagnostics=execution_result.diagnostics,
        )
        if (
            self.ports.config.mutations_enabled
            or getattr(getattr(self.ports.config, "execution_mode", None), "is_paper", False)
        ) and (execution_result.position_changed or execution_result.order_ids):
            self.ports.invalidate_private_state(
                evaluation.cycle_context,
                symbol=symbol_config.symbol,
                reason=execution_result.reason,
            )
        self._record_result_transition(evaluation, action, command, execution_result)
        return StageOutcomes.result(
            evaluation,
            SymbolEvaluationStageResult(
                stage=execution_result.status.value,
                classification="operacional",
                reason=execution_result.reason,
                terminal=False,
                execution_will_run=True,
                action=action,
                status=execution_result.status.value,
                diagnostics={
                    "execution_command_id": command.command_id,
                    "position_changed": execution_result.position_changed,
                    "order_ids": execution_result.order_ids,
                },
            ),
        )

    def _record_result_transition(
        self,
        evaluation: SymbolEvaluationContext,
        action: str,
        command: ExecutionCommand,
        result: ExecutionResult,
    ) -> None:
        to_state: SymbolOperationalState
        stage = result.status.value
        if action in OPEN_POSITION_ACTIONS and result.position_changed:
            self.ports.count_new_position()
            to_state = SymbolOperationalState.POSITION_OPEN
            diagnostics: dict[str, Any] = {
                "execution_command_id": command.command_id,
                "order_ids": result.order_ids,
            }
        elif action == CLOSE_POSITION_ACTION and result.position_changed:
            to_state = SymbolOperationalState.FLAT_CONFIRMED
            diagnostics = {
                "execution_command_id": command.command_id,
                "order_ids": result.order_ids,
            }
        elif result.status is ExecutionStatus.BLOCKED:
            to_state = SymbolOperationalState.BLOCKED
            diagnostics = {
                "execution_command_id": command.command_id,
                "position_changed": result.position_changed,
            }
        elif result.status in {ExecutionStatus.UNCERTAIN, ExecutionStatus.FAILED}:
            to_state = SymbolOperationalState.ERROR
            diagnostics = {
                "execution_command_id": command.command_id,
                "position_changed": result.position_changed,
            }
        elif (
            action in OPEN_POSITION_ACTIONS
            and result.recovery_status is PositionRecoveryStatus.ROLLED_BACK
        ):
            to_state = SymbolOperationalState.FLAT_CONFIRMED
            stage = "ROLLED_BACK"
            diagnostics = {
                "execution_command_id": command.command_id,
                "recovery_status": result.recovery_status.value,
            }
        else:
            return
        self.ports.record_transition(
            evaluation,
            to_state=to_state,
            reason=result.reason,
            stage=stage,
            action=action,
            diagnostics=diagnostics,
        )
