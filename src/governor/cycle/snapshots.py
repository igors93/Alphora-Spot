"""The cycle's operational snapshot session: baseline, selected symbols and just-in-time reads."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from config.symbol_context import SymbolTradingConfig
from governor.cycle_context import CycleContext
from governor.evaluation.outcomes import EventJournal
from market_data.snapshot_provider import (
    ACCOUNT,
    SnapshotReadiness,
    snapshot_error_diagnostics_payload,
    wire_names,
)
from market_data.snapshot_provider import (
    position as snapshot_position_key,
)
from utils.events import EventChannel, observe_event


class CycleSnapshots:
    """Open, refresh and close the snapshot session that pins one cycle's market view.

    Every failure degrades to the legacy per-symbol reads (fail-closed for entries,
    decided later by the readiness diagnostics); none of them stops the cycle.
    ``provider`` is the operational snapshot provider, ``None`` when it is disabled.
    """

    def __init__(
        self,
        *,
        provider: Any,
        execution_engine: Any,
        router: Any,
        symbol_configs: Sequence[SymbolTradingConfig],
        journal: EventJournal,
        logger: Any,
    ) -> None:
        self.provider = provider
        self.execution_engine = execution_engine
        self.router = router
        self.symbol_configs = symbol_configs
        self.journal = journal
        self.logger = logger

    def _symbols(self) -> list[str]:
        return [item.symbol for item in self.symbol_configs]

    def collect_health(self, label: str) -> dict[str, Any]:
        if self.provider is None:
            return {}
        collect = getattr(self.provider, "collect_health", None)
        if not callable(collect):
            return {}
        try:
            return dict(collect(label) or {})
        except Exception as error:
            self.logger.warning(
                "Falha ao coletar health do snapshot engine | label=%s | error=%s", label, error
            )
            return {}

    # --- cycle start --------------------------------------------------------------------

    def _local_positions(self) -> dict[str, Any] | None:
        get_positions = getattr(self.execution_engine, "get_positions", None)
        if callable(get_positions):
            try:
                return dict(get_positions(self.symbol_configs))
            except Exception as error:
                self.logger.warning(
                    "Falha ao carregar posicoes globais para baseline de snapshots: %s",
                    error,
                )

        fallback_positions = {}
        for symbol_config in self.symbol_configs:
            try:
                fallback_positions[symbol_config.symbol] = self.execution_engine.get_position(
                    symbol_config
                )
            except Exception as error:
                self.logger.warning(
                    "Falha ao carregar posicao para baseline de snapshots | symbol=%s | error=%s",
                    symbol_config.symbol,
                    error,
                )
        return fallback_positions or None

    def prepare_baseline(self, context: CycleContext) -> None:
        provider = self.provider
        if provider is None:
            return

        local_positions = self._local_positions()
        session = None
        try:
            session = provider.open_cycle_session(
                cycle_id=context.cycle_id,
                now=context.now,
                symbols=self._symbols(),
                local_positions=local_positions,
            )
            context.snapshot_session = session
            snapshot = session.resolve_baseline()
            context.operational_snapshot = snapshot
            context.snapshot_baseline_diagnostic = provider.readiness_diagnostic(
                snapshot,
                profile="baseline",
                symbols=self._symbols(),
            )
            context.snapshot_baseline_ready = (
                context.snapshot_baseline_readiness >= SnapshotReadiness.READ_ONLY
            )
            self.apply_baseline(context)
        except Exception as error:
            self.logger.warning(
                "Falha ao preparar baseline de snapshots do ciclo; fallback legado ativo: %s",
                error,
            )
            observe_event(
                wire_names.CYCLE_BASELINE_FAILED,
                component=__name__,
                category="governor",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                cycle_id=context.cycle_id,
                reason=str(error),
                error_type=type(error).__name__,
                diagnostics=snapshot_error_diagnostics_payload(error),
            )
            context.snapshot_baseline_diagnostic = provider.readiness_diagnostic(
                getattr(error, "snapshot", None),
                profile="baseline",
                symbols=self._symbols(),
                error=error,
            )
            if session is not None:
                try:
                    session.close()
                except Exception as close_error:
                    self.logger.warning(
                        "Falha ao fechar sessao de snapshots apos erro de baseline: %s",
                        close_error,
                    )
            context.snapshot_session = None
            self.collect_health("snapshot_failed")

    def apply_baseline(self, context: CycleContext) -> None:
        """Copy the account and the positions of the session snapshot into the cycle."""

        snapshot = context.operational_snapshot
        if snapshot is None:
            return

        account_item = snapshot.resources.get(ACCOUNT)
        if account_item is not None and isinstance(account_item.value, dict):
            context.account_info = dict(account_item.value)
            context.account_info_loaded = True
        elif ACCOUNT in snapshot.errors:
            context.account_info = None
            context.account_info_loaded = True

        for symbol_config in self.symbol_configs:
            item = snapshot.resources.get(snapshot_position_key(symbol_config.symbol))
            if item is not None:
                context.positions[symbol_config.symbol] = item.value

        if context.account_info_loaded or context.positions:
            context.private_state_loaded_at = time.time()

    def prepare_selected(self, context: CycleContext, symbols: list[str]) -> None:
        session = context.snapshot_session
        if session is None:
            return
        try:
            resolve_light_selected = getattr(session, "resolve_light_selected", None)
            snapshot = (
                resolve_light_selected(symbols)
                if callable(resolve_light_selected)
                else session.resolve_selected(symbols)
            )
            context.operational_snapshot = snapshot
            self.apply_baseline(context)
        except Exception as error:
            self.logger.warning(
                "Falha ao preparar snapshot operacional dos simbolos selecionados; "
                "fallback por simbolo ativo: %s%s",
                error,
                format_snapshot_violation_detail(error),
            )
            observe_event(
                wire_names.CYCLE_SELECTED_FAILED,
                component=__name__,
                category="governor",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                cycle_id=context.cycle_id,
                symbols=symbols,
                reason=str(error),
                error_type=type(error).__name__,
                diagnostics=snapshot_error_diagnostics_payload(error),
            )
            try:
                context.operational_snapshot = session.snapshot()
            except Exception as snapshot_error:
                self.logger.warning(
                    "Falha ao recuperar snapshot operacional parcial: %s",
                    snapshot_error,
                )
            context.snapshot_selected_diagnostic = session.provider.readiness_diagnostic(
                getattr(error, "snapshot", None) or context.operational_snapshot,
                profile="evaluation",
                symbols=symbols,
                error=error,
            )
            self.collect_health("snapshot_failed")

    # --- just-in-time read before one symbol's heavy stages ------------------------------

    def prepare_symbol_evaluation(self, context: CycleContext | None, symbol: str) -> None:
        """Resolve one symbol's heavy inputs immediately before its heavy evaluation.

        Multi-symbol live execution is deliberately sequential. A snapshot prefetched for all
        symbols at cycle start can therefore be several seconds old by the time the second or
        third symbol reaches the heavy stage. Resolving only the current symbol here avoids
        order-dependent stale-data failures while preserving fail-closed behavior.
        """

        if context is None or context.force_isolated_heavy_reads:
            return
        session = context.snapshot_session
        if session is None:
            return

        try_resolve_symbol = getattr(session, "try_resolve_symbol_evaluation", None)
        if callable(try_resolve_symbol):
            self._try_resolve_symbol(context, session, try_resolve_symbol, symbol)
            return

        resolve_symbol = getattr(session, "resolve_symbol_evaluation", None)
        if callable(resolve_symbol):
            self._resolve_symbol(context, session, resolve_symbol, symbol)

    def _try_resolve_symbol(
        self,
        context: CycleContext,
        session: Any,
        try_resolve_symbol: Any,
        symbol: str,
    ) -> None:
        try:
            result = try_resolve_symbol(symbol)
            context.operational_snapshot = result.snapshot
            context.snapshot_selected_diagnostic = (
                session.provider.readiness_diagnostic_from_result(
                    result,
                    profile="evaluation",
                    symbols=[symbol],
                )
            )
            context.snapshot_selected_ready = (
                context.snapshot_selected_readiness >= SnapshotReadiness.READ_ONLY
            )
            observe_event(
                wire_names.SYMBOL_EVALUATION_TRY_RESOLVED,
                component=__name__,
                category="governor",
                channel=EventChannel.AUDIT,
                cycle_id=context.cycle_id,
                symbol=symbol,
                state=result.state.value,
                reason=result.reason,
                allowed_actions=list(result.allowed_actions),
                diagnostics=result.to_dict(),
            )
            if result.rejected:
                context.prefetched_heavy_errors[str(symbol).upper()] = result.reason
                return
            self.apply_baseline(context)
        except Exception as error:
            self._record_symbol_snapshot_failure(
                context,
                session,
                symbol,
                error,
                message=(
                    "Falha ao preparar snapshot operacional just-in-time (try); "
                    "fallback isolado do simbolo ativo | symbol=%s | error=%s%s"
                ),
            )

    def _resolve_symbol(
        self,
        context: CycleContext,
        session: Any,
        resolve_symbol: Any,
        symbol: str,
    ) -> None:
        try:
            snapshot = resolve_symbol(symbol)
            context.operational_snapshot = snapshot
            context.snapshot_selected_diagnostic = session.provider.readiness_diagnostic(
                snapshot,
                profile="evaluation",
                symbols=[symbol],
            )
            context.snapshot_selected_ready = (
                context.snapshot_selected_readiness >= SnapshotReadiness.READ_ONLY
            )
            self.apply_baseline(context)
        except Exception as error:
            self._record_symbol_snapshot_failure(
                context,
                session,
                symbol,
                error,
                message=(
                    "Falha ao preparar snapshot operacional just-in-time; "
                    "fallback isolado do simbolo ativo | symbol=%s | error=%s%s"
                ),
            )

    def _record_symbol_snapshot_failure(
        self,
        context: CycleContext,
        session: Any,
        symbol: str,
        error: Exception,
        *,
        message: str,
    ) -> None:
        self.logger.warning(message, symbol, error, format_snapshot_violation_detail(error))
        observe_event(
            wire_names.SYMBOL_EVALUATION_SNAPSHOT_FAILED,
            component=__name__,
            category="governor",
            channel=EventChannel.AUDIT,
            severity="WARNING",
            cycle_id=context.cycle_id,
            symbol=symbol,
            reason=str(error),
            error_type=type(error).__name__,
            diagnostics=snapshot_error_diagnostics_payload(error),
        )
        try:
            context.operational_snapshot = session.snapshot()
        except Exception as snapshot_error:
            self.logger.warning(
                "Falha ao recuperar snapshot operacional parcial do simbolo | symbol=%s | error=%s",
                symbol,
                snapshot_error,
            )
        self.collect_health("snapshot_failed")

    # --- after an order and at cycle end -------------------------------------------------

    def invalidate_private_state(
        self,
        context: CycleContext | None,
        *,
        symbol: str,
        reason: str,
    ) -> None:
        """Drop the cycle's private view after an order so later symbols read it again."""

        if context is None:
            return
        previous_generation = context.private_state_generation
        context.invalidate_private_state()
        invalidate_router = getattr(self.router, "invalidate_private_state_after_mutation", None)
        router_invalidated = False
        if callable(invalidate_router):
            router_invalidated = bool(invalidate_router(symbol, reason=reason))
        self.journal.append(
            "cycle_private_state_invalidated",
            severity="INFO",
            cycle_id=context.cycle_id,
            symbol=symbol,
            reason=reason,
            previous_generation=previous_generation,
            private_state_generation=context.private_state_generation,
            router_invalidated=router_invalidated,
        )

    def close(self, context: CycleContext) -> None:
        session = context.snapshot_session
        if session is None:
            return
        try:
            context.operational_snapshot = session.snapshot()
        except Exception as snapshot_error:
            self.logger.warning(
                "Falha ao capturar o snapshot operacional final: %s", snapshot_error
            )
        try:
            session.close()
        except Exception as error:
            self.logger.warning("Falha ao fechar sessao de snapshots do ciclo: %s", error)
        finally:
            context.snapshot_session = None


def format_snapshot_violation_detail(error: BaseException) -> str:
    violations = tuple(getattr(error, "violations", ()) or ())
    if not violations:
        skew = getattr(error, "observation_skew_seconds", None)
        if skew is not None:
            oldest = getattr(error, "oldest_key", "?")
            newest = getattr(error, "newest_key", "?")
            return f" | skew={skew:.2f}s oldest={oldest} newest={newest}"
        return ""
    parts: list[str] = []
    for violation in violations:
        reason = str(getattr(getattr(violation, "reason", ""), "value", "") or "")
        keys = [str(k) for k in getattr(violation, "keys", ())]
        age = getattr(violation, "current_age_seconds", None)
        max_age = getattr(violation, "max_age_seconds", None)
        key_str = ",".join(keys)
        if age is not None and max_age is not None:
            parts.append(f"{reason}:{key_str}(age={age:.1f}s>max={max_age:.1f}s)")
        else:
            parts.append(f"{reason}:{key_str}")
    return f" | detalhes=[{'; '.join(parts)}]"
