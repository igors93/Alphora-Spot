"""Defensive mode: observe market stress each cycle and, when enforced, block new entries."""

from __future__ import annotations

from typing import Any, Protocol

from domain.models import FuturesPosition
from governor.cycle_context import CycleContext
from governor.defensive_mode import DefensiveModeEvaluation, DefensiveModeEvaluator
from governor.evaluation.outcomes import EventJournal
from governor.evaluation.runtime_board import StrategyNameSource
from governor.governor_trade_helpers import (
    _ai_signal_audit_fields,
    _is_new_entry_action,
)
from utils.events import EventChannel, observe_event

DefensiveBlock = dict[str, Any]


class _SymbolConfig(Protocol):
    @property
    def symbol(self) -> str: ...


class DefensiveModeService:
    """Build the defensive-mode evaluation and translate it into entry blocks.

    ``config`` is the application config; every defensive setting is optional
    and read with its default. The only cross-cycle state (whether the mode was
    active in the previous cycle) stays with the caller: ``record_shadow``
    receives it and returns the new value.
    """

    def __init__(
        self,
        *,
        config: Any,
        journal: EventJournal | None = None,
        strategy: StrategyNameSource | None = None,
    ) -> None:
        self.config = config
        self.journal = journal
        self.strategy = strategy

    # --- cycle level: observation ---------------------------------------------------

    def enabled(self) -> bool:
        return bool(getattr(self.config, "governor_defensive_mode_enabled", True))

    def build_evaluator(self) -> DefensiveModeEvaluator | None:
        if not self.enabled():
            return None

        config = self.config
        return DefensiveModeEvaluator(
            shadow_only=bool(getattr(config, "governor_defensive_mode_shadow_only", True)),
            min_shock_symbols=int(
                getattr(config, "governor_defensive_mode_min_shock_symbols", 2) or 2
            ),
            max_stale_symbols=int(
                getattr(config, "governor_defensive_mode_max_stale_symbols", 0) or 0
            ),
            max_price_age_seconds=float(
                getattr(
                    config,
                    "governor_defensive_mode_max_price_age_seconds",
                    5.0,
                )
                or 0.0
            ),
            budget_pressure_enabled=bool(
                getattr(
                    config,
                    "governor_defensive_mode_budget_pressure_enabled",
                    True,
                )
            ),
        )

    def evaluate_shadow(
        self,
        *,
        shock_events: list[dict[str, Any]],
        fast_lane_symbols: list[str],
        market_health: dict[str, Any],
        request_budget_summary: dict[str, Any],
        open_symbols: set[str],
        dirty_symbols: list[str],
    ) -> DefensiveModeEvaluation | None:
        evaluator = self.build_evaluator()
        if evaluator is None:
            return None

        return evaluator.evaluate(
            shock_events=shock_events,
            fast_lane_symbols=fast_lane_symbols,
            market_health=market_health,
            request_budget_summary=request_budget_summary,
            open_symbols=open_symbols,
            dirty_symbols=dirty_symbols,
        )

    @staticmethod
    def summary(evaluation: DefensiveModeEvaluation | None) -> dict[str, Any]:
        if evaluation is None:
            return {
                "enabled": False,
                "mode": "DISABLED",
                "shadow_only": True,
            }

        return {
            "enabled": True,
            **evaluation.to_dict(),
        }

    def record_shadow(
        self,
        evaluation: DefensiveModeEvaluation | None,
        *,
        previously_active: bool,
    ) -> bool:
        """Record the cycle's evaluation and return whether the mode is now active."""

        if evaluation is None:
            return previously_active

        active = evaluation.mode != "NORMAL"
        if active and not previously_active:
            observe_event(
                "defensive_mode_activated",
                component=__name__,
                category="governor",
                channel=EventChannel.AUDIT,
                severity=evaluation.severity,
                mode=evaluation.mode,
                reason=evaluation.reason,
                shadow_only=evaluation.shadow_only,
                diagnostics=evaluation.diagnostics,
            )
        elif not active and previously_active:
            observe_event(
                "defensive_mode_deactivated",
                component=__name__,
                category="governor",
                channel=EventChannel.AUDIT,
                mode=evaluation.mode,
                reason=evaluation.reason,
                shadow_only=evaluation.shadow_only,
            )
        self._journal().append(
            "governor_defensive_mode_shadow",
            status="OBSERVED",
            mode=evaluation.mode,
            reason=evaluation.reason,
            severity=evaluation.severity,
            shadow_only=evaluation.shadow_only,
            would_block_new_entries=evaluation.would_block_new_entries,
            would_reduce_new_entries=evaluation.would_reduce_new_entries,
            would_prioritize_protection=evaluation.would_prioritize_protection,
            would_allow_closing=evaluation.would_allow_closing,
            would_allow_stop_take_adjustment=(evaluation.would_allow_stop_take_adjustment),
            execution_changed=False,
            order_mutation=False,
            diagnostics=evaluation.diagnostics,
        )
        return active

    # --- symbol level: enforcement ---------------------------------------------------

    def enforcement_enabled(self) -> bool:
        return bool(
            getattr(
                self.config,
                "governor_defensive_mode_enforcement_enabled",
                False,
            )
        )

    def block_new_entries_enabled(self) -> bool:
        return bool(
            getattr(
                self.config,
                "governor_defensive_mode_block_new_entries",
                True,
            )
        )

    def new_entry_block_decision(
        self,
        *,
        symbol_config: _SymbolConfig,
        score: Any,
        position: FuturesPosition,
        context: CycleContext | None,
        action: str | None = None,
        stage: str = "PRE_HEAVY_SCORE",
    ) -> DefensiveBlock | None:
        evaluation = getattr(context, "defensive_mode", None)
        if evaluation is None:
            return None

        if evaluation.shadow_only:
            return None

        if not self.enforcement_enabled():
            return None

        if not self.block_new_entries_enabled():
            return None

        if not evaluation.would_block_new_entries:
            return None

        if position.is_open:
            return None

        action_value = str(action or getattr(score, "action", "") or "").upper().strip()
        if not _is_new_entry_action(action_value):
            return None

        return {
            "symbol": symbol_config.symbol,
            "status": "BLOCKED",
            "reason": f"DEFENSIVE_MODE_{evaluation.mode}",
            "mode": evaluation.mode,
            "mode_reason": evaluation.reason,
            "severity": evaluation.severity,
            "action": action_value,
            "stage": stage,
            "shadow_only": evaluation.shadow_only,
            "would_block_new_entries": evaluation.would_block_new_entries,
            "execution_changed": True,
            "order_mutation": False,
            "diagnostics": {
                **evaluation.diagnostics,
                "safety_note": (
                    "Fase 10H: Defensive Mode ativo parcial bloqueou apenas "
                    "nova entrada. Fechamento, protecao e ajustes de stop/take "
                    "permanecem permitidos."
                ),
            },
        }

    def record_active_block(
        self,
        *,
        symbol_config: _SymbolConfig,
        score: Any,
        decision: DefensiveBlock,
    ) -> None:
        self._journal().append(
            "trade_blocked_by_defensive_mode",
            symbol=symbol_config.symbol,
            strategy=self._strategy_name(),
            score=score.score,
            priority=score.priority.value,
            action=decision["action"],
            stage=decision.get("stage", ""),
            status=decision["status"],
            reason=decision["reason"],
            defensive_mode=decision["mode"],
            defensive_reason=decision["mode_reason"],
            severity=decision["severity"],
            shadow_only=decision["shadow_only"],
            execution_changed=decision["execution_changed"],
            order_mutation=decision["order_mutation"],
            diagnostics=decision["diagnostics"],
            **_ai_signal_audit_fields(
                score,
                final_action="BLOCKED",
                block_reason=decision["reason"],
            ),
        )

    @staticmethod
    def register_block(*, context: CycleContext | None, decision: DefensiveBlock) -> None:
        if context is None:
            return

        context.defensive_blocks.append(decision)

    def _journal(self) -> EventJournal:
        if self.journal is None:
            raise RuntimeError("DefensiveModeService needs a journal to record events")
        return self.journal

    def _strategy_name(self) -> str:
        if self.strategy is None:
            raise RuntimeError("DefensiveModeService needs the strategy coordinator")
        return self.strategy.global_strategy_name
