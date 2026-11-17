import json
import os
import subprocess
from pathlib import Path

import pytest

from desktop.services.operation_models import PreflightStatus
from desktop.services.preflight_service import (
    PreflightService,
    apply_partial_approval,
    extract_preflight_messages,
    load_json_from_mixed_output,
)


class Runner:
    def __init__(self, completed):
        self.completed = completed
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.completed


def completed(payload, returncode=0, prefix=""):
    return subprocess.CompletedProcess(
        args=["python"],
        returncode=returncode,
        stdout=prefix + json.dumps(payload),
    )


def service(tmp_path, result):
    runner = Runner(result)
    instance = PreflightService(
        project_root=tmp_path,
        python_executable="python",
        command_runner=runner,
        sanitizer=lambda value: value,
    )
    return instance, runner


def test_ready_preflight_and_process_environment(tmp_path):
    instance, runner = service(
        tmp_path,
        completed({"ok": True, "summary": "Tudo certo", "checks": []}),
    )

    result = instance.run({"ALPHORA_MULTI_SYMBOL_ENABLED": "false"})

    assert result.status is PreflightStatus.READY
    _, kwargs = runner.calls[0]
    assert str((Path(tmp_path) / "src").resolve()) in kwargs["env"]["PYTHONPATH"].split(os.pathsep)


def test_warning_preflight_requires_confirmation(tmp_path):
    payload = {
        "ok": True,
        "checks": [{"ok": True, "severity": "warning", "message": "Atenção"}],
    }
    instance, _ = service(tmp_path, completed(payload))

    result = instance.run({})

    assert result.status is PreflightStatus.REQUIRES_CONFIRMATION
    assert result.confirmation is not None
    assert result.confirmation.key == "preflight_warnings"


def test_partial_preflight_disables_only_blocked_symbols(tmp_path):
    payload = {
        "ok": False,
        "global_ok": True,
        "approved_symbols": ["BTCUSDT"],
        "blocked_symbols": ["ethusdt"],
    }
    instance, _ = service(tmp_path, completed(payload))

    result = instance.run({"ALPHORA_MULTI_SYMBOL_ENABLED": "true"})
    updated = apply_partial_approval(
        [
            {"symbol": "BTCUSDT", "enabled": True},
            {"symbol": "ETHUSDT", "enabled": True},
        ],
        result,
    )

    assert result.status is PreflightStatus.PARTIAL
    assert result.approved_symbols == ("BTCUSDT",)
    assert result.blocked_symbols == ("ETHUSDT",)
    assert updated[0]["enabled"] is True
    assert updated[1]["enabled"] is False


def test_blocked_preflight_collects_symbol_messages(tmp_path):
    payload = {
        "ok": False,
        "global_ok": False,
        "symbol_checks": {
            "BTCUSDT": [{"ok": False, "severity": "error", "message": "Regra inválida"}]
        },
    }
    instance, _ = service(tmp_path, completed(payload))

    result = instance.run({"ALPHORA_MULTI_SYMBOL_ENABLED": "true"})

    assert result.status is PreflightStatus.BLOCKED
    assert "BTCUSDT: Regra inválida" in result.messages[0].message


def test_invalid_json_returns_error(tmp_path):
    result_process = subprocess.CompletedProcess(
        args=["python"],
        returncode=0,
        stdout="saída sem json",
    )
    instance, _ = service(tmp_path, result_process)

    result = instance.run({})

    assert result.status is PreflightStatus.ERROR
    assert result.messages[0].code == "preflight_invalid_json"


def test_nonzero_exit_returns_error(tmp_path):
    instance, _ = service(tmp_path, completed({"ok": False}, returncode=2))

    result = instance.run({})

    assert result.status is PreflightStatus.ERROR
    assert result.messages[0].code == "preflight_nonzero_exit"


def test_parser_reads_json_after_log_prefix():
    payload = load_json_from_mixed_output('INFO iniciando\n{"ok": true}\n')
    assert payload == {"ok": True}


def test_message_extraction_matches_preflight_structure():
    payload = {
        "checks": [{"ok": False, "severity": "error", "message": "geral"}],
        "global_checks": [{"ok": True, "severity": "warning", "message": "aviso"}],
        "symbol_checks": {"ethusdt": [{"ok": False, "severity": "error", "message": "símbolo"}]},
    }

    assert extract_preflight_messages(payload, include_warnings=False) == [
        "geral",
        "ETHUSDT: símbolo",
    ]
    assert extract_preflight_messages(payload, include_warnings=True) == ["aviso"]


def test_partial_approval_rejects_non_partial_result(tmp_path):
    instance, _ = service(tmp_path, completed({"ok": True}))
    result = instance.run({})

    with pytest.raises(ValueError):
        apply_partial_approval([], result)


def test_invalid_sanitized_payload_fails_closed(tmp_path):
    runner = Runner(completed({"ok": True, "summary": "Tudo certo"}))
    instance = PreflightService(
        project_root=tmp_path,
        python_executable="python",
        command_runner=runner,
        sanitizer=lambda value: "invalid" if isinstance(value, dict) else value,
    )

    result = instance.run({})

    assert result.status is PreflightStatus.BLOCKED
    assert "formato inválido" in result.messages[0].message


def test_unexpected_runner_error_returns_internal_error(tmp_path):
    def broken_runner(*args, **kwargs):
        raise ValueError("unexpected")

    instance = PreflightService(
        project_root=tmp_path,
        command_runner=broken_runner,
        sanitizer=lambda value: value,
    )

    result = instance.run({})

    assert result.status is PreflightStatus.ERROR
    assert result.messages[0].code == "preflight_internal_error"
