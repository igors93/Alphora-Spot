"""M3 G8: the services that replaced TradePipelineMixin, tested without a Governor."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from governor.events import (
    CANDIDATE_NET_GAIN_BELOW_ECONOMIC_TRADE_PLAN,
    CANDIDATE_REWARD_RISK_BELOW_ECONOMIC_TRADE_PLAN,
)
from governor.services.adaptive_management import AdaptiveManagementService
from governor.services.economic_gate import intelligent_exit_candidate_rejection_reason
from governor.services.intelligent_flow import IntelligentFlowComponents, IntelligentFlowService
from governor.services.trade_planning import TradePlanningService, spread_bps_for_order_policy
from tests.governor.stages.fakes import FakeJournal

SYMBOL = SimpleNamespace(symbol="BTCUSDT")


def _validation(*, allowed=True, reason="OK", reward_risk="2", gain_bps="30"):
    return SimpleNamespace(
        decision=SimpleNamespace(
            allowed=allowed,
            reason=reason,
            reward_risk=Decimal(reward_risk),
            expected_net_gain_bps=Decimal(gain_bps),
        )
    )


@pytest.mark.parametrize(
    ("candidate", "baseline", "expected"),
    [
        (_validation(allowed=False, reason="LOW_GAIN"), _validation(), "LOW_GAIN"),
        (_validation(), _validation(allowed=False), None),
        (
            _validation(reward_risk="1.5"),
            _validation(reward_risk="2"),
            CANDIDATE_REWARD_RISK_BELOW_ECONOMIC_TRADE_PLAN,
        ),
        (
            _validation(gain_bps="20"),
            _validation(gain_bps="30"),
            CANDIDATE_NET_GAIN_BELOW_ECONOMIC_TRADE_PLAN,
        ),
        (_validation(reward_risk="2.5", gain_bps="35"), _validation(), None),
    ],
)
def test_intelligent_exit_must_be_at_least_as_economic_as_the_plan(candidate, baseline, expected):
    assert (
        intelligent_exit_candidate_rejection_reason(
            candidate_validation=candidate,
            baseline_validation=baseline,
        )
        == expected
    )


def _planning(**config) -> TradePlanningService:
    return TradePlanningService(
        config=SimpleNamespace(
            **{"dynamic_sizing_enabled": True, "dynamic_exit_enabled": True, **config}
        ),
        journal=FakeJournal(),
        pipeline_events=None,
        trade_pipeline=None,
        order_execution_policy=None,
        learning=None,
        strategy_name=lambda: "test",
        trace_payload=lambda: {},
    )


def _result(**plan):
    values = {
        "action": "OPEN_LONG",
        "status": "ALLOW_NORMAL",
        "quantity": Decimal("0.5"),
        "stop_price": Decimal("99"),
        "take_profit_price": Decimal("102"),
    }
    values.update(plan)
    return SimpleNamespace(plan=SimpleNamespace(**values))


@pytest.mark.parametrize(
    ("config", "result", "action", "expected"),
    [
        (
            {"dynamic_sizing_enabled": False},
            _result(),
            "OPEN_LONG",
            (None, "DYNAMIC_SIZING_DISABLED"),
        ),
        ({}, None, "OPEN_LONG", (None, "NO_TRADE_PLAN")),
        ({}, _result(), "CLOSE_POSITION", (None, "ACTION_NOT_OPEN")),
        ({}, _result(action="OPEN_SHORT"), "OPEN_LONG", (None, "PLAN_ACTION_MISMATCH")),
        ({}, _result(status="BLOCK"), "OPEN_LONG", (None, "PLAN_STATUS_BLOCK")),
        ({}, _result(quantity=Decimal("0")), "OPEN_LONG", (None, "PLAN_QUANTITY_NOT_POSITIVE")),
        ({}, _result(), "OPEN_LONG", (Decimal("0.5"), "TRADE_PLAN_QUANTITY_READY")),
    ],
)
def test_planned_quantity_reaches_the_order_only_from_an_executable_plan(
    config, result, action, expected
):
    assert _planning(**config).planned_quantity_for_execution(result, action=action) == expected


def test_planned_exit_requires_both_stop_and_take():
    planning = _planning()

    assert planning.planned_exit_for_execution(
        _result(take_profit_price=None), action="OPEN_LONG"
    ) == (None, "PLAN_EXIT_NOT_AVAILABLE")
    planned_exit, reason = planning.planned_exit_for_execution(_result(), action="OPEN_LONG")
    assert reason == "TRADE_PLAN_EXIT_READY"
    assert planned_exit["stop_price"] == Decimal("99")
    assert planned_exit["take_profit_price"] == Decimal("102")


def test_order_policy_spread_prefers_the_plan_context_spread():
    result = SimpleNamespace(
        context=SimpleNamespace(spread_pct=Decimal("-0.0002")),
        plan=SimpleNamespace(diagnostics={"cost": {"rates": {"spread_bps": "9"}}}),
    )
    assert spread_bps_for_order_policy(result) == Decimal("2.0000")

    result.context.spread_pct = None
    assert spread_bps_for_order_policy(result) == Decimal("9")
    assert spread_bps_for_order_policy(None) is None


def test_intelligent_layer_failure_is_journaled_and_the_trade_continues():
    class _BrokenBuilder:
        def build_shadow_plan(self, **_kwargs):
            raise RuntimeError("builder down")

    journal = FakeJournal()
    flow = IntelligentFlowService(
        config=SimpleNamespace(
            architecture_mode="INTELLIGENT_FLOW",
            intelligent_activation_level="SHADOW",
            dynamic_sizing_enabled=False,
            dynamic_exit_enabled=False,
        ),
        journal=journal,
        components=IntelligentFlowComponents(order_builder=_BrokenBuilder()),
    )

    assert (
        flow.record_order_shadow(
            symbol_config=SYMBOL,
            action="OPEN_LONG",
            trade_pipeline_result=None,
            planned_quantity=None,
            planned_exit=None,
        )
        is None
    )
    assert journal.events == [
        (
            "intelligent_order_plan_shadow_failed",
            {"symbol": "BTCUSDT", "action": "OPEN_LONG", "reason": "builder down"},
        )
    ]
    assert (
        flow.record_exit_shadow(
            symbol_config=SYMBOL, action="OPEN_LONG", trade_pipeline_result=None, planned_exit=None
        )
        is None
    )


def test_adaptive_plan_is_skipped_when_the_engine_cannot_apply_it():
    journal = FakeJournal()
    service = AdaptiveManagementService(
        config=SimpleNamespace(),
        journal=journal,
        manager=None,
        planner=None,
        activation_policy=None,
        execution_engine=object(),
    )

    service.record_execution_result(
        symbol_config=SYMBOL,
        adjustment_plan=SimpleNamespace(plan_id="adj-1", execution_enabled=False),
    )

    event_type, payload = journal.events[0]
    assert event_type == "adaptive_adjustment_execution_result"
    assert payload["status"] == "SKIPPED"
    assert payload["reason"] == "EXECUTION_ENGINE_NOT_SUPPORTED"
