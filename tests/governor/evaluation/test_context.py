"""G2: the evaluation context is typed and its phases can be reset explicitly."""

from __future__ import annotations

import ast
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from governor.symbol_evaluation_pipeline import SymbolEvaluationContext, SymbolEvaluationInput

PIPELINE_SOURCE = (
    Path(__file__).resolve().parents[3] / "src" / "governor" / "symbol_evaluation_pipeline.py"
)

# Fields produced before stage 15; the pre-dispatch replan must keep them.
_KEPT_BY_REPLAN = {
    "evaluation_input",
    "cycle_context",
    "light",
    "position",
    "runtime_state",
    "score",
    "trend_context",
    "scheduled",
    "learning_advisory",
    "status",
    "warmup",
    "operational_state",
    "protection_warning",
    "ai_audit",
    "budget_decision",
    "router_heavy",
    "heavy",
    "trade_decision",
    "hot_result",
    "intent",
    "state_reconciliation_decision",
    "light_evaluation",
    "heavy_evaluation",
    "intent_candidate",
    "diagnostics",
    "terminal_result",
    "lifecycle_started_at",
    "lifecycle_finished",
    # Kept on purpose: identifiers and prices of the superseded plan stay visible
    # to the replan (trade_plan_id is overwritten by stage 15).
    "trade_plan_id",
    "account_snapshot",
    "pre_dispatch_price_decision",
    # Stage 22 builds the command after the replan, so the old value is replaced.
    "execution_command",
}


def test_every_context_field_documents_its_writer_stage() -> None:
    tree = ast.parse(PIPELINE_SOURCE.read_text(encoding="utf-8"))
    lines = PIPELINE_SOURCE.read_text(encoding="utf-8").splitlines()
    context = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SymbolEvaluationContext"
    )
    bookkeeping = {
        "evaluation_input",
        "diagnostics",
        "terminal_result",
        "lifecycle_started_at",
        "lifecycle_finished",
    }
    undocumented = [
        item.target.id
        for item in context.body
        if isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and item.target.id not in bookkeeping
        and not lines[item.lineno - 2].strip().startswith("#")
    ]
    assert undocumented == []


def test_reset_plan_for_replan_clears_only_the_plan_and_execution_phases() -> None:
    evaluation = SymbolEvaluationContext(
        evaluation_input=SymbolEvaluationInput(
            symbol_config=SimpleNamespace(symbol="BTCUSDT"),
            now=1.0,
            cycle_id="c",
            symbol_eval_id="e",
        ),
        warmup=True,
        operational_state="IN_POSITION",
        trade_plan_id="plan-1",
    )
    sentinel = object()
    for item in fields(SymbolEvaluationContext):
        if item.name in _KEPT_BY_REPLAN:
            continue
        if item.type in {"bool"}:
            setattr(evaluation, item.name, True)
        elif item.type in {"str"}:
            setattr(evaluation, item.name, "set")
        elif item.name == "planned_quantity":
            evaluation.planned_quantity = Decimal("1")
        else:
            setattr(evaluation, item.name, sentinel)

    evaluation.reset_plan_for_replan()

    for item in fields(SymbolEvaluationContext):
        if item.name in _KEPT_BY_REPLAN:
            continue
        assert getattr(evaluation, item.name) in (None, False, ""), item.name
    assert evaluation.warmup is True
    assert evaluation.operational_state == "IN_POSITION"
    assert evaluation.trade_plan_id == "plan-1"
