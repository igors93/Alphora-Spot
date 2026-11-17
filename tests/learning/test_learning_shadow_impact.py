import json
from decimal import Decimal

from trading.learning_shadow_impact import (
    IMPACT_BLOCK_WOULD_HELP,
    IMPACT_BLOCK_WOULD_HURT,
    IMPACT_REDUCE_SIZE_WOULD_HELP,
    LearningShadowImpactAnalyzer,
    export_learning_shadow_impact_summary,
    format_learning_shadow_impact_summary,
)


def _write_event(path, event_type, data):
    with path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "event_type": event_type,
                    "data": data,
                },
                ensure_ascii=False,
            )
        )
        file.write("\n")


def _write_shadow(
    path,
    *,
    plan_id,
    status,
    net_action,
    original_quantity="0.1",
    shadow_quantity="0",
    would_block_trade=False,
    would_reduce_size=False,
):
    _write_event(
        path,
        "learning_execution_shadow_evaluated",
        {
            "symbol": "BTCUSDT",
            "action": "OPEN_LONG",
            "plan_id": plan_id,
            "decision_id": f"lex_{plan_id}",
            "status": status,
            "suggested_action": net_action,
            "original_quantity": original_quantity,
            "shadow_quantity": shadow_quantity,
            "suggested_size_multiplier": (
                "0.5" if would_reduce_size else "0" if would_block_trade else "1"
            ),
            "would_block_trade": would_block_trade,
            "would_reduce_size": would_reduce_size,
            "can_use_for_execution": False,
            "shadow_only": True,
        },
    )


def _write_outcome(path, *, plan_id, outcome_status, net_pnl):
    _write_event(
        path,
        "trade_plan_outcome_recorded",
        {
            "symbol": "BTCUSDT",
            "trade_plan_id": plan_id,
            "outcome_id": f"out_{plan_id}",
            "outcome_status": outcome_status,
            "net_pnl": net_pnl,
            "source": "PAPER",
        },
    )


def test_learning_shadow_impact_matches_shadow_decisions_to_outcomes(tmp_path):
    journal = tmp_path / "governor_events.jsonl"

    _write_shadow(
        journal,
        plan_id="tp_block_loss",
        status="WOULD_BLOCK",
        net_action="BLOCK_EXECUTION_SHADOW",
        would_block_trade=True,
    )
    _write_outcome(
        journal,
        plan_id="tp_block_loss",
        outcome_status="LOSS",
        net_pnl="-2.00",
    )

    _write_shadow(
        journal,
        plan_id="tp_block_win",
        status="WOULD_BLOCK",
        net_action="BLOCK_EXECUTION_SHADOW",
        would_block_trade=True,
    )
    _write_outcome(
        journal,
        plan_id="tp_block_win",
        outcome_status="WIN",
        net_pnl="1.50",
    )

    _write_shadow(
        journal,
        plan_id="tp_reduce_loss",
        status="WOULD_REDUCE_SIZE",
        net_action="REDUCE_SIZE_SHADOW",
        original_quantity="0.2",
        shadow_quantity="0.1",
        would_reduce_size=True,
    )
    _write_outcome(
        journal,
        plan_id="tp_reduce_loss",
        outcome_status="LOSS",
        net_pnl="-2.00",
    )

    summary = LearningShadowImpactAnalyzer().analyze_journal(journal)

    assert summary.total_shadow_decisions == 3
    assert summary.matched_outcomes == 3
    assert summary.unmatched_shadow_decisions == 0
    assert summary.would_block_count == 2
    assert summary.would_reduce_size_count == 1
    assert summary.block_would_help_count == 1
    assert summary.block_would_hurt_count == 1
    assert summary.reduce_size_would_help_count == 1
    assert summary.estimated_loss_avoided_by_blocks == Decimal("2.00")
    assert summary.estimated_profit_missed_by_blocks == Decimal("1.50")
    assert summary.estimated_block_net_impact == Decimal("0.50")
    assert summary.estimated_reduce_size_net_impact == Decimal("1.000")
    assert summary.estimated_total_shadow_impact == Decimal("1.500")


def test_learning_shadow_impact_marks_trade_impact_status(tmp_path):
    journal = tmp_path / "governor_events.jsonl"

    _write_shadow(
        journal,
        plan_id="tp_block_loss",
        status="WOULD_BLOCK",
        net_action="BLOCK_EXECUTION_SHADOW",
        would_block_trade=True,
    )
    _write_outcome(
        journal,
        plan_id="tp_block_loss",
        outcome_status="LOSS",
        net_pnl="-1",
    )

    _write_shadow(
        journal,
        plan_id="tp_block_win",
        status="WOULD_BLOCK",
        net_action="BLOCK_EXECUTION_SHADOW",
        would_block_trade=True,
    )
    _write_outcome(
        journal,
        plan_id="tp_block_win",
        outcome_status="WIN",
        net_pnl="1",
    )

    _write_shadow(
        journal,
        plan_id="tp_reduce_loss",
        status="WOULD_REDUCE_SIZE",
        net_action="REDUCE_SIZE_SHADOW",
        original_quantity="0.2",
        shadow_quantity="0.1",
        would_reduce_size=True,
    )
    _write_outcome(
        journal,
        plan_id="tp_reduce_loss",
        outcome_status="LOSS",
        net_pnl="-1",
    )

    summary = LearningShadowImpactAnalyzer().analyze_journal(journal)
    impacts = {trade.plan_id: trade.impact_status for trade in summary.matched_trades}

    assert impacts["tp_block_loss"] == IMPACT_BLOCK_WOULD_HELP
    assert impacts["tp_block_win"] == IMPACT_BLOCK_WOULD_HURT
    assert impacts["tp_reduce_loss"] == IMPACT_REDUCE_SIZE_WOULD_HELP


def test_learning_shadow_impact_counts_unmatched_shadow_decisions(tmp_path):
    journal = tmp_path / "governor_events.jsonl"

    _write_shadow(
        journal,
        plan_id="tp_without_outcome",
        status="WOULD_BLOCK",
        net_action="BLOCK_EXECUTION_SHADOW",
        would_block_trade=True,
    )

    summary = LearningShadowImpactAnalyzer().analyze_journal(journal)

    assert summary.total_shadow_decisions == 1
    assert summary.matched_outcomes == 0
    assert summary.unmatched_shadow_decisions == 1


def test_learning_shadow_impact_formats_and_exports_report(tmp_path):
    journal = tmp_path / "governor_events.jsonl"
    output = tmp_path / "reports" / "impact.txt"

    _write_shadow(
        journal,
        plan_id="tp_block_loss",
        status="WOULD_BLOCK",
        net_action="BLOCK_EXECUTION_SHADOW",
        would_block_trade=True,
    )
    _write_outcome(
        journal,
        plan_id="tp_block_loss",
        outcome_status="LOSS",
        net_pnl="-2",
    )

    summary = LearningShadowImpactAnalyzer().analyze_journal(journal)
    text = format_learning_shadow_impact_summary(summary)
    exported = export_learning_shadow_impact_summary(
        source_journal_path=journal,
        output_path=output,
    )

    assert "Alphora Learning Shadow Impact" in text
    assert "Total shadow decisions: 1" in text
    assert output.exists()
    assert "Loss avoided by blocks: 2" in exported
