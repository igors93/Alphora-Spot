"""Run audit: manifest, summary, retention and the audit sink of the event API.

Events reach this module through ``utils.events``: ``AUDIT_RUN_SINK`` writes every record of
the ``AUDIT`` channel to ``runtime/runs/<run_id>/events/<category>_events.jsonl`` and feeds the
run summary counters (migration M7, ADR-006).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from operational_module_modes import (
    operational_module_mode_audit_records,
    operational_module_mode_snapshot,
)
from utils.events import EventChannel, EventRecord, observe_event
from utils.run_context import (
    get_run_id as _shared_get_run_id,
)
from utils.run_context import (
    initialize_run_context,
    reset_run_context_for_testing,
)

AUDIT_CATEGORIES = {
    "system",
    "governor",
    "strategy",
    "risk",
    "execution",
    "market_data",
    "exchange",
    "backtesting",
    "desktop",
}

_RUN_DIR: Path | None = None
_STARTED_AT_UTC = datetime.now(timezone.utc)
_FINALIZED = False
_ATEXIT_REGISTERED = False
_JSONL_LOCKS: dict[Path, threading.Lock] = {}
_JSONL_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class AuditSettings:
    runs_dir: Path = Path("runtime/runs")
    retention_days: int = 30
    max_runs: int = 100
    jsonl_enabled: bool = True
    text_logs_enabled: bool = True
    full_traceback: bool = False


_SETTINGS = AuditSettings()
_TESTING_MODE = False  # True while reset_for_testing is active; blocks settings overwrite
_SUMMARY_COUNTERS: dict[str, int] = {
    "total_cycles": 0,
    "total_errors": 0,
    "total_warnings": 0,
    "total_strategy_decisions": 0,
    "total_orders_submitted": 0,
    "total_orders_failed": 0,
    "total_risk_blocks": 0,
    "total_exchange_rule_blocks": 0,
    "stale_market_data_count": 0,
    "slow_cycle_count": 0,
    "unhandled_exception_count": 0,
}


def reset_for_testing(tmp_dir: Path | None = None) -> None:
    """Reset all global audit-logger state so tests start from a clean slate.

    Each call assigns a new run_id and redirects the audit run directory to an
    isolated temporary location so test events never land in the operational
    runtime/runs tree or in the previous test's directory.  File handlers that
    were installed by _configure_text_handlers are removed from the root logger
    so log records from one test do not bleed into another test's files.

    ``tmp_dir``: an already-existing directory to use as the runs root.
    When omitted a new temporary directory is created via ``tempfile.mkdtemp``
    and is left on disk (pytest's tmp_path fixture handles cleanup for callers
    that pass it; the mkdtemp path is transient test data and harmless to keep).

    This function must only be called from test code.  Production code must
    never call it.
    """
    import tempfile

    global _RUN_DIR, _STARTED_AT_UTC, _FINALIZED, _ATEXIT_REGISTERED
    global _SETTINGS, _TESTING_MODE

    # Remove run-specific file handlers so subsequent log records are not
    # routed to the previous test's run directory.
    try:
        from utils.logger import remove_runtime_file_handlers

        remove_runtime_file_handlers()
    except Exception:
        pass

    runs_root = tmp_dir if tmp_dir is not None else Path(tempfile.mkdtemp(prefix="alphora_test_"))

    reset_run_context_for_testing()
    initialize_run_context(run_id=uuid.uuid4().hex[:12])
    _RUN_DIR = None
    _STARTED_AT_UTC = datetime.now(timezone.utc)
    _FINALIZED = False
    _ATEXIT_REGISTERED = False

    # Point the runs dir at the isolated tmp location and disable text file
    # handlers so tests don't install persistent file handlers into the root
    # logger.  JSONL remains enabled so code paths that write events (e.g.
    # reconciliation reports) still work inside the isolated directory.
    # _TESTING_MODE = True prevents initialize_run_audit from overwriting
    # these settings when called lazily via _ensure_run_dir().
    _SETTINGS = AuditSettings(
        runs_dir=runs_root,
        text_logs_enabled=False,
    )
    _TESTING_MODE = True

    with _JSONL_LOCKS_GUARD:
        _JSONL_LOCKS.clear()

    for key in list(_SUMMARY_COUNTERS):
        _SUMMARY_COUNTERS[key] = 0


def get_run_id() -> str:
    return _shared_get_run_id()


def get_run_dir() -> Path | None:
    return _RUN_DIR


def initialize_run_audit(config: Any | None = None) -> Path | None:
    global _FINALIZED, _RUN_DIR, _SETTINGS

    initialize_run_context(config=config)

    # In testing mode _SETTINGS is controlled by reset_for_testing (which
    # sets an isolated runs_dir and disables text file handlers).  We must
    # not overwrite those settings when config is None (lazy init from
    # _ensure_run_dir), or we would re-enable real file I/O and lose the
    # isolated tmp directory.  When a real config is supplied (production
    # boot path), we always override — the caller owns the settings.
    if not _TESTING_MODE or config is not None:
        _SETTINGS = _settings_from_config(config)
    _FINALIZED = False
    requested_run_dir = _SETTINGS.runs_dir / get_run_id()
    run_dir = _prepare_or_fallback_run_dir(requested_run_dir)
    if run_dir is None:
        return None

    _RUN_DIR = run_dir
    _write_manifest(config)
    _write_snapshots(config)
    cleanup_old_runs(
        _SETTINGS.runs_dir,
        current_run_id=get_run_id(),
        retention_days=_SETTINGS.retention_days,
        max_runs=_SETTINGS.max_runs,
    )
    _register_atexit()
    _configure_text_handlers(run_dir)
    log = logging.getLogger(__name__)
    if config is not None:
        log.info(
            "Run audit directory initialized | run_id=%s | path=%s",
            get_run_id(),
            run_dir,
        )
    else:
        log.debug(
            "Run audit directory initialized (lazy) | run_id=%s | path=%s",
            get_run_id(),
            run_dir,
        )
    observe_event(
        "run_started",
        component=__name__,
        category="system",
        channel=EventChannel.AUDIT,
        execution_mode=_enum_value(getattr(config, "execution_mode", "")),
        environment=_enum_value(getattr(config, "environment", "")),
        symbols=_manifest_effective_symbols(config),
    )
    for record in operational_module_mode_audit_records(config):
        observe_event(
            "operational_module_modes_loaded",
            component=__name__,
            category="system",
            channel=EventChannel.AUDIT,
            **record,
        )
    return run_dir


def audit_event(
    category: str,
    event_type: str,
    severity: str = "INFO",
    **payload: Any,
) -> dict[str, Any] | None:
    return _write_audit_event(category, event_type, severity, payload)


class _AuditRunSink:
    """The ``AUDIT`` channel of ``utils.events``, written to the run directory."""

    def accepts(self, record: EventRecord) -> bool:
        return record.channel is EventChannel.AUDIT

    def write(self, record: EventRecord) -> None:
        payload = dict(record.payload)
        if record.error is not None:
            payload["error"] = _exception_payload(record.error)
            logging.getLogger(__name__).error(
                "Audit error | category=%s | event_type=%s | error=%s",
                record.category,
                record.event_type,
                record.error,
                exc_info=_SETTINGS.full_traceback,
            )
        _write_audit_event(
            record.category,
            record.event_type,
            record.severity,
            payload,
            component=record.component,
        )


AUDIT_RUN_SINK = _AuditRunSink()


def _write_audit_event(
    category: str,
    event_type: str,
    severity: str,
    payload: dict[str, Any],
    *,
    component: str = "",
) -> dict[str, Any] | None:
    payload.pop("_observability_mirrored", None)
    if not _SETTINGS.jsonl_enabled:
        return None

    run_dir = _ensure_run_dir()
    if run_dir is None:
        return None

    normalized_category = _normalize_category(category)
    normalized_severity = str(severity or "INFO").upper().strip() or "INFO"
    safe_payload = sanitize_for_logging(payload)
    event = {
        "timestamp": _utc_now_iso(),
        "run_id": get_run_id(),
        "category": normalized_category,
        "event_type": str(event_type),
        "symbol": _first_payload_value(safe_payload, "symbol"),
        "strategy": _first_payload_value(safe_payload, "strategy", "strategy_name"),
        "strategy_name": _first_payload_value(safe_payload, "strategy_name", "strategy"),
        "cycle_id": _first_payload_value(safe_payload, "cycle_id"),
        "decision_id": _first_payload_value(safe_payload, "decision_id"),
        "plan_id": _first_payload_value(safe_payload, "plan_id", "trade_plan_id"),
        "order_client_id": _first_payload_value(
            safe_payload,
            "order_client_id",
            "client_order_id",
            "entry_client_order_id",
            "close_client_order_id",
            "client_algo_id",
            "stop_client_algo_id",
            "take_client_algo_id",
        ),
        "severity": normalized_severity,
        "payload": safe_payload,
    }
    if component:
        event["component"] = component
    append_jsonl(_event_path(run_dir, normalized_category), event)
    _update_summary_counters(event)
    # NOTE: audit events are intentionally NOT mirrored to the observability
    # AUDIT channel: its sink writes to the same events/{category}_events.jsonl
    # file, which duplicated every event in two different schemas.
    return event


def audit_error(
    category: str,
    error: BaseException,
    event_type: str = "error",
    **payload: Any,
) -> dict[str, Any] | None:
    error_payload = dict(payload)
    error_payload["error"] = _exception_payload(error)
    logging.getLogger(__name__).error(
        "Audit error | category=%s | event_type=%s | error=%s",
        category,
        event_type,
        error,
        exc_info=_SETTINGS.full_traceback,
    )
    return audit_event(category, event_type, severity="ERROR", **error_payload)


def sanitize_for_logging(payload: Any) -> Any:
    """Converte tipos Python para JSON e aplica masking via logprivacy."""
    from utils.privacy import sanitize_for_audit

    return sanitize_for_audit(_make_jsonable(payload))


def _write_jsonl_line_with_retry(target: Path, line: str) -> None:
    from utils.jsonl_writer import shared_jsonl_writer

    shared_jsonl_writer().append_line(target, line)


def append_jsonl(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            sanitize_for_logging(payload),
            ensure_ascii=False,
            sort_keys=True,
        )
        with _lock_for_path(target):
            _write_jsonl_line_with_retry(target, line)
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Falha ao gravar audit JSONL | path=%s | error=%s",
            target,
            error,
        )


def append_run_log(filename: str, line: str) -> None:
    run_dir = _ensure_run_dir()
    if run_dir is None:
        return

    target = run_dir / "logs" / filename
    try:
        from utils.jsonl_writer import shared_jsonl_writer
        from utils.privacy import sanitize_for_audit

        safe_line = sanitize_for_audit(str(line))
        shared_jsonl_writer().append_line(target, str(safe_line).rstrip("\n"))
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Falha ao gravar log auxiliar do run | path=%s | error=%s",
            target,
            error,
        )


def finalize_run_audit(final_status: str = "COMPLETED") -> Path | None:
    global _FINALIZED

    if _FINALIZED:
        return None

    _FINALIZED = True
    observe_event(
        "run_finished",
        component=__name__,
        category="system",
        channel=EventChannel.AUDIT,
        final_status=final_status,
    )
    return write_run_summary(final_status=final_status)


def write_run_summary(final_status: str = "COMPLETED") -> Path | None:
    run_dir = _RUN_DIR
    if run_dir is None:
        return None

    finished_at = datetime.now(timezone.utc)
    summary = {
        "run_id": get_run_id(),
        "started_at_utc": _STARTED_AT_UTC.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "duration_seconds": max(0.0, (finished_at - _STARTED_AT_UTC).total_seconds()),
        "execution_mode": _manifest_value("execution_mode"),
        "environment": _manifest_value("environment"),
        "symbols": _manifest_value("symbols", []),
        **_SUMMARY_COUNTERS,
        "final_status": final_status,
    }
    path = run_dir / "reports" / "run_summary.json"
    _write_json_file(path, summary)
    return path


def update_run_manifest_fields(**fields: Any) -> None:
    """Merge late-resolved fields into run_manifest.json.

    The manifest is written during initialize_run_audit, before the governor
    exists — in GOVERNOR_MULTI_SYMBOL mode the effective symbol set is only
    known after the governor is built, so the caller pushes it here. The run
    summary reads the manifest at finalization and picks the update up.
    """
    if _RUN_DIR is None or not fields:
        return
    path = _RUN_DIR / "run_manifest.json"
    try:
        with path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)
        if not isinstance(manifest, dict):
            manifest = {}
    except Exception:
        manifest = {}
    manifest.update(sanitize_for_logging(fields))
    _write_json_file(path, manifest)


def cleanup_old_runs(
    runs_dir: str | Path,
    *,
    current_run_id: str,
    retention_days: int = 30,
    max_runs: int = 100,
) -> None:
    root = Path(runs_dir)
    if not root.exists() or not root.is_dir():
        return

    now = datetime.now(timezone.utc).timestamp()
    max_age_seconds = max(0, int(retention_days)) * 24 * 60 * 60
    candidates = [item for item in root.iterdir() if item.is_dir() and item.name != current_run_id]
    deletions: set[Path] = set()
    if max_age_seconds > 0:
        for item in candidates:
            try:
                age_seconds = now - item.stat().st_mtime
            except OSError:
                continue
            if age_seconds > max_age_seconds:
                deletions.add(item)

    if max_runs > 0:
        ordered = sorted(
            candidates,
            key=lambda item: item.stat().st_mtime if item.exists() else 0,
            reverse=True,
        )
        for item in ordered[max_runs - 1 :]:
            deletions.add(item)

    for item in sorted(deletions):
        if item.name == current_run_id:
            continue
        try:
            shutil.rmtree(item)
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Falha ao limpar run antigo | path=%s | error=%s",
                item,
                error,
            )


def _settings_from_config(config: Any | None) -> AuditSettings:
    return AuditSettings(
        runs_dir=Path(
            _env_or_attr(config, "ALPHORA_AUDIT_RUNS_DIR", "audit_runs_dir", "runtime/runs")
        ),
        retention_days=_int_env_or_attr(
            config,
            "ALPHORA_AUDIT_RETENTION_DAYS",
            "audit_retention_days",
            30,
        ),
        max_runs=_int_env_or_attr(config, "ALPHORA_AUDIT_MAX_RUNS", "audit_max_runs", 100),
        jsonl_enabled=_bool_env_or_attr(
            config,
            "ALPHORA_AUDIT_JSONL_ENABLED",
            "audit_jsonl_enabled",
            True,
        ),
        text_logs_enabled=_bool_env_or_attr(
            config,
            "ALPHORA_AUDIT_TEXT_LOGS_ENABLED",
            "audit_text_logs_enabled",
            True,
        ),
        full_traceback=_bool_env_or_attr(
            config,
            "ALPHORA_AUDIT_FULL_TRACEBACK",
            "audit_full_traceback",
            False,
        ),
    )


def _prepare_or_fallback_run_dir(requested_run_dir: Path) -> Path | None:
    try:
        _prepare_run_dir(requested_run_dir)
        return requested_run_dir
    except Exception as error:
        fallback = Path("logs") / "runs" / get_run_id()
        logging.getLogger(__name__).warning(
            "Run audit directory initialization failed | run_id=%s | path=%s | "
            "fallback=%s | error=%s",
            get_run_id(),
            requested_run_dir,
            fallback,
            error,
        )
        try:
            _prepare_run_dir(fallback)
            return fallback
        except Exception as fallback_error:
            logging.getLogger(__name__).warning(
                "Run audit fallback directory initialization failed | run_id=%s | "
                "path=%s | error=%s",
                get_run_id(),
                fallback,
                fallback_error,
            )
            return None


def _prepare_run_dir(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for child in ("logs", "events", "snapshots", "reports"):
        (run_dir / child).mkdir(parents=True, exist_ok=True)


def _ensure_run_dir() -> Path | None:
    if _RUN_DIR is not None:
        return _RUN_DIR
    try:
        return initialize_run_audit(None)
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Falha ao inicializar auditoria sob demanda | run_id=%s | error=%s",
            get_run_id(),
            error,
        )
        return None


def _configure_text_handlers(run_dir: Path) -> None:
    if not _SETTINGS.text_logs_enabled:
        return

    try:
        from utils.logger import configure_runtime_file_logging

        configure_runtime_file_logging(run_dir, enabled=True)
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Falha ao configurar logs textuais do run | path=%s | error=%s",
            run_dir,
            error,
        )


def _register_atexit() -> None:
    global _ATEXIT_REGISTERED

    if _ATEXIT_REGISTERED:
        return
    atexit.register(finalize_run_audit, "COMPLETED")
    _ATEXIT_REGISTERED = True


def _write_manifest(config: Any | None) -> None:
    if _RUN_DIR is None:
        return

    manifest = _manifest_payload(config)
    _write_json_file(_RUN_DIR / "run_manifest.json", manifest)


def _write_snapshots(config: Any | None) -> None:
    if _RUN_DIR is None:
        return

    snapshots_dir = _RUN_DIR / "snapshots"
    config_payload = (
        {"config_available": False}
        if config is None
        else sanitize_for_logging(asdict(config) if is_dataclass(config) else vars(config))  # type: ignore[arg-type]
    )
    _write_json_file(snapshots_dir / "config_snapshot.json", config_payload)
    _write_json_file(snapshots_dir / "strategy_snapshot.json", _strategy_snapshot(config))
    _write_json_file(snapshots_dir / "symbols_snapshot.json", _symbols_snapshot(config))


def _manifest_effective_symbols(config: Any | None) -> list[str]:
    """Return all symbols that will actually be evaluated this run.

    In GOVERNOR_MULTI_SYMBOL mode, config.symbols only holds the primary symbol while the full
    set lives in config.symbol_contexts. Prefer symbol_contexts when available so the manifest
    reflects the real operational scope instead of just the primary symbol.
    """
    if config is None:
        return []
    contexts = getattr(config, "symbol_contexts", None) or ()
    if contexts:
        symbols = [getattr(ctx, "symbol", None) for ctx in contexts]
        result = [s for s in symbols if s is not None]
        if result:
            return result
    return list(getattr(config, "symbols", ()) or ())


def _manifest_payload(config: Any | None) -> dict[str, Any]:
    return sanitize_for_logging(
        {
            "run_id": get_run_id(),
            "started_at_utc": _STARTED_AT_UTC.isoformat(),
            "execution_mode": _enum_value(getattr(config, "execution_mode", "")),
            "environment": _enum_value(getattr(config, "environment", "")),
            "use_testnet": getattr(config, "use_testnet", None),
            "execute_orders": getattr(config, "execute_orders", None),
            "allow_real_trading": getattr(config, "allow_real_trading", None),
            "strategy_module": getattr(config, "strategy_module", ""),
            "strategy_class": getattr(config, "strategy_class", ""),
            "strategy_params_present": bool(getattr(config, "strategy_params", None)),
            "symbols": _manifest_effective_symbols(config),
            "max_active_symbols": getattr(config, "max_active_symbols", None),
            "position_notional_usdt": getattr(config, "position_notional_usdt", None),
            "stop_loss_pct": getattr(config, "stop_loss_pct", None),
            "take_profit_pct": getattr(config, "take_profit_pct", None),
            "leverage": getattr(config, "leverage", None),
            "trend_context_enabled": getattr(config, "trend_context_enabled", None),
            "economic_gate_enabled": getattr(config, "economic_gate_enabled", None),
            "market_ws_enabled": getattr(config, "market_ws_enabled", None),
            "user_data_stream_enabled": getattr(config, "user_data_stream_enabled", None),
            "operational_module_modes": (
                {} if config is None else operational_module_mode_snapshot(config)
            ),
            "git_commit": _git_commit(),
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "process_id": os.getpid(),
        }
    )


def _strategy_snapshot(config: Any | None) -> dict[str, Any]:
    return sanitize_for_logging(
        {
            "strategy_module": getattr(config, "strategy_module", ""),
            "strategy_class": getattr(config, "strategy_class", ""),
            "strategy_params_present": bool(getattr(config, "strategy_params", None)),
            "strategy_params": getattr(config, "strategy_params", {}),
            "strategy_runtime_mode": getattr(config, "strategy_runtime_mode", ""),
        }
    )


def _symbols_snapshot(config: Any | None) -> dict[str, Any]:
    contexts = getattr(config, "symbol_contexts", ()) or ()
    return sanitize_for_logging(
        {
            "symbols": list(getattr(config, "symbols", ()) or ()),
            "symbol_contexts": [
                asdict(item) if is_dataclass(item) else dict(getattr(item, "__dict__", {}))  # type: ignore[arg-type]
                for item in contexts
            ],
        }
    )


def _write_json_file(path: Path, payload: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as file:
            json.dump(sanitize_for_logging(payload), file, ensure_ascii=False, indent=2)
            file.write("\n")
    except Exception as error:
        logging.getLogger(__name__).warning(
            "Falha ao gravar arquivo de auditoria | path=%s | error=%s",
            path,
            error,
        )


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2.0,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _manifest_value(key: str, default: Any = "") -> Any:
    if _RUN_DIR is None:
        return default
    path = _RUN_DIR / "run_manifest.json"
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except Exception:
        return default
    return payload.get(key, default)


def _event_path(run_dir: Path, category: str) -> Path:
    return run_dir / "events" / f"{category}_events.jsonl"


def _normalize_category(category: str) -> str:
    normalized = str(category or "system").lower().strip()
    if normalized in AUDIT_CATEGORIES:
        return normalized
    return "system"


def _first_payload_value(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in {None, ""}:
            return str(value)
    return ""


def _update_summary_counters(event: dict[str, Any]) -> None:
    event_type = str(event.get("event_type", "")).lower()
    category = str(event.get("category", "")).lower()
    severity = str(event.get("severity", "")).upper()
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    if severity == "WARNING":
        _SUMMARY_COUNTERS["total_warnings"] += 1
    if severity == "ERROR":
        _SUMMARY_COUNTERS["total_errors"] += 1
    if event_type == "cycle_finished":
        _SUMMARY_COUNTERS["total_cycles"] += 1
    if event_type in {"strategy_decision_created", "governor_decision", "trade_approved"}:
        _SUMMARY_COUNTERS["total_strategy_decisions"] += 1
    # Only order_submit_started: it fires exactly once per submission (normal
    # and algo orders). The *_submit_attempt journal mirrors fire for the same
    # submissions and double-counted entry/close orders.
    if event_type == "order_submit_started":
        _SUMMARY_COUNTERS["total_orders_submitted"] += 1
    if event_type in {"order_submit_failed"} or (
        "order" in event_type and any(token in event_type for token in ("failed", "unknown"))
    ):
        _SUMMARY_COUNTERS["total_orders_failed"] += 1
    if event_type == "risk_check_blocked" or ("risk" in event_type and "blocked" in event_type):
        _SUMMARY_COUNTERS["total_risk_blocks"] += 1
    reason = str(payload.get("reason", "")).upper()
    if (
        event_type in {"exchange_rule_validation_failed", "notional_too_small"}
        or "SYMBOL_RULES" in reason
        or "NOTIONAL" in reason
    ):
        _SUMMARY_COUNTERS["total_exchange_rule_blocks"] += 1
    if category == "market_data" and ("stale" in event_type or payload.get("stale") is True):
        _SUMMARY_COUNTERS["stale_market_data_count"] += 1
    if event_type == "cycle_finished" and payload.get("slow_cycle") is True:
        _SUMMARY_COUNTERS["slow_cycle_count"] += 1
    if event_type == "unhandled_exception":
        _SUMMARY_COUNTERS["unhandled_exception_count"] += 1


def _make_jsonable(value: Any) -> Any:
    """Converte tipos Python arbitrários para tipos JSON-serializáveis.

    Não aplica masking — isso é responsabilidade de sanitize_for_logging().
    """
    if isinstance(value, dict):
        return {str(key): _make_jsonable(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [_make_jsonable(item) for item in value]

    if isinstance(value, set):
        return sorted(
            (_make_jsonable(item) for item in value),
            key=str,
        )

    if is_dataclass(value):
        return _make_jsonable(asdict(value))  # type: ignore[arg-type]

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, BaseException):
        return _exception_payload(value)

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    from utils.privacy import to_safe_fallback

    return to_safe_fallback(value)


def _exception_payload(error: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": type(error).__name__,
        "message": str(error),
    }
    if _SETTINGS.full_traceback:
        payload["traceback"] = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
    return payload


def _lock_for_path(path: Path) -> threading.Lock:
    target = path.resolve()
    with _JSONL_LOCKS_GUARD:
        lock = _JSONL_LOCKS.get(target)
        if lock is None:
            lock = threading.Lock()
            _JSONL_LOCKS[target] = lock
        return lock


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _env_or_attr(config: Any | None, env_name: str, attr_name: str, default: str) -> str:
    raw = os.getenv(env_name)
    if raw is not None and raw.strip():
        return raw.strip()
    return str(getattr(config, attr_name, default) or default)


def _int_env_or_attr(
    config: Any | None,
    env_name: str,
    attr_name: str,
    default: int,
) -> int:
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return int(getattr(config, attr_name, default) or default)
    return int(raw)


def _bool_env_or_attr(
    config: Any | None,
    env_name: str,
    attr_name: str,
    default: bool,
) -> bool:
    raw = os.getenv(env_name)
    if raw is None or not raw.strip():
        return bool(getattr(config, attr_name, default))
    normalized = raw.strip().lower()
    if normalized in {"true", "1", "yes", "y", "sim"}:
        return True
    if normalized in {"false", "0", "no", "n", "nao"}:
        return False
    raise ValueError(f"{env_name} precisa ser true ou false")
