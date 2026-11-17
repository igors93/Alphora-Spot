import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

from futures.risk_guard import FuturesRiskGuard
from governor.market_data_router import MarketDataRouter
from utils.audit_logger import (
    append_run_log,
    audit_event,
    cleanup_old_runs,
    finalize_run_audit,
    get_run_id,
    initialize_run_audit,
    sanitize_for_logging,
)
from utils.logger import get_logger


class SampleEnum(Enum):
    VALUE = "value"


class FakeMarketClient:
    def __init__(self):
        self.ticker_calls = 0

    def ticker_price(self, symbol, *, timeout=None):
        self.ticker_calls += 1
        return {"symbol": symbol, "price": "100"}

    def mark_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "markPrice": "100"}

    def exchange_info(self, *, timeout=None):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def position_risk(self, _symbol):
        return []

    def open_orders(self, _symbol):
        return []

    def open_algo_orders(self, _symbol):
        return []

    def account_info(self, *, timeout=None):
        return {"assets": []}


def _config_with_audit_dir(config, tmp_path, **kwargs):
    values = {
        "audit_runs_dir": str(tmp_path / "runs"),
        "audit_jsonl_enabled": True,
        "audit_text_logs_enabled": True,
    }
    values.update(kwargs)
    return replace(config, **values)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _flush_logging_handlers():
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_initialize_run_audit_creates_structure_and_manifest(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(dynamic_exit_enabled=True), tmp_path)

    run_dir = initialize_run_audit(config)

    assert run_dir is not None
    assert (run_dir / "logs").is_dir()
    assert (run_dir / "events").is_dir()
    assert (run_dir / "snapshots").is_dir()
    assert (run_dir / "reports").is_dir()
    assert (run_dir / "run_manifest.json").is_file()
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["operational_module_modes"]["dynamic_exit"] == "ACTIVE"
    events = _read_jsonl(run_dir / "events" / "system_events.jsonl")
    mode_event = next(
        event
        for event in events
        if event["event_type"] == "operational_module_modes_loaded"
        and event["payload"]["module"] == "dynamic_exit"
    )
    assert mode_event["payload"]["mode"] == "ACTIVE"
    assert mode_event["payload"]["can_affect_execution"] is True


def test_sanitize_for_logging_masks_sensitive_fields():
    payload = {
        "api_key": "abc",
        "api_secret": "def",
        "token": "ghi",
        "password": "jkl",
        "listen_key": "mno",
        "nested": {"private_key": "pqr"},
    }

    sanitized = sanitize_for_logging(payload)

    # logprivacy substitui valores de chaves sensíveis por placeholders
    assert sanitized["api_key"] != "abc"
    assert sanitized["api_secret"] != "def"
    assert sanitized["token"] != "ghi"
    assert sanitized["password"] != "jkl"
    assert sanitized["listen_key"] != "mno"
    assert sanitized["nested"]["private_key"] != "pqr"

    # Os valores mascarados devem ser strings não-vazias
    for key in ("api_key", "api_secret", "token", "password", "listen_key"):
        assert isinstance(sanitized[key], str) and sanitized[key]
    assert isinstance(sanitized["nested"]["private_key"], str)


def test_sanitize_for_logging_preserves_non_sensitive_fields():
    payload = {"symbol": "BTCUSDT", "score": 85, "price": 100.0}
    sanitized = sanitize_for_logging(payload)
    assert sanitized["symbol"] == "BTCUSDT"
    assert sanitized["score"] == 85
    assert sanitized["price"] == 100.0


def test_audit_event_writes_valid_jsonl(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(), tmp_path)
    run_dir = initialize_run_audit(config)

    audit_event("strategy", "strategy_score_evaluated", symbol="BTCUSDT", score=80)

    events = _read_jsonl(run_dir / "events" / "strategy_events.jsonl")
    assert any(event["event_type"] == "strategy_score_evaluated" for event in events)
    assert all(event["run_id"] == get_run_id() for event in events)


