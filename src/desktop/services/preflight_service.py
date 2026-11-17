from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from desktop.services.operation_models import (
    ConfirmationKind,
    ConfirmationRequest,
    MessageSeverity,
    OperationMessage,
    PreflightResult,
    PreflightStatus,
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
Sanitizer = Callable[[Any], Any]


class PreflightService:
    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        python_executable: str | None = None,
        timeout_seconds: float = 60.0,
        command_runner: CommandRunner | None = None,
        sanitizer: Sanitizer | None = None,
    ) -> None:
        self._project_root = Path(project_root) if project_root is not None else Path.cwd()
        self._python_executable = python_executable or sys.executable
        self._timeout_seconds = float(timeout_seconds)
        self._command_runner = command_runner or subprocess.run
        self._sanitizer = sanitizer or _default_sanitizer()

    def run(self, environment: Mapping[str, str]) -> PreflightResult:
        try:
            return self._run(environment)
        except Exception as error:
            return PreflightResult(
                status=PreflightStatus.ERROR,
                messages=(
                    OperationMessage(
                        code="preflight_internal_error",
                        title="Preflight falhou",
                        message=_safe_text(self._sanitizer, error),
                        severity=MessageSeverity.ERROR,
                    ),
                ),
            )

    def _run(self, environment: Mapping[str, str]) -> PreflightResult:
        command = [self._python_executable, "-m", "ops.preflight", "--json"]
        process_environment = _build_process_environment(
            project_root=self._project_root,
            overrides=environment,
        )

        try:
            completed = self._command_runner(
                command,
                cwd=self._project_root,
                env=process_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return PreflightResult(
                status=PreflightStatus.ERROR,
                messages=(
                    OperationMessage(
                        code="preflight_execution_error",
                        title="Preflight falhou",
                        message=_safe_text(self._sanitizer, error),
                        severity=MessageSeverity.ERROR,
                    ),
                ),
            )

        output = _safe_text(self._sanitizer, completed.stdout or "")
        if completed.returncode != 0:
            return PreflightResult(
                status=PreflightStatus.ERROR,
                output=output,
                messages=(
                    OperationMessage(
                        code="preflight_nonzero_exit",
                        title="Preflight falhou",
                        message="Preflight terminou com erro.",
                        severity=MessageSeverity.ERROR,
                    ),
                ),
            )

        try:
            raw_payload = load_json_from_mixed_output(completed.stdout or "{}")
        except json.JSONDecodeError:
            return PreflightResult(
                status=PreflightStatus.ERROR,
                output=output,
                messages=(
                    OperationMessage(
                        code="preflight_invalid_json",
                        title="Preflight inválido",
                        message="Não foi possível ler JSON do preflight.",
                        severity=MessageSeverity.ERROR,
                    ),
                ),
            )

        payload = _safe_payload(self._sanitizer, raw_payload)
        if bool(payload.get("ok", False)):
            warnings = extract_preflight_messages(payload, include_warnings=True)
            if warnings:
                message = "Preflight retornou avisos:\n\n" + "\n".join(warnings)
                return PreflightResult(
                    status=PreflightStatus.REQUIRES_CONFIRMATION,
                    payload=payload,
                    output=output,
                    messages=(
                        OperationMessage(
                            code="preflight_warnings",
                            title="Preflight com avisos",
                            message=message,
                            severity=MessageSeverity.WARNING,
                        ),
                    ),
                    confirmation=ConfirmationRequest(
                        key="preflight_warnings",
                        kind=ConfirmationKind.ACKNOWLEDGEMENT,
                        title="Preflight com avisos",
                        message=message + "\n\nDeseja iniciar mesmo assim?",
                    ),
                )
            return PreflightResult(
                status=PreflightStatus.READY,
                payload=payload,
                output=output,
                messages=(
                    OperationMessage(
                        code="preflight_ready",
                        title="Preflight concluído",
                        message=str(payload.get("summary", "Preflight aprovado.")),
                    ),
                ),
            )

        approved = _normalized_symbols(payload.get("approved_symbols", []))
        blocked = _normalized_symbols(payload.get("blocked_symbols", []))
        multi_symbol = str(environment.get("ALPHORA_MULTI_SYMBOL_ENABLED", "")).lower() == "true"
        if multi_symbol and bool(payload.get("global_ok", False)) and approved and blocked:
            message = (
                "Alguns símbolos foram bloqueados pelo preflight:\n\n"
                + "\n".join(blocked)
                + "\n\nDeseja iniciar apenas os símbolos aprovados?"
            )
            return PreflightResult(
                status=PreflightStatus.PARTIAL,
                payload=payload,
                output=output,
                approved_symbols=approved,
                blocked_symbols=blocked,
                messages=(
                    OperationMessage(
                        code="preflight_partial",
                        title="Preflight parcial",
                        message=message,
                        severity=MessageSeverity.WARNING,
                    ),
                ),
                confirmation=ConfirmationRequest(
                    key="preflight_partial_symbols",
                    kind=ConfirmationKind.ACKNOWLEDGEMENT,
                    title="Preflight parcial",
                    message=message,
                ),
            )

        errors = extract_preflight_messages(payload, include_warnings=False)
        message = "\n".join(errors) or str(payload.get("summary", "Preflight bloqueou o início."))
        return PreflightResult(
            status=PreflightStatus.BLOCKED,
            payload=payload,
            output=output,
            approved_symbols=approved,
            blocked_symbols=blocked,
            messages=(
                OperationMessage(
                    code="preflight_blocked",
                    title="Início bloqueado pelo preflight",
                    message=message,
                    severity=MessageSeverity.ERROR,
                ),
            ),
        )


def apply_partial_approval(
    symbol_profiles: list[dict[str, Any]],
    result: PreflightResult,
) -> list[dict[str, Any]]:
    if result.status is not PreflightStatus.PARTIAL:
        raise ValueError("O resultado informado não representa um preflight parcial.")

    blocked = set(result.blocked_symbols)
    profiles = deepcopy(symbol_profiles)
    for profile in profiles:
        symbol = str(profile.get("symbol", "")).upper().strip()
        if symbol in blocked:
            profile["enabled"] = False
    return profiles


def load_json_from_mixed_output(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise json.JSONDecodeError("Nenhum objeto JSON encontrado", text, 0)


def extract_preflight_messages(
    payload: Mapping[str, Any],
    *,
    include_warnings: bool,
) -> list[str]:
    messages: list[str] = []

    for check in _check_items(payload.get("checks", [])):
        _append_check_message(messages, check, include_warnings=include_warnings)

    for check in _check_items(payload.get("global_checks", [])):
        _append_check_message(messages, check, include_warnings=include_warnings)

    symbol_checks = payload.get("symbol_checks", {})
    if isinstance(symbol_checks, Mapping):
        for symbol, checks in symbol_checks.items():
            for check in _check_items(checks):
                before = len(messages)
                _append_check_message(messages, check, include_warnings=include_warnings)
                if len(messages) > before:
                    messages[-1] = f"{str(symbol).upper()}: {messages[-1]}"

    return [message for message in messages if message]


def _check_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _append_check_message(
    messages: list[str],
    check: Mapping[str, Any],
    *,
    include_warnings: bool,
) -> None:
    severity = str(check.get("severity", "")).lower()
    should_include = severity == "warning" if include_warnings else not bool(check.get("ok", False))
    if should_include:
        message = str(check.get("message", "")).strip()
        if message:
            messages.append(message)


def _build_process_environment(
    *,
    project_root: Path,
    overrides: Mapping[str, str],
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({str(key): str(value) for key, value in overrides.items()})

    src_path = str((project_root / "src").resolve())
    current_python_path = environment.get("PYTHONPATH", "")
    entries = [entry for entry in current_python_path.split(os.pathsep) if entry]
    if src_path not in entries:
        entries.insert(0, src_path)
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    return environment


def _normalized_symbols(values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set)):
        return ()
    symbols: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value).upper().strip()
        if symbol and symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)
    return tuple(symbols)


def _safe_text(sanitizer: Sanitizer, value: Any) -> str:
    try:
        return str(sanitizer(value))
    except Exception:
        return "[SANITIZATION_ERROR]"


def _safe_payload(sanitizer: Sanitizer, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        sanitized = sanitizer(payload)
    except Exception:
        return {
            "ok": False,
            "summary": "Não foi possível sanitizar o resultado do preflight.",
        }
    if isinstance(sanitized, dict):
        return sanitized
    return {
        "ok": False,
        "summary": "O resultado sanitizado do preflight possui formato inválido.",
    }


def _default_sanitizer() -> Sanitizer:
    from utils.privacy import sanitize_for_audit

    return sanitize_for_audit
