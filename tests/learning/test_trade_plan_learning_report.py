import json

from trading.learning_recommendations import REPORT_STATUS_INSUFFICIENT_DATA
from trading.learning_report import (
    LEARNING_REPORT_EVENT_TYPE,
    TradePlanLearningReporter,
    export_latest_learning_report,
    format_learning_report,
    load_latest_learning_report,
    load_learning_reports_from_journal,
)
from utils.trade_journal import TradeJournal


def _write_outcome(path, *, trade_plan_id="tp_test", outcome_status="WIN", net_pnl="1"):
    with path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "event_type": "trade_plan_outcome_recorded",
                    "data": {
                        "outcome": {
                            "symbol": "BTCUSDT",
                            "trade_plan_id": trade_plan_id,
                            "outcome_status": outcome_status,
                            "net_pnl": net_pnl,
                            "adaptive_adjustments_applied": 0,
                        }
                    },
                },
                ensure_ascii=False,
            )
        )
        file.write("\n")


def test_learning_reporter_records_recommendation_report(tmp_path):
    source_journal = tmp_path / "trades.jsonl"
    output_journal = tmp_path / "governor_events.jsonl"
    _write_outcome(source_journal)

    reporter = TradePlanLearningReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
    )

    report = reporter.record_if_changed(previous_total_outcomes=0)

    assert report is not None
    assert report.total_outcomes == 1
    assert report.status == REPORT_STATUS_INSUFFICIENT_DATA

    events = [json.loads(line) for line in output_journal.read_text(encoding="utf-8").splitlines()]

    assert len(events) == 1
    assert events[0]["event_type"] == LEARNING_REPORT_EVENT_TYPE
    assert events[0]["data"]["total_outcomes"] == 1
    assert events[0]["data"]["can_use_for_execution"] is False


def test_learning_reporter_does_not_record_when_outcome_count_did_not_change(tmp_path):
    source_journal = tmp_path / "trades.jsonl"
    output_journal = tmp_path / "governor_events.jsonl"
    _write_outcome(source_journal)

    reporter = TradePlanLearningReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
    )

    report = reporter.record_if_changed(previous_total_outcomes=1)

    assert report is None
    assert not output_journal.exists()


def test_learning_reporter_skips_heavy_report_when_outcome_count_did_not_change(tmp_path):
    class RaisingAnalyzer:
        def summarize_journal(self, _path):
            raise AssertionError("heavy learning report should not be generated")

    source_journal = tmp_path / "trades.jsonl"
    output_journal = tmp_path / "governor_events.jsonl"
    _write_outcome(source_journal)

    reporter = TradePlanLearningReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
        analyzer=RaisingAnalyzer(),
    )

    report = reporter.record_if_changed(previous_total_outcomes=1)

    assert report is None
    assert not output_journal.exists()


def test_learning_reporter_does_not_record_empty_journal(tmp_path):
    source_journal = tmp_path / "missing.jsonl"
    output_journal = tmp_path / "governor_events.jsonl"

    reporter = TradePlanLearningReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
    )

    report = reporter.record_if_changed(previous_total_outcomes=0)

    assert report is None
    assert not output_journal.exists()


def test_learning_report_loader_reads_latest_report(tmp_path):
    source_journal = tmp_path / "trades.jsonl"
    output_journal = tmp_path / "governor_events.jsonl"
    _write_outcome(source_journal, trade_plan_id="tp_first", net_pnl="1")
    reporter = TradePlanLearningReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
    )
    reporter.record_if_changed(previous_total_outcomes=0)

    _write_outcome(source_journal, trade_plan_id="tp_second", net_pnl="-1")
    reporter.record_if_changed(previous_total_outcomes=1)

    reports = load_learning_reports_from_journal(output_journal)
    latest = load_latest_learning_report(output_journal)

    assert len(reports) == 2
    assert latest is not None
    assert latest["total_outcomes"] == 2


def test_learning_report_formatter_generates_readable_text(tmp_path):
    source_journal = tmp_path / "trades.jsonl"
    output_journal = tmp_path / "governor_events.jsonl"
    _write_outcome(source_journal)

    reporter = TradePlanLearningReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
    )
    reporter.record_if_changed(previous_total_outcomes=0)

    latest = load_latest_learning_report(output_journal)
    assert latest is not None

    text = format_learning_report(latest)

    assert "Alphora Learning Report" in text
    assert "Total outcomes: 1" in text
    assert "Recomendações:" in text
    assert "COLLECT_MORE_DATA" in text


def test_learning_report_export_writes_text_file(tmp_path):
    source_journal = tmp_path / "trades.jsonl"
    output_journal = tmp_path / "governor_events.jsonl"
    report_path = tmp_path / "reports" / "learning_report.txt"
    _write_outcome(source_journal)

    reporter = TradePlanLearningReporter(
        source_journal_path=source_journal,
        output_journal=TradeJournal(str(output_journal)),
    )
    reporter.record_if_changed(previous_total_outcomes=0)

    exported = export_latest_learning_report(
        source_journal_path=output_journal,
        output_path=report_path,
    )

    assert exported is not None
    assert report_path.exists()
    assert "Alphora Learning Report" in report_path.read_text(encoding="utf-8")
