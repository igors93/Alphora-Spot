"""Probabilistic AI gate: the last word on new entries proposed by an AI model."""

from __future__ import annotations

from typing import Any, Protocol

from domain.models import FuturesPosition
from governor.evaluation.outcomes import EventJournal
from governor.evaluation.runtime_board import StrategyNameSource
from governor.governor_trade_helpers import (
    EXECUTION_OPEN_ACTIONS,
    _ai_signal_audit_fields,
    _metadata_float,
)

AIGateDecision = dict[str, Any]


class _SymbolConfig(Protocol):
    @property
    def symbol(self) -> str: ...


class AIGateService:
    """Evaluate and record the AI probability gate for one symbol at a time.

    ``config`` is the application config; optional AI settings are read with
    defaults so a config without the AI section keeps the gate disabled.
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

    def record_signal(self, *, symbol_config: _SymbolConfig, score: Any, stage: str) -> None:
        metadata = getattr(score, "metadata", {}) or {}
        if not isinstance(metadata, dict):
            return
        if metadata.get("source") != "AI_PROBABILISTIC":
            return

        payload = {
            "symbol": symbol_config.symbol,
            "stage": stage,
            "strategy": self._strategy_name(),
            "score": score.score,
            "priority": score.priority.value,
            "action": score.action,
            "thresholds": {
                "min_long_probability": getattr(
                    self.config,
                    "ai_min_long_probability",
                    metadata.get("min_long_probability"),
                ),
                "min_short_probability": getattr(
                    self.config,
                    "ai_min_short_probability",
                    metadata.get("min_short_probability"),
                ),
                "min_direction_gap": getattr(
                    self.config,
                    "ai_min_direction_gap",
                    metadata.get("min_direction_gap"),
                ),
                "min_expected_value_bps": getattr(
                    self.config,
                    "ai_min_expected_value_bps",
                    metadata.get("min_expected_value_bps"),
                ),
            },
            **_ai_signal_audit_fields(score),
        }
        # One event per evaluation: the payload already names the resolved model and whether the
        # global fallback was used (it was written two or three times with the same content).
        self._journal().append("ai_probability_signal", **payload)

    def evaluate(
        self,
        *,
        symbol_config: _SymbolConfig,
        score: Any,
        action: str,
        position: FuturesPosition,
        stage: str,
    ) -> AIGateDecision | None:
        if not getattr(self.config, "ai_probabilistic_enabled", False):
            return None

        action_value = str(action or "").upper().strip()
        if action_value not in EXECUTION_OPEN_ACTIONS:
            return None

        if position.is_open:
            return None

        def decision(allowed: bool, reason: str, checks: dict[str, Any]) -> AIGateDecision:
            return _gate_payload(
                symbol=symbol_config.symbol,
                score=score,
                action=action_value,
                stage=stage,
                allowed=allowed,
                reason=reason,
                checks=checks,
            )

        metadata = getattr(score, "metadata", {}) or {}
        if not isinstance(metadata, dict) or metadata.get("source") != "AI_PROBABILISTIC":
            return decision(False, "AI_PROBABILITY_METADATA_MISSING", {})

        if not bool(metadata.get("model_available", True)):
            return decision(
                False,
                "AI_MODEL_UNAVAILABLE",
                {"model_unavailable_reason": metadata.get("model_unavailable_reason", "")},
            )

        if self._model_approval_required() and not self._model_approved(metadata):
            return decision(
                False,
                "AI_MODEL_NOT_APPROVED_FOR_ENVIRONMENT",
                {
                    "approved_for": metadata.get("approved_for", []),
                    "blocked_for": metadata.get("blocked_for", []),
                    "execution_mode": self.config.execution_mode.value,
                },
            )

        p_long = _metadata_float(metadata, "p_long_event")
        p_short = _metadata_float(metadata, "p_short_event")
        expected_value = _metadata_float(metadata, "expected_value_bps")
        if p_long is None or p_short is None or expected_value is None:
            return decision(False, "AI_PROBABILITY_METADATA_MISSING", {})

        config = self.config
        checks = {
            "p_long_event": p_long,
            "p_short_event": p_short,
            "expected_value_bps": expected_value,
            "min_long_probability": config.ai_min_long_probability,
            "min_short_probability": config.ai_min_short_probability,
            "min_direction_gap": config.ai_min_direction_gap,
            "min_expected_value_bps": config.ai_min_expected_value_bps,
            "model_source": metadata.get("model_source", ""),
            "model_path": metadata.get("model_path", ""),
            "model_id": metadata.get("model_id", ""),
            "fallback_used": metadata.get("fallback_used", False),
            "fallback_reason": metadata.get("fallback_reason", ""),
        }

        if action_value == "OPEN_LONG":
            if p_long < config.ai_min_long_probability:
                reason = "AI_PROBABILITY_BELOW_THRESHOLD"
            elif p_long - p_short < config.ai_min_direction_gap:
                reason = "AI_DIRECTION_GAP_TOO_SMALL"
            elif expected_value < config.ai_min_expected_value_bps:
                reason = "AI_EXPECTED_VALUE_TOO_LOW"
            else:
                reason = "AI_PROBABILITY_GATE_PASSED"
            return decision(reason == "AI_PROBABILITY_GATE_PASSED", reason, checks)

        if p_short < config.ai_min_short_probability:
            reason = "AI_PROBABILITY_BELOW_THRESHOLD"
        elif p_short - p_long < config.ai_min_direction_gap:
            reason = "AI_DIRECTION_GAP_TOO_SMALL"
        elif expected_value < config.ai_min_expected_value_bps:
            reason = "AI_EXPECTED_VALUE_TOO_LOW"
        else:
            reason = "AI_PROBABILITY_GATE_PASSED"
        return decision(reason == "AI_PROBABILITY_GATE_PASSED", reason, checks)

    def record_gate(
        self,
        *,
        symbol_config: _SymbolConfig,
        score: Any,
        gate: AIGateDecision,
    ) -> None:
        event_type = (
            "ai_probability_gate_passed" if gate.get("allowed") else "ai_probability_gate_blocked"
        )
        self._journal().append(
            event_type,
            symbol=symbol_config.symbol,
            stage=gate.get("stage", ""),
            status=gate.get("status", ""),
            action=gate.get("action", ""),
            reason=gate.get("reason", ""),
            checks=gate.get("checks", {}),
            **_ai_signal_audit_fields(
                score,
                final_action=gate.get("action", ""),
                block_reason=None if gate.get("allowed") else gate.get("reason", ""),
            ),
        )

    def _model_approval_required(self) -> bool:
        return bool(getattr(self.config, "ai_require_model_approved", False))

    def _model_approved(self, metadata: dict[str, Any]) -> bool:
        execution_mode = str(self.config.execution_mode.value).upper()
        blocked_for = {
            str(item).upper().strip()
            for item in list(metadata.get("blocked_for", []))
            if str(item).strip()
        }
        if execution_mode in blocked_for:
            return False
        # DRY_RUN cannot place orders and remains available for model research
        # even before an artifact is promoted to a live-capable environment.
        if execution_mode == "DRY_RUN":
            return True
        approved_for = {
            str(item).upper().strip()
            for item in list(metadata.get("approved_for", []))
            if str(item).strip()
        }
        return execution_mode in approved_for

    def _journal(self) -> EventJournal:
        if self.journal is None:
            raise RuntimeError("AIGateService needs a journal to record events")
        return self.journal

    def _strategy_name(self) -> str:
        if self.strategy is None:
            raise RuntimeError("AIGateService needs the strategy coordinator to record signals")
        return self.strategy.global_strategy_name


def _gate_payload(
    *,
    symbol: str,
    score: Any,
    action: str,
    stage: str,
    allowed: bool,
    reason: str,
    checks: dict[str, Any],
) -> AIGateDecision:
    return {
        "symbol": symbol,
        "stage": stage,
        "allowed": bool(allowed),
        "status": "PASSED" if allowed else "BLOCKED",
        "reason": reason,
        "action": action,
        "score": getattr(score, "score", 0),
        "priority": getattr(getattr(score, "priority", None), "value", ""),
        "checks": dict(checks),
    }
