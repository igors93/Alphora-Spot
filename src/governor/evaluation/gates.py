"""Adapters from each Governor gate result to ``domain.decisions.GateDecision``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from domain.decisions import GateDecision, GateVerdict
from futures.planning.economic_gate import EconomicGateDecision
from futures.risk_guard import RiskGuardDecision
from futures.trend.regime_policy import RegimePolicyDecision
from governor.models import GovernorDecision
from governor.pre_execution_guard import PreExecutionGuardDecision


def _verdict(allowed: bool) -> GateVerdict:
    return GateVerdict.ALLOW if allowed else GateVerdict.BLOCK


def from_economic_gate(decision: EconomicGateDecision) -> GateDecision:
    return GateDecision(
        gate="economic",
        verdict=_verdict(decision.allowed),
        code=decision.reason,
        values={
            "expected_net_gain": decision.expected_net_gain,
            "expected_net_gain_bps": decision.expected_net_gain_bps,
            "expected_net_loss": decision.expected_net_loss,
            "reward_risk": decision.reward_risk,
            "round_trip_cost": decision.round_trip_cost,
            "round_trip_cost_bps": decision.round_trip_cost_bps,
        },
        limits={"minimum_profitable_move": decision.minimum_profitable_move},
    )


def from_portfolio_decision(decision: GovernorDecision) -> GateDecision:
    """Portfolio risk: the stage reports the operator message (``motivo_pt``)."""

    checks = dict(decision.checks or {})
    return GateDecision(
        gate="portfolio",
        verdict=_verdict(decision.allowed),
        code=decision.reason,
        message=str(checks.get("motivo_pt", decision.reason)),
        values=checks,
    )


def from_pre_execution_guard(decision: PreExecutionGuardDecision) -> GateDecision:
    return GateDecision(
        gate="pre_execution",
        verdict=_verdict(decision.allowed),
        code=decision.reason,
        message=decision.motivo_pt or decision.reason,
        values={
            "severity": decision.severity,
            "economic_gate_allowed": decision.economic_gate_allowed,
            "portfolio_allowed": decision.portfolio_allowed,
            "position_state": decision.position_state,
        },
    )


def from_risk_guard(decision: RiskGuardDecision) -> GateDecision:
    return GateDecision(
        gate="risk_guard",
        verdict=_verdict(decision.allowed),
        code=decision.reason,
        values=dict(decision.metrics),
    )


def from_regime_policy(decision: RegimePolicyDecision) -> GateDecision:
    return GateDecision(
        gate="regime_policy",
        verdict=_verdict(decision.allowed),
        code=decision.reason,
        values={"against_trend": decision.against_trend, "mode": decision.mode},
    )


def from_ai_gate(decision: Mapping[str, Any]) -> GateDecision:
    """The AI probability gate payload from ``AIGateService.evaluate``."""

    return GateDecision(
        gate="ai_probability",
        verdict=_verdict(bool(decision["allowed"])),
        code=str(decision["reason"]),
        values=dict(decision.get("checks", {})),
    )


def from_defensive_block(decision: Mapping[str, Any]) -> GateDecision:
    """A defensive-mode block payload from ``DefensiveModeService``."""

    return GateDecision(
        gate="defensive_mode",
        verdict=GateVerdict.BLOCK,
        code=str(decision["reason"]),
        values={
            "mode": decision.get("mode"),
            "mode_reason": decision.get("mode_reason"),
            "severity": decision.get("severity"),
        },
    )


__all__ = [
    "from_ai_gate",
    "from_defensive_block",
    "from_economic_gate",
    "from_portfolio_decision",
    "from_pre_execution_guard",
    "from_regime_policy",
    "from_risk_guard",
]
