import json
from decimal import Decimal

from trading.learning import (
    OUTCOME_EVENT_TYPE,
    TradePlanLearningAnalyzer,
    load_trade_plan_outcomes_from_journal,
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


def test_learning_loader_reads_trade_plan_outcome_events(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_event(
        journal,
        OUTCOME_EVENT_TYPE,
        {
            "outcome": {
                "symbol": "BTCUSDT",
                "trade_plan_id": "tp_win",
                "outcome_status": "WIN",
                "net_pnl": "1.25",
                "adaptive_adjustments_applied": 1,
            }
        },
    )
    _write_event(
        journal,
        "other_event",
        {"trade_plan_id": "ignored"},
    )

    outcomes = load_trade_plan_outcomes_from_journal(journal)

    assert len(outcomes) == 1
    assert outcomes[0]["trade_plan_id"] == "tp_win"
    assert outcomes[0]["net_pnl"] == "1.25"


def test_learning_summary_calculates_core_metrics(tmp_path):
    journal = tmp_path / "journal.jsonl"
    _write_event(
        journal,
        OUTCOME_EVENT_TYPE,
        {
            "outcome": {
                "symbol": "BTCUSDT",
                "trade_plan_id": "tp_win",
                "outcome_status": "WIN",
                "net_pnl": "2.00",
                "adaptive_adjustments_applied": 1,
            }
        },
    )
    _write_event(
        journal,
        OUTCOME_EVENT_TYPE,
        {
            "outcome": {
                "symbol": "BTCUSDT",
                "trade_plan_id": "tp_loss",
                "outcome_status": "LOSS",
                "net_pnl": "-0.50",
                "adaptive_adjustments_applied": 0,
            }
        },
    )
    _write_event(
        journal,
        OUTCOME_EVENT_TYPE,
        {
            "outcome": {
                "symbol": "ETHUSDT",
                "trade_plan_id": None,
                "outcome_status": "BREAKEVEN",
                "net_pnl": "0",
                "adaptive_adjustments_applied": 0,
            }
        },
    )

    summary = TradePlanLearningAnalyzer().summarize_journal(journal)

    assert summary.total_outcomes == 3
    assert summary.linked_outcomes == 2
    assert summary.unlinked_outcomes == 1
    assert summary.wins == 1
    assert summary.losses == 1
    assert summary.breakeven == 1
    assert summary.total_net_pnl == Decimal("1.50")
    assert summary.average_net_pnl == Decimal("0.50")
    assert summary.win_rate == Decimal("0.3333333333333333333333333333")
    assert summary.adjusted.count == 1
    assert summary.adjusted.total_net_pnl == Decimal("2.00")
    assert summary.not_adjusted.count == 2
    assert summary.not_adjusted.total_net_pnl == Decimal("-0.50")
    assert summary.by_symbol["BTCUSDT"].count == 2
    assert summary.by_symbol["ETHUSDT"].count == 1


def test_learning_summary_handles_missing_journal(tmp_path):
    missing = tmp_path / "missing.jsonl"

    summary = TradePlanLearningAnalyzer().summarize_journal(missing)

    assert summary.total_outcomes == 0
    assert summary.total_net_pnl == Decimal("0")
    assert summary.win_rate == Decimal("0")
    assert summary.diagnostics["has_outcomes"] is False