def test_audit_event_serializes_common_non_json_types(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(), tmp_path)
    run_dir = initialize_run_audit(config)

    audit_event(
        "system",
        "serialization_probe",
        decimal_value=Decimal("1.23"),
        enum_value=SampleEnum.VALUE,
        path_value=tmp_path / "file.txt",
        datetime_value=datetime(2026, 1, 1, tzinfo=timezone.utc),
        exception_value=ValueError("boom"),
    )

    events = _read_jsonl(run_dir / "events" / "system_events.jsonl")
    event = next(item for item in events if item["event_type"] == "serialization_probe")
    assert event["payload"]["decimal_value"] == "1.23"
    assert event["payload"]["enum_value"] == "value"
    assert event["payload"]["path_value"] == str(tmp_path / "file.txt")
    assert event["payload"]["datetime_value"].startswith("2026-01-01")
    assert event["payload"]["exception_value"]["type"] == "ValueError"


def test_text_logger_includes_run_id_and_splits_warning_error(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(), tmp_path)
    run_dir = initialize_run_audit(config)
    logger = get_logger("tests.audit_logger")

    logger.info("info probe")
    logger.warning("warning probe")
    logger.error("error probe")
    _flush_logging_handlers()

    alphora_log = (run_dir / "logs" / "alphora.log").read_text(encoding="utf-8")
    warnings_log = (run_dir / "logs" / "warnings.log").read_text(encoding="utf-8")
    errors_log = (run_dir / "logs" / "errors.log").read_text(encoding="utf-8")
    assert f"run_id={get_run_id()}" in alphora_log
    assert "warning probe" in warnings_log
    assert "error probe" in errors_log


def test_runtime_file_logging_reuses_existing_handlers(tmp_path, isolated_logging, monkeypatch):
    from utils import logger as logger_module

    run_dir = tmp_path / "run"
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True)
    warnings_path = (logs_dir / "warnings.log").resolve()
    existing_handler = logging.FileHandler(warnings_path, encoding="utf-8")
    logging.getLogger().addHandler(existing_handler)
    monkeypatch.setattr(logger_module, "_LOGGING_CONFIGURED", True)
    logger_module._RUN_LOG_HANDLER_PATHS.clear()

    logger_module.configure_runtime_file_logging(run_dir)

    warning_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename).resolve() == warnings_path
    ]
    assert len(warning_handlers) == 1


def test_config_snapshot_masks_secrets(config_factory, tmp_path):
    config = _config_with_audit_dir(
        replace(
            config_factory(),
            api_key="actual-api-key",
            api_secret="actual-api-secret",
            strategy_params={"token": "actual-token"},
        ),
        tmp_path,
    )
    run_dir = initialize_run_audit(config)

    snapshot_text = (run_dir / "snapshots" / "config_snapshot.json").read_text(encoding="utf-8")
    # Valores sensíveis não devem aparecer em texto puro no snapshot
    assert "actual-api-key" not in snapshot_text
    assert "actual-api-secret" not in snapshot_text
    assert "actual-token" not in snapshot_text
    # O JSON deve continuar válido após o masking
    json.loads(snapshot_text)


def test_run_manifest_does_not_store_secrets(config_factory, tmp_path):
    config = _config_with_audit_dir(
        replace(
            config_factory(),
            api_key="manifest-api-key",
            api_secret="manifest-api-secret",
        ),
        tmp_path,
    )
    run_dir = initialize_run_audit(config)

    manifest_text = (run_dir / "run_manifest.json").read_text(encoding="utf-8")
    assert "manifest-api-key" not in manifest_text
    assert "manifest-api-secret" not in manifest_text
    json.dumps(json.loads(manifest_text))


