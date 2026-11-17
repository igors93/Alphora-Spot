"""Compare recorded Governor decisions and fake-exchange effects."""

import json
from pathlib import Path

import pytest

from tests.golden.harness import run_scenario

SCENARIOS = (
    "no_signal",
    "entry_approved",
    "economic_gate_block",
    "portfolio_risk_block",
    "defensive_mode_block",
    "warmup",
    "close_with_protection",
    "unknown_entry_result",
    "dynamic_plan_entry",
    "learning_shadow_entry",
    "paper_entry",
    "dry_run_entry",
    "short_entry",
    "adaptive_open_position",
)
ROOT = Path(__file__).parent
STAGE_CONTRACTS = {
    "no_signal": (7, "LIGHT_ONLY"),
    "entry_approved": (22, "NON_TERMINAL"),
    "economic_gate_block": (15, "ECONOMIC_GATE"),
    "portfolio_risk_block": (18, "PORTFOLIO_GOVERNOR"),
    "defensive_mode_block": (6, "PRE_HEAVY_DEFENSIVE_MODE"),
    "warmup": (3, "WARMUP"),
    "close_with_protection": (22, "NON_TERMINAL"),
    "unknown_entry_result": (22, "NON_TERMINAL"),
    "dynamic_plan_entry": (22, "NON_TERMINAL"),
    "learning_shadow_entry": (22, "NON_TERMINAL"),
    "paper_entry": (22, "NON_TERMINAL"),
    "dry_run_entry": (22, "NON_TERMINAL"),
    "short_entry": (22, "NON_TERMINAL"),
    "adaptive_open_position": (15, "ECONOMIC_GATE"),
}


def _assert_scenario_contract(scenario, actual):
    trace_length, terminal_stage = STAGE_CONTRACTS[scenario]
    assert len(actual["stages"]) == 1
    assert len(actual["stages"][0]["trace"]) == trace_length
    assert actual["stages"][0]["terminal_stage"] == terminal_stage
    mutations = actual["mutations"]
    if scenario in {
        "no_signal",
        "economic_gate_block",
        "portfolio_risk_block",
        "defensive_mode_block",
        "warmup",
        "paper_entry",
        "dry_run_entry",
        "adaptive_open_position",
    }:
        assert all(not calls for calls in mutations.values())
    if scenario in {"entry_approved", "unknown_entry_result", "learning_shadow_entry"}:
        assert len(mutations["new_order"]) == 1
        assert mutations["new_order"][0]["quantity"] == "0.5"
        assert [item["triggerPrice"] for item in mutations["new_algo_order"]] == [
            "99",
            "102",
        ]
        assert actual["exchange_state"]["direction"] == "LONG"
        assert actual["exchange_state"]["quantity"] == "0.5"
        assert len(actual["exchange_state"]["open_algo_orders"]) == 2
    if scenario == "close_with_protection":
        assert len(mutations["new_order"]) == 1
        assert mutations["new_order"][0]["reduceOnly"] == "true"
        assert mutations["new_order"][0]["quantity"] == "0.5"
        assert len(mutations["cancel_algo_order"]) == 2
        assert actual["exchange_state"]["direction"] == "NONE"
        assert actual["exchange_state"]["quantity"] == "0"
        assert actual["exchange_state"]["open_algo_orders"] == []
    if scenario == "unknown_entry_result":
        assert len(actual["queries"]["query_order"]) == 1
        assert (
            actual["queries"]["query_order"][0]["orig_client_order_id"]
            == mutations["new_order"][0]["newClientOrderId"]
        )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_golden_replay(scenario, config_factory, request):
    directory = ROOT / scenario
    actual = run_scenario(directory, config_factory)
    _assert_scenario_contract(scenario, actual)
    expected_path = directory / "expected.json"
    if request.config.getoption("--update-golden"):
        expected_path.write_text(
            json.dumps(actual, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    else:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        assert actual == expected
