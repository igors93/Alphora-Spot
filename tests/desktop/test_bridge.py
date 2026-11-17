"""Contract of the JSON-lines bridge used by the native desktop interface."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from desktop.bridge import handlers
from desktop.bridge.errors import BridgeError
from desktop.bridge.handlers import Bridge
from desktop.bridge.protocol import BRIDGE_PROTOCOL_VERSION
from desktop.bridge.server import SLOW_METHODS, JsonLineChannel, serve
from utils.audit_logger import get_run_dir

SRC = Path(__file__).resolve().parents[2] / "src"


def _run_protocol(dispatch, requests: list[Any]) -> list[dict[str, Any]]:
    reader = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
    output = io.StringIO()
    serve(
        dispatch,
        reader=reader,
        channel=JsonLineChannel(output),
        ready_payload={"protocol": BRIDGE_PROTOCOL_VERSION},
    )
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_protocol_answers_by_id_and_reports_errors():
    def dispatch(method: str, params: dict[str, Any]) -> Any:
        if method == "echo":
            return params
        if method == "fail":
            raise BridgeError("Título", "Mensagem")
        raise RuntimeError("boom")

    messages = _run_protocol(
        dispatch,
        [
            {"id": 1, "method": "echo", "params": {"a": 1}},
            {"id": 2, "method": "fail"},
            {"id": 3, "method": "crash"},
            {"method": "no-id"},
        ],
    )

    assert messages[0] == {"event": "ready", "data": {"protocol": 1}}
    by_id = {message["id"]: message for message in messages[1:]}
    assert set(by_id) == {1, 2, 3}
    assert by_id[1] == {"id": 1, "ok": True, "result": {"a": 1}}
    assert by_id[2]["error"] == {"title": "Título", "message": "Mensagem"}
    assert by_id[3]["ok"] is False
    assert by_id[3]["error"]["title"] == "Erro interno"


def test_protocol_ignores_malformed_lines():
    reader = io.StringIO('not json\n\n{"id": 5, "method": "x"}\n')
    output = io.StringIO()
    serve(
        lambda *_: 42,
        reader=reader,
        channel=JsonLineChannel(output),
        ready_payload={"protocol": BRIDGE_PROTOCOL_VERSION},
    )
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert lines[-1] == {"id": 5, "ok": True, "result": 42}


def test_slow_methods_never_hold_quick_reads_back():
    # Four slow calls fill their pool and wait until a quick read has been answered;
    # with a single shared pool the quick read would queue behind them instead.
    answered = threading.Event()

    def dispatch(method: str, params: dict[str, Any]) -> Any:
        if method in SLOW_METHODS:
            return answered.wait(timeout=5)
        answered.set()
        return "quick"

    slow = [{"id": index, "method": "operation.authorize"} for index in range(1, 5)]
    messages = _run_protocol(dispatch, [*slow, {"id": 99, "method": "symbols.rows"}])

    by_id = {message["id"]: message for message in messages[1:]}
    assert by_id[99]["result"] == "quick"
    assert all(by_id[index]["result"] is True for index in range(1, 5))


@pytest.fixture
def bridge(tmp_path: Path) -> Bridge:
    (tmp_path / "config").mkdir()
    return Bridge(tmp_path)


def test_hello_exposes_valid_module_modes(bridge: Bridge):
    assert bridge.hello()["protocol"] == BRIDGE_PROTOCOL_VERSION
    options = bridge.hello()["options"]
    modules = {item["key"]: item for item in options["modules"]}

    reconciliation = [option["value"] for option in modules["state_reconciliation_mode"]["options"]]
    assert "REPAIR_SAFE" in reconciliation
    assert "REPAIR" not in reconciliation
    assert options["activation_levels"]["DRY_RUN"] == ["SHADOW_ONLY"]
    assert "AUTO_REPAIR" in options["profile_choices"]["protection_policy"]


def test_python_and_rust_use_the_same_39_bridge_methods(bridge: Bridge):
    interface = Path(__file__).resolve().parents[2] / "interface" / "src"
    source = "\n".join(path.read_text(encoding="utf-8") for path in interface.rglob("*.rs"))
    prefixes = {method.split(".", 1)[0] for method in bridge._handlers}
    ui_methods = {
        method
        for method in re.findall(r'"([a-z_]+\.[a-z_]+)"', source)
        if method.split(".", 1)[0] in prefixes
    }
    assert len(bridge._handlers) == 39
    assert ui_methods == set(bridge._handlers)
    assert f"const BRIDGE_PROTOCOL_VERSION: u32 = {BRIDGE_PROTOCOL_VERSION};" in source


def test_task_lifecycle_reaches_the_single_audit_sink(bridge: Bridge):
    bridge.audit_task({"phase": "started", "kind": "backtest", "pid": 42})
    bridge.audit_task({"phase": "finished", "kind": "backtest", "status": "ERROR", "exit_code": 1})

    run_dir = get_run_dir()
    assert run_dir is not None
    events = [
        json.loads(line)
        for line in (run_dir / "events" / "desktop_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    lifecycle = [
        event for event in events if event["event_type"] in {"task_started", "task_finished"}
    ]
    assert [event["event_type"] for event in lifecycle] == ["task_started", "task_finished"]
    assert lifecycle[1]["severity"] == "ERROR"


def test_unknown_method_is_a_user_facing_error(bridge: Bridge):
    with pytest.raises(BridgeError):
        bridge.dispatch("nope", {})


def test_credentials_are_saved_and_only_masked_back(bridge: Bridge):
    result = bridge.dispatch(
        "credentials.save",
        {"environment": "TESTNET", "api_key": "ABCDEFGHIJKLMNOP", "api_secret": "secret-value"},
    )

    testnet = next(item for item in result["summaries"] if item["environment"] == "TESTNET")
    assert testnet["configured"] is True
    assert testnet["apiKeyPreview"] == "ABCD••••••••MNOP"
    assert "secret-value" not in json.dumps(result)

    cleared = bridge.dispatch("credentials.clear", {"environment": "TESTNET"})
    testnet = next(item for item in cleared["summaries"] if item["environment"] == "TESTNET")
    assert testnet["configured"] is False


def test_credentials_require_key_and_secret_together(bridge: Bridge):
    with pytest.raises(BridgeError):
        bridge.dispatch("credentials.save", {"environment": "REAL", "api_key": "only-key"})


def test_symbol_check_normalizes_and_rejects_duplicates(bridge: Bridge):
    result = bridge.dispatch(
        "symbols.check",
        {
            "profile": {"symbol": " ethusdt "},
            "profiles": [{"symbol": "ETHUSDT"}],
            "exclude_index": None,
        },
    )
    assert result["profile"]["symbol"] == "ETHUSDT"
    assert any("já existe" in error for error in result["errors"])


def test_symbols_save_round_trip(bridge: Bridge):
    loaded = bridge.dispatch("symbols.load", {})
    profiles = loaded["profiles"]
    profiles.append({**profiles[0], "symbol": "ETHUSDT"})
    portfolio = {**loaded["portfolio"], "max_open_positions": "2"}

    saved = bridge.dispatch("symbols.save", {"profiles": profiles, "portfolio": portfolio})

    assert [item["symbol"] for item in saved["profiles"]] == ["BTCUSDT", "ETHUSDT"]
    assert saved["portfolio"]["max_open_positions"] == "2"


def test_symbols_save_rejects_invalid_portfolio(bridge: Bridge):
    loaded = bridge.dispatch("symbols.load", {})
    with pytest.raises(BridgeError) as error:
        bridge.dispatch(
            "symbols.save",
            {"profiles": loaded["profiles"], "portfolio": {"max_open_positions": "zero"}},
        )
    assert error.value.title == "Risco de portfólio inválido"


def test_prepare_blocks_without_credentials(bridge: Bridge):
    result = bridge.dispatch(
        "operation.prepare", {"execution_mode": "DRY_RUN", "environment": "TESTNET"}
    )
    assert result["status"] == "BLOCKED"
    assert result["title"]


def test_prepare_rejects_unknown_execution_mode(bridge: Bridge):
    with pytest.raises(BridgeError):
        bridge.dispatch("operation.prepare", {"execution_mode": "YOLO", "environment": "REAL"})


def test_authorize_without_plan_is_rejected(bridge: Bridge):
    with pytest.raises(BridgeError):
        bridge.dispatch("operation.authorize", {"responses": []})


def test_snapshot_returns_only_the_requested_sections(bridge: Bridge):
    events_only = bridge.observability_snapshot({"sections": ["events"]})
    full = bridge.observability_snapshot({})

    assert set(events_only) == {"events", "updated_at"}
    assert {"journal_text", "scanner_text", "state_text", "sources"} <= set(full)


def test_locate_refuses_paths_outside_the_workspace(bridge: Bridge, tmp_path: Path):
    with pytest.raises(BridgeError):
        bridge.dispatch("observability.locate", {"path": str(tmp_path.parent / "elsewhere")})
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.jsonl").write_text("{}\n", encoding="utf-8")
    found = bridge.dispatch("observability.locate", {"candidates": ["data/x.jsonl"]})
    assert found["path"] == str((tmp_path / "data" / "x.jsonl").resolve())


def test_equity_curve_is_downsampled_keeping_extremes(tmp_path: Path):
    path = tmp_path / "equity.csv"
    rows = ["timestamp,equity"] + [f"{index},{1000 + (index % 97)}" for index in range(5000)]
    rows[2501] = "2500,5000"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    curve = handlers._load_equity_curve(str(path))

    assert len(curve) <= handlers._EQUITY_POINTS
    assert max(curve) == 5000.0
    assert curve[0] == 1000.0
    assert curve[-1] == float(1000 + (4999 % 97))


def test_backtest_values_keep_only_known_keys():
    values = handlers._backtest_values({"symbol": "btcusdt ", "unknown": 1}, partial=True)
    assert values == {"symbol": "btcusdt"}


def test_bridge_process_speaks_json_lines(tmp_path: Path):
    environment = {
        **os.environ,
        "ALPHORA_PROJECT_ROOT": str(tmp_path),
        "PYTHONPATH": str(SRC),
        "PYTHONIOENCODING": "utf-8",
    }
    request = json.dumps({"id": 1, "method": "credentials.load", "params": {}}) + "\n"
    completed = subprocess.run(
        [sys.executable, "-m", "desktop.bridge"],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    messages = [json.loads(line) for line in completed.stdout.splitlines()]
    assert messages[0]["event"] == "ready"
    assert messages[0]["data"]["root"] == str(tmp_path.resolve())
    assert messages[1]["id"] == 1 and messages[1]["ok"] is True