def test_risk_guard_audits_invalid_journal_lines(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(), tmp_path)
    journal_path = Path(config.trade_journal_path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text("{bad json\n", encoding="utf-8")
    run_dir = initialize_run_audit(config)

    FuturesRiskGuard(config).can_open_position(notional=Decimal("10"))

    events = _read_jsonl(run_dir / "events" / "risk_events.jsonl")
    assert any(
        event["event_type"] == "risk_guard_journal_invalid_lines_detected" for event in events
    )


def test_market_data_router_audits_stale_cached_snapshot(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(), tmp_path)
    run_dir = initialize_run_audit(config)
    router = MarketDataRouter(
        FakeMarketClient(),
        light_ttl_seconds=100.0,
        max_data_age_seconds=1.0,
        snapshot_enabled=False,
    )

    router.get_light_snapshot("BTCUSDT", now=0.0)
    snapshot = router.get_light_snapshot("BTCUSDT", now=2.0)

    assert snapshot.stale is True
    events = _read_jsonl(run_dir / "events" / "market_data_events.jsonl")
    assert any(event["event_type"] == "market_snapshot_stale" for event in events)


def test_audit_event_disabled_keeps_system_running(config_factory, tmp_path):
    config = _config_with_audit_dir(
        config_factory(),
        tmp_path,
        audit_jsonl_enabled=False,
    )
    run_dir = initialize_run_audit(config)

    result = audit_event("system", "disabled_probe")

    assert result is None
    assert not (run_dir / "events" / "system_events.jsonl").exists()


def test_fallback_to_logs_when_run_dir_cannot_be_created(config_factory, tmp_path):
    blocked = tmp_path / "blocked-runs"
    blocked.write_text("not a directory", encoding="utf-8")
    config = replace(config_factory(), audit_runs_dir=str(blocked))

    run_dir = initialize_run_audit(config)

    assert run_dir is not None
    assert run_dir.exists()
    assert "logs" in run_dir.parts


def test_retention_cleanup_never_deletes_current_run(tmp_path):
    runs_dir = tmp_path / "runs"
    current = runs_dir / "current"
    old = runs_dir / "old"
    current.mkdir(parents=True)
    old.mkdir()

    cleanup_old_runs(runs_dir, current_run_id="current", retention_days=0, max_runs=1)

    assert current.exists()
    assert not old.exists()


def test_run_summary_is_json_serializable(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(), tmp_path)
    run_dir = initialize_run_audit(config)

    summary_path = finalize_run_audit("COMPLETED")

    assert summary_path == run_dir / "reports" / "run_summary.json"
    json.dumps(json.loads(summary_path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# append_run_log — sanitização antes da escrita
# ---------------------------------------------------------------------------


def test_append_run_log_masks_token_in_line(config_factory, tmp_path):
    # Note: "aux" is a Windows reserved device name — use "taskout.log" instead.
    config = _config_with_audit_dir(config_factory(), tmp_path)
    run_dir = initialize_run_audit(config)

    append_run_log("taskout.log", "token=SENSITIVE_TEST_TOKEN_789 action=connect")

    log_file = run_dir / "logs" / "taskout.log"
    assert log_file.exists()
    text = log_file.read_text(encoding="utf-8")
    assert "SENSITIVE_TEST_TOKEN_789" not in text


def test_append_run_log_masks_api_key_in_line(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(), tmp_path)
    run_dir = initialize_run_audit(config)

    append_run_log("taskout.log", "api_key=SENSITIVE_TEST_API_KEY_123 status=ok")

    log_file = run_dir / "logs" / "taskout.log"
    assert log_file.exists()
    text = log_file.read_text(encoding="utf-8")
    assert "SENSITIVE_TEST_API_KEY_123" not in text


def test_append_run_log_preserves_normal_text(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(), tmp_path)
    run_dir = initialize_run_audit(config)

    append_run_log("taskout.log", "symbol=BTCUSDT side=BUY price=50000")

    log_file = run_dir / "logs" / "taskout.log"
    assert log_file.exists()
    text = log_file.read_text(encoding="utf-8")
    assert "symbol=BTCUSDT" in text
    assert "side=BUY" in text


def test_append_run_log_single_newline_per_entry(config_factory, tmp_path):
    config = _config_with_audit_dir(config_factory(), tmp_path)
    run_dir = initialize_run_audit(config)

    append_run_log("taskout.log", "line one")
    append_run_log("taskout.log", "line two")

    log_file = run_dir / "logs" / "taskout.log"
    assert log_file.exists()
    lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# sanitize_for_logging — objetos desconhecidos não usam str() arbitrário
# ---------------------------------------------------------------------------


def test_sanitize_unknown_object_does_not_call_str():
    class DangerousStr:
        def __str__(self) -> str:
            return "api_key=should-not-appear"

    result = sanitize_for_logging({"data": DangerousStr()})
    assert "should-not-appear" not in str(result)
