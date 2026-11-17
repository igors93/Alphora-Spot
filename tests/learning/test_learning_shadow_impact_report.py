import json
from decimal import Decimal

from trading.learning_shadow_impact_report import (
    LEARNING_SHADOW_IMPACT_REPORT_EVENT_TYPE,
    LearningShadowImpactReporter,
)
from utils.trade_journal import TradeJournal


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


def _write_shadow(path, *, plan_id="tp_test"):
    _write_event(
        path,
        "learning_execution_shadow_evaluated",
        {
            "symbol": "BTCUSDT",
            "action": "OPEN_LONG",
            "plan_id": plan_id,
            "decision_id": f"lex_{plan_id}",
            "status": "WOULD_BLOCK",
            "suggested_action": "BLOCK_EXECUTION_SHADOW",
            "original_quantity": "0.1",
            "shadow_quantity": "0",
            "suggested_size_multiplier": "0",
            "would_block_trade": True,
            "would_reduce_size": False,
            "can_use_for_execution": False,
            "shadow_only": True,
        },
    )


def _write_outcome(path, *, plan_id="tp_test", net_pnl="-1.00"):
    _write_event(
        path,
        "trade_plan_outcome_recorded",
        {
            "symbol": "BTCUSDT",
            "trade_plan_id": plan_id,
            "outcome_id": f"out_{plan_id}",
            "outcome_status": "LOSS",
            "net_pnl": net_pnl,
            "source": "PAPER",
        },
    )


def test_learning_shadow_impact_reporter_records_summary(tmp_path):
    source_journal = tmp_path / "governor_events.jsonl"
    output_journal = tmp_path / "impact_events.jsonl"

    _write_shadow(source_journal)
    _write_outcome(source_journal)

    reporter = LearningShadowImpactReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
    )

    summary = reporter.record_if_changed(
        previous_total_shadow_decisions=0,
        previous_matched_outcomes=0,
    )

    assert summary is not None
    assert summary.total_shadow_decisions == 1
    assert summary.matched_outcomes == 1
    assert summary.estimated_loss_avoided_by_blocks == Decimal("1.00")

    events = [json.loads(line) for line in output_journal.read_text(encoding="utf-8").splitlines()]

    assert len(events) == 1
    assert events[0]["event_type"] == LEARNING_SHADOW_IMPACT_REPORT_EVENT_TYPE
    assert events[0]["data"]["total_shadow_decisions"] == 1
    assert events[0]["data"]["matched_outcomes"] == 1
    assert events[0]["data"]["confidence_status"] == "INSUFFICIENT_DATA"
    assert events[0]["data"]["confidence_grade"] == "LOW"
    assert events[0]["data"]["activation_status"] == "OBSERVE_MORE"
    assert events[0]["data"]["paper_activation_status"] == "STORE_DISABLED"
    assert events[0]["data"]["paper_activation_enabled"] is False
    assert events[0]["data"]["can_apply_reduction_in_paper"] is False
    assert events[0]["data"]["can_apply_block_in_paper"] is False
    assert events[0]["data"]["eligible_for_paper_reduction"] is False
    assert events[0]["data"]["eligible_for_paper_block"] is False
    assert events[0]["data"]["can_activate_testnet"] is False
    assert events[0]["data"]["can_activate_real"] is False
    assert events[0]["data"]["can_use_for_execution"] is False
    assert events[0]["data"]["shadow_only"] is True


def test_learning_shadow_impact_reporter_does_not_record_when_unchanged(tmp_path):
    source_journal = tmp_path / "governor_events.jsonl"
    output_journal = tmp_path / "impact_events.jsonl"

    _write_shadow(source_journal)
    _write_outcome(source_journal)

    reporter = LearningShadowImpactReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
    )

    summary = reporter.record_if_changed(
        previous_total_shadow_decisions=1,
        previous_matched_outcomes=1,
    )

    assert summary is None
    assert not output_journal.exists()


def test_learning_shadow_impact_reporter_skips_heavy_summary_when_unchanged(tmp_path):
    class RaisingAnalyzer:
        def analyze_journal(self, _path):
            raise AssertionError("heavy shadow impact summary should not be generated")

    source_journal = tmp_path / "governor_events.jsonl"
    output_journal = tmp_path / "impact_events.jsonl"

    _write_shadow(source_journal)
    _write_outcome(source_journal)

    reporter = LearningShadowImpactReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
        analyzer=RaisingAnalyzer(),
    )

    summary = reporter.record_if_changed(
        previous_total_shadow_decisions=1,
        previous_matched_outcomes=1,
    )

    assert summary is None
    assert not output_journal.exists()


def test_learning_shadow_impact_reporter_does_not_record_empty_journal(tmp_path):
    source_journal = tmp_path / "missing.jsonl"
    output_journal = tmp_path / "impact_events.jsonl"

    reporter = LearningShadowImpactReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
    )

    summary = reporter.record_if_changed(
        previous_total_shadow_decisions=0,
        previous_matched_outcomes=0,
    )

    assert summary is None
    assert not output_journal.exists()
