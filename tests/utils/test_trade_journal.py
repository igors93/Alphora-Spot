"""Testes de sanitização do TradeJournal."""

import json
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from domain.models import TradeAction
from governor.event_sink import EventSink
from governor.execution_engine import ExecutionCommand, ExecutionResult, ExecutionStatus
from governor.hot_symbol_worker import GuardDecision, GuardOutcome, OperationalDirective
from scanner.signal_score import SignalPriority
from trading.intent import TradeIntent
from utils.audit_logger import get_run_id, reset_for_testing
from utils.events import EventChannel
from utils.trade_journal import TradeJournal, journal_event_record


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_trade_journal_masks_api_secret(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append("order_placed", api_secret="SENSITIVE_TEST_API_SECRET_456")

    text = path.read_text(encoding="utf-8")
    assert "SENSITIVE_TEST_API_SECRET_456" not in text
    events = _read_jsonl(path)
    assert len(events) == 1
    assert json.dumps(events[0])  # JSONL válido


def test_trade_journal_masks_listen_key(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append("ws_connected", listen_key="SENSITIVE_TEST_LISTEN_KEY_ABC")

    text = path.read_text(encoding="utf-8")
    assert "SENSITIVE_TEST_LISTEN_KEY_ABC" not in text


def test_trade_journal_masks_signature(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append("request_sent", signature="SENSITIVE_TEST_SIGNATURE_DEF")

    text = path.read_text(encoding="utf-8")
    assert "SENSITIVE_TEST_SIGNATURE_DEF" not in text


def test_trade_journal_masks_api_key(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append("auth", api_key="SENSITIVE_TEST_API_KEY_123")

    text = path.read_text(encoding="utf-8")
    assert "SENSITIVE_TEST_API_KEY_123" not in text


def test_trade_journal_masks_nested_sensitive_field(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append("config_loaded", credentials={"api_secret": "nested-secret-xyz"})

    text = path.read_text(encoding="utf-8")
    assert "nested-secret-xyz" not in text


def test_trade_journal_preserves_non_sensitive_fields(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append("trade_executed", symbol="BTCUSDT", side="BUY", price=Decimal("50000.0"))

    events = _read_jsonl(path)
    assert len(events) == 1
    assert events[0]["data"]["symbol"] == "BTCUSDT"
    assert events[0]["data"]["side"] == "BUY"
    assert events[0]["data"]["price"] == "50000.0"


def test_trade_journal_preserves_event_type(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append("my_event", x=1)

    events = _read_jsonl(path)
    assert events[0]["event_type"] == "my_event"


def test_trade_journal_jsonl_remains_valid_after_masking(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append("order_placed", token="tok-secret", symbol="ETHUSDT", amount=Decimal("1.5"))
    journal.append("order_placed", token="tok-secret-2", symbol="BTCUSDT", amount=Decimal("0.1"))

    events = _read_jsonl(path)
    assert len(events) == 2
    for event in events:
        json.dumps(event)  # deve ser serializável


def test_trade_journal_list_of_objects_sanitized(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append(
        "batch_order",
        orders=[
            {"api_secret": "sec-1", "symbol": "BTCUSDT"},
            {"api_secret": "sec-2", "symbol": "ETHUSDT"},
        ],
    )

    text = path.read_text(encoding="utf-8")
    assert "sec-1" not in text
    assert "sec-2" not in text
    events = _read_jsonl(path)
    assert events[0]["data"]["orders"][0]["symbol"] == "BTCUSDT"
    assert events[0]["data"]["orders"][1]["symbol"] == "ETHUSDT"


def test_trade_journal_each_event_on_own_line(tmp_path):
    path = tmp_path / "trades.jsonl"
    journal = TradeJournal(str(path))
    journal.append("e1", x=1)
    journal.append("e2", x=2)

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # cada linha deve ser JSON válido independente


def test_trade_journal_serializes_immutable_execution_contracts(tmp_path):
    path = tmp_path / "contracts.jsonl"
    journal = TradeJournal(str(path))
    intent = TradeIntent(
        symbol="BTCUSDT",
        action="OPEN_LONG",
        confidence=90,
        reason="contract",
        strategy_name="test_strategy",
        priority=SignalPriority.READY,
        market_price=Decimal("100"),
        metadata={
            "nested": MappingProxyType(
                {
                    "levels": (Decimal("1.1"), Decimal("1.2")),
                    "tags": frozenset({"alpha", "beta"}),
                }
            )
        },
    )
    command = ExecutionCommand.from_intent(
        intent=intent,
        heavy_snapshot=type(
            "Heavy",
            (),
            {"market": type("Market", (), {"price": Decimal("100")})()},
        )(),
        planned_quantity=Decimal("0.2"),
        trade_plan_id="tp-json",
    )
    result = ExecutionResult(
        command_id=command.command_id,
        status=ExecutionStatus.DRY_RUN,
        symbol="BTCUSDT",
        action=TradeAction.OPEN_LONG,
        order_ids=("order-1",),
        position_changed=False,
        reason="DRY_RUN_OPEN_NOT_SENT",
        diagnostics=MappingProxyType({"tuple": (Decimal("1"),), "frozen": frozenset({"x"})}),
    )

    journal.append("contracts", intent=intent, command=command, result=result)

    text = path.read_text(encoding="utf-8")
    assert "[UNSUPPORTED:mappingproxy]" not in text
    event = _read_jsonl(path)[0]
    data = event["data"]
    assert data["intent"]["metadata"]["nested"]["levels"] == ["1.1", "1.2"]
    assert sorted(data["intent"]["metadata"]["nested"]["tags"]) == ["alpha", "beta"]
    assert data["command"]["metadata"]["strategy_name"] == "test_strategy"
    assert data["result"]["status"] == "DRY_RUN"
    assert data["result"]["diagnostics"]["tuple"] == ["1"]


def test_event_sink_serializes_guard_decision_and_nested_mappings(tmp_path):
    path = tmp_path / "events.jsonl"
    journal = TradeJournal(str(path))
    sink = EventSink(destinations=(journal,))
    decision = GuardDecision(
        outcome=GuardOutcome.FORCE_CLOSE,
        reason="RISK_GUARD_CLOSE_MAX_HOLD",
        checks=MappingProxyType(
            {
                "nested": MappingProxyType({"values": (Decimal("2.5"),)}),
                "tags": frozenset({"risk", "hold"}),
            }
        ),
        directive=OperationalDirective(
            action=TradeAction.CLOSE_POSITION,
            reason="RISK_GUARD_CLOSE_MAX_HOLD",
            source="test",
            parent_intent_id="intent-1",
        ),
    )

    write_result = sink.emit("guard_decision", decision=decision)

    assert write_result.success is True
    text = path.read_text(encoding="utf-8")
    assert "[UNSUPPORTED:mappingproxy]" not in text
    event = _read_jsonl(path)[0]
    decision_payload = event["data"]["decision"]
    assert decision_payload["outcome"] == "FORCE_CLOSE"
    assert decision_payload["checks"]["nested"]["values"] == ["2.5"]
    assert sorted(decision_payload["checks"]["tags"]) == ["hold", "risk"]
    assert decision_payload["directive"]["action"] == "CLOSE_POSITION"


def test_event_sink_keeps_an_empty_journal_that_defines_len():
    class _ListJournal(list):
        def append(self, event_type, **payload):
            super().append((event_type, payload))

    journal = _ListJournal()
    assert not journal  # empty and falsy, but still a destination

    result = EventSink(destinations=(journal, None)).emit("trade_intent_created", symbol="BTCUSDT")

    assert result.success is True
    assert journal == [("trade_intent_created", {"symbol": "BTCUSDT"})]


def test_journal_record_uses_the_payload_severity_once(tmp_path):
    payload = {
        "severity": "WARNING",
        "symbol": "BTCUSDT",
        "run_id": "journal-run",
        "diagnostics": {"reason": "shadow advisory"},
    }

    record = journal_event_record(
        tmp_path / "governor_events.jsonl",
        "learning_advisory_shadow_evaluated",
        payload,
    )

    assert record.channel is EventChannel.AUDIT
    assert record.category == "governor"
    assert record.event_type == "learning_advisory_shadow_evaluated"
    assert record.severity == "WARNING"
    assert "severity" not in record.payload
    assert record.payload["symbol"] == "BTCUSDT"
    assert record.payload["run_id"] == "journal-run"
    assert record.payload["diagnostics"] == {"reason": "shadow advisory"}
    assert record.payload["legacy_journal_path"] == str(tmp_path / "governor_events.jsonl")
    assert payload["severity"] == "WARNING"


def test_journal_record_infers_the_default_severity(tmp_path):
    record = journal_event_record(
        tmp_path / "governor_events.jsonl",
        "governor_defensive_mode_shadow",
        {"symbol": "BTCUSDT", "diagnostics": {"reason": "defensive shadow"}},
    )

    assert record.category == "governor"
    assert record.severity == "INFO"
    assert record.payload["diagnostics"] == {"reason": "defensive shadow"}


def test_journal_line_reaches_the_run_audit_trail(tmp_path):
    reset_for_testing(tmp_path / "runs")
    journal = TradeJournal(str(tmp_path / "data" / "trades_btcusdt_testnet.jsonl"))

    journal.append("entry_order_failed", symbol="BTCUSDT", reason="timeout")

    events = _read_jsonl(tmp_path / "runs" / get_run_id() / "events" / "execution_events.jsonl")
    (event,) = [item for item in events if item["event_type"] == "entry_order_failed"]
    assert event["component"] == "utils.trade_journal"
    assert event["severity"] == "ERROR"
    assert event["payload"]["reason"] == "timeout"
