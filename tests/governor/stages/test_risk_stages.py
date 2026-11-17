"""G5: final risk guards (18) and learning execution guards (19)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from governor.stages.risk import FinalRiskGuardsStage, LearningExecutionStage, RiskPorts
from governor.symbol_state_machine import SymbolOperationalState
from tests.governor.stages.fakes import make_evaluation, make_score
from trading.intent import TradeIntent


def _intent() -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT", action="OPEN_LONG", reason="signal", market_price=Decimal("100")
    )


class _Portfolio:
    def __init__(self, *, allowed: bool) -> None:
        self.allowed = allowed
        self.notionals: list = []

    def evaluate_trade_intent(self, intent, symbol_configs, account, *, override_notional=None):
        self.notionals.append(override_notional)
        checks = {} if self.allowed else {"motivo_pt": "Limite de exposicao atingido"}
        return SimpleNamespace(
            allowed=self.allowed,
            reason="OK" if self.allowed else "MAX_EXPOSURE",
            checks=checks,
        )


def _validation(allowed: bool):
    return SimpleNamespace(
        decision=SimpleNamespace(allowed=allowed, reason="FINAL_OK" if allowed else "FINAL_LOW")
    )


def _ports(journal, transitions, *, economic=True, portfolio=None, finalized=None, warnings=None):
    return RiskPorts(
        evaluate_final_economic_gate=lambda **_kwargs: _validation(economic),
        record_final_economic_gate_block=lambda **_kwargs: journal.append("final_gate_block"),
        portfolio_risk_engine=portfolio or _Portfolio(allowed=True),
        symbol_configs=[],
        record_finalized=lambda **kwargs: (finalized if finalized is not None else []).append(
            kwargs
        ),
        journal=journal,
        log_warning=lambda *args: (warnings if warnings is not None else []).append(args),
        record_transition=transitions,
    )


def _final_guards(outcomes, ports, *, order_plan=None) -> FinalRiskGuardsStage:
    return FinalRiskGuardsStage(
        outcomes=outcomes,
        ports=ports,
        select_order_execution_policy=lambda **_kwargs: order_plan,
        strategy_name=lambda: "test_strategy",
        execution_mode=lambda: "TESTNET_LIVE",
        environment=lambda: "TESTNET",
    )


def _evaluation(**fields):
    return make_evaluation(
        intent=_intent(), score=make_score(), planned_quantity=Decimal("0.5"), **fields
    )


def test_final_guards_approve_and_pass_the_planned_notional(outcomes, journal, transitions):
    portfolio = _Portfolio(allowed=True)
    stage = _final_guards(outcomes, _ports(journal, transitions, portfolio=portfolio))
    evaluation = _evaluation()

    assert stage(evaluation) is None

    assert stage.name == "economic_portfolio_pre_execution_inputs"
    assert portfolio.notionals == [Decimal("50.0")]
    assert journal.types() == ["governor_decision"]
    assert [call["to_state"] for call in transitions.calls] == [
        SymbolOperationalState.FINAL_ECONOMIC_APPROVED,
        SymbolOperationalState.PORTFOLIO_APPROVED,
    ]


def test_final_economic_gate_block_skips_the_flow_decision_event(outcomes, journal, transitions):
    stage = _final_guards(outcomes, _ports(journal, transitions, economic=False))

    result = stage(_evaluation())

    assert result is not None and result.stage == "FINAL_ECONOMIC_GATE"
    assert journal.types() == ["final_gate_block"]


def test_order_policy_and_portfolio_blocks(outcomes, journal, transitions, snapshots):
    order_plan = SimpleNamespace(allowed=False, reason="ORDER_POLICY_BLOCKED")
    blocked_by_policy = _final_guards(outcomes, _ports(journal, transitions), order_plan=order_plan)
    finalized: list[dict] = []
    warnings: list = []
    blocked_by_portfolio = _final_guards(
        outcomes,
        _ports(
            journal,
            transitions,
            portfolio=_Portfolio(allowed=False),
            finalized=finalized,
            warnings=warnings,
        ),
    )

    policy_result = blocked_by_policy(_evaluation())
    portfolio_result = blocked_by_portfolio(_evaluation())

    assert policy_result is not None and policy_result.stage == "ORDER_EXECUTION_POLICY"
    assert portfolio_result is not None and portfolio_result.stage == "PORTFOLIO_GOVERNOR"
    assert portfolio_result.reason == "Limite de exposicao atingido"
    assert finalized and warnings
    assert "trade_blocked_by_governor" in journal.types()
    assert snapshots["BTCUSDT"].reason == "Limite de exposicao atingido"


def _learning(outcomes, ports, *, paper=None, testnet=None, dynamic_sizing=True):
    return LearningExecutionStage(
        outcomes=outcomes,
        ports=ports,
        evaluate_paper=lambda **_kwargs: paper,
        record_testnet_observation=lambda **_kwargs: None,
        evaluate_testnet=lambda **_kwargs: testnet,
        dynamic_sizing_enabled=lambda: dynamic_sizing,
    )


def _policy(*, blocked=False, reduced=False, final_quantity=None, reason="LEARNING"):
    return SimpleNamespace(
        blocked=blocked,
        reduced=reduced,
        final_quantity=final_quantity,
        original_quantity=Decimal("0.5"),
        reason=reason,
        plan_id="plan-1",
        diagnostics={},
    )


def test_learning_without_policies_changes_nothing(outcomes, journal, transitions):
    evaluation = _evaluation()

    assert _learning(outcomes, _ports(journal, transitions))(evaluation) is None
    assert evaluation.learning_changed_execution_payload is False
    assert journal.events == []


def test_learning_paper_block_stops_the_entry(outcomes, journal, transitions):
    paper = _policy(blocked=True, reason="LEARNING_PAPER_BLOCK")

    result = _learning(outcomes, _ports(journal, transitions), paper=paper)(_evaluation())

    assert result is not None and result.stage == "LEARNING_PAPER"
    assert result.classification == "ajuste"
    assert journal.types() == [
        "trade_blocked_by_learning_paper",
        "governor_evaluate_symbol_flow_decision",
    ]


def test_learning_testnet_reduction_revalidates_economics_and_portfolio(
    outcomes, journal, transitions
):
    portfolio = _Portfolio(allowed=True)
    testnet = _policy(reduced=True, final_quantity=Decimal("0.2"), reason="TESTNET_REDUCE")
    evaluation = _evaluation()

    stage = _learning(outcomes, _ports(journal, transitions, portfolio=portfolio), testnet=testnet)

    assert stage(evaluation) is None
    assert evaluation.planned_quantity == Decimal("0.2")
    assert evaluation.learning_payload_revalidated is True
    assert portfolio.notionals == [Decimal("20.0")]
    assert transitions.calls[-1]["stage"] == "PORTFOLIO_GOVERNOR_AFTER_LEARNING"


def test_learning_testnet_reduction_is_ignored_without_dynamic_sizing(
    outcomes, journal, transitions
):
    testnet = _policy(reduced=True, final_quantity=Decimal("0.2"))
    evaluation = _evaluation()

    stage = _learning(outcomes, _ports(journal, transitions), testnet=testnet, dynamic_sizing=False)

    assert stage(evaluation) is None
    assert evaluation.planned_quantity == Decimal("0.5")
    assert journal.types() == ["learning_testnet_reduction_ignored"]
