import json

from trading.learning_cli import main
from trading.learning_report import TradePlanLearningReporter
from utils.trade_journal import TradeJournal


def _write_outcome(path):
    with path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "event_type": "trade_plan_outcome_recorded",
                    "data": {
                        "outcome": {
                            "symbol": "BTCUSDT",
                            "trade_plan_id": "tp_test",
                            "outcome_status": "WIN",
                            "net_pnl": "1",
                            "adaptive_adjustments_applied": 0,
                        }
                    },
                },
                ensure_ascii=False,
            )
        )
        file.write("\n")


def _write_shadow(path):
    with path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "event_type": "learning_execution_shadow_evaluated",
                    "data": {
                        "symbol": "BTCUSDT",
                        "action": "OPEN_LONG",
                        "plan_id": "tp_test",
                        "decision_id": "lex_test",
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
                },
                ensure_ascii=False,
            )
        )
        file.write("\n")


def test_learning_cli_prints_latest_report(tmp_path, capsys):
    trades_journal = tmp_path / "trades.jsonl"
    events_journal = tmp_path / "governor_events.jsonl"
    _write_outcome(trades_journal)

    reporter = TradePlanLearningReporter(
        source_journal_path=trades_journal,
        output_journal=TradeJournal(str(events_journal)),
    )
    reporter.record_if_changed(previous_total_outcomes=0)

    exit_code = main(["--events", str(events_journal)])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Alphora Learning Report" in captured.out
    assert "COLLECT_MORE_DATA" in captured.out


def test_learning_cli_exports_latest_report(tmp_path, capsys):
    trades_journal = tmp_path / "trades.jsonl"
    events_journal = tmp_path / "governor_events.jsonl"
    output_path = tmp_path / "reports" / "learning_report.txt"
    _write_outcome(trades_journal)

    reporter = TradePlanLearningReporter(
        source_journal_path=trades_journal,
        output_journal=TradeJournal(str(events_journal)),
    )
    reporter.record_if_changed(previous_total_outcomes=0)

    exit_code = main(
        [
            "--events",
            str(events_journal),
            "--output",
            str(output_path),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert output_path.exists()
    assert "Relatório salvo em:" in captured.out
    assert "Alphora Learning Report" in output_path.read_text(encoding="utf-8")


def test_learning_cli_handles_missing_report(tmp_path, capsys):
    events_journal = tmp_path / "missing.jsonl"

    exit_code = main(["--events", str(events_journal)])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Nenhum relatório de aprendizado encontrado." in captured.out


def test_learning_cli_prints_shadow_impact_report(tmp_path, capsys):
    events_journal = tmp_path / "governor_events.jsonl"
    _write_shadow(events_journal)
    _write_outcome(events_journal)

    exit_code = main(["--events", str(events_journal), "--impact"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Alphora Learning Shadow Impact" in captured.out
    assert "Total shadow decisions: 1" in captured.out
    assert "Learning confidence:" in captured.out
    assert "Learning activation criteria:" in captured.out
    assert "Learning paper activation flags:" in captured.out
    assert "Can use for execution: False" in captured.out


def test_learning_cli_exports_shadow_impact_report(tmp_path, capsys):
    events_journal = tmp_path / "governor_events.jsonl"
    output_path = tmp_path / "reports" / "learning_shadow_impact.txt"
    _write_shadow(events_journal)
    _write_outcome(events_journal)

    exit_code = main(
        [
            "--events",
            str(events_journal),
            "--impact",
            "--output",
            str(output_path),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert output_path.exists()
    assert "Relatório de impacto salvo em:" in captured.out

    exported = output_path.read_text(encoding="utf-8")
    assert "Alphora Learning Shadow Impact" in exported
    assert "Learning confidence:" in exported
    assert "Learning activation criteria:" in exported
    assert "Learning paper activation flags:" in exported
