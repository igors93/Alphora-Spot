"""G5: every gate result maps to the common GateDecision contract."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from domain.decisions import GateDecision, GateVerdict
from futures.planning.economic_gate import EconomicGateDecision
from futures.risk_guard import RiskGuardDecision
from futures.trend.regime_policy import RegimePolicyDecision
from governor.evaluation import StageOutcomes, SymbolRuntimeBoard
from governor.evaluation.gates import (
    from_ai_gate,
    from_defensive_block,
    from_economic_gate,
    from_portfolio_decision,
    from_regime_policy,
    from_risk_guard,
)
from governor.models import GovernorDecision
from tests.governor.stages.fakes import FakeJournal, make_evaluation


def test_gate_decision_defaults_the_message_and_freezes_its_maps() -> None:
    values = {"a": 1}
    decision = GateDecision(gate="g", verdict=GateVerdict.BLOCK, code="CODE", values=values)
    values["a"] = 2

    assert decision.message == "CODE"
    assert decision.blocked and not decision.allowed
    assert decision.values["a"] == 1
    with pytest.raises(TypeError):
        decision.values["a"] = 3  # type: ignore[index]
    assert decision.to_dict()["verdict"] == "block"


def test_gate_decision_requires_gate_and_code() -> None:
    with pytest.raises(ValueError):
        GateDecision(gate="", verdict=GateVerdict.ALLOW, code="OK")
    with pytest.raises(ValueError):
        GateDecision(gate="g", verdict=GateVerdict.ALLOW, code="")


def test_economic_and_risk_adapters_keep_values_and_limits() -> None:
    economic = from_economic_gate(
        EconomicGateDecision(
            allowed=False,
            reason="NET_GAIN_BELOW_MINIMUM",
            plan_id="plan-1",
            symbol="BTCUSDT",
            action="OPEN_LONG",
            expected_net_gain=Decimal("0.1"),
            expected_net_gain_bps=Decimal("2"),
            expected_net_loss=Decimal("1"),
            reward_risk=Decimal("0.5"),
            round_trip_cost=Decimal("0.2"),
            round_trip_cost_bps=Decimal("4"),
            minimum_profitable_move=Decimal("0.3"),
        )
    )
    risk = from_risk_guard(RiskGuardDecision(allowed=True, reason="OK", metrics={"trades": 1}))

    assert economic.gate == "economic" and economic.blocked
    assert economic.values["reward_risk"] == Decimal("0.5")
    assert economic.limits["minimum_profitable_move"] == Decimal("0.3")
    assert risk.allowed and risk.values == {"trades": 1}


def test_portfolio_adapter_reports_the_operator_message() -> None:
    decision = from_portfolio_decision(
        GovernorDecision(
            allowed=False,
            symbol="BTCUSDT",
            action="OPEN_LONG",
            reason="MAX_EXPOSURE",
            checks={"motivo_pt": "Exposicao maxima atingida"},
        )
    )

    assert decision.code == "MAX_EXPOSURE"
    assert decision.message == "Exposicao maxima atingida"


def test_payload_adapters_for_ai_defensive_and_regime_gates() -> None:
    ai = from_ai_gate({"allowed": False, "reason": "AI_EXPECTED_VALUE_TOO_LOW", "checks": {}})
    defensive = from_defensive_block({"reason": "DEFENSIVE_MODE_MARKET_SHOCK", "mode": "X"})
    regime = from_regime_policy(
        RegimePolicyDecision(allowed=True, reason="REGIME_OK", mode="OBSERVE")
    )

    assert ai.blocked and ai.code == "AI_EXPECTED_VALUE_TOO_LOW"
    assert defensive.blocked and defensive.values["mode"] == "X"
    assert regime.allowed


def test_gate_blocked_translates_in_one_place_and_rejects_allowed_gates() -> None:
    snapshots: dict = {}
    outcomes = StageOutcomes(
        journal=FakeJournal(),
        trace_payload=dict,
        board=SymbolRuntimeBoard(snapshots, strategy=SimpleNamespace(global_strategy_name="s")),
    )
    blocked = GateDecision(
        gate="portfolio", verdict=GateVerdict.BLOCK, code="MAX", message="Mensagem"
    )

    result = outcomes.gate_blocked(
        make_evaluation(), blocked, stage="PORTFOLIO_GOVERNOR", classification="operacional"
    )

    assert result.reason == "Mensagem" and snapshots["BTCUSDT"].reason == "Mensagem"
    with pytest.raises(ValueError):
        outcomes.gate_blocked(
            make_evaluation(),
            GateDecision(gate="g", verdict=GateVerdict.ALLOW, code="OK"),
            stage="S",
            classification="c",
        )
