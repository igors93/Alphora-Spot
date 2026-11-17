"""Regression tests for futures engine review cycle 1 corrections."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from binance.futures_client import BinanceFuturesAPIError
from config import ExecutionMode
from domain.models import FuturesPosition, PositionDirection, TradeAction
from execution.engine import FuturesTradingEngine
from execution.exchange_config import configure_margin_leverage_safely
from futures.risk_manager import FuturesRiskManager
from resilience.budgets import POSITION_CONFIRMATION_BUDGET
from resilience.catalog import (
    FLAT_CONFIRMATION_MAX_TIME_SECONDS,
    FLAT_CONFIRMATION_REQUEST_TIMEOUT,
    POSITION_CONFIRMATION_MAX_TIME_SECONDS,
    POSITION_CONFIRMATION_REQUEST_TIMEOUT,
)
from tests.fakes import FakeFuturesClient


def make_engine(config, client, symbol_rules=None):
    engine = FuturesTradingEngine(
        config=config,
        client=client,
        risk_manager=FuturesRiskManager(
            position_notional_usdt=config.position_notional_usdt,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
        ),
    )
    if symbol_rules is not None:
        engine.rules = symbol_rules
    return engine


class TimeoutRecordingClient(FakeFuturesClient):
    def __init__(self):
        super().__init__()
        self.position_risk_timeouts = []

    def position_risk(self, symbol, *, timeout=None):
        self.position_risk_timeouts.append(timeout)
        return super().position_risk(symbol, timeout=timeout)


# ---------------------------------------------------------------------------
# Problem 1: Redundant double mutations_enabled guard in _close_position
# The `if self.config.mutations_enabled:` wrapper around `_wait_until_flat`
# was redundant because the preceding `if not mutations_enabled: return`
# already guaranteed mutations are enabled at that point. After the fix,
# _wait_until_flat is called directly without the outer guard.
# ---------------------------------------------------------------------------


def test_close_position_calls_wait_until_flat_when_mutations_enabled(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.001")
    client.entry_price = Decimal("50000")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "T-1"

    wait_calls = []

    def fake_wait_until_flat():
        client.direction = PositionDirection.NONE
        client.quantity = Decimal("0")
        wait_calls.append(True)
        return True

    engine._wait_until_flat = fake_wait_until_flat
    engine._cancel_robot_open_orders = lambda **kw: None
    engine._safe_cancel_all_open_orders = lambda **kw: True
    engine._resolve_live_close_pnl = lambda **kw: {
        "net_pnl": None,
        "pnl_status": "unknown_live_close",
        "pnl_source": "unavailable",
    }
    engine._clear_runtime_state = lambda: None
    engine._record_operational_state = lambda *a, **kw: None
    engine.trade_audit_recorder.record_completed = lambda **kw: None

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.001"),
        entry_price=Decimal("50000"),
        unrealized_pnl=Decimal("0"),
    )

    engine._close_position(position, "test_close")

    assert len(wait_calls) == 1, "_wait_until_flat must be called exactly once"


def test_close_position_pauses_engine_when_wait_until_flat_fails(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.001")
    client.entry_price = Decimal("50000")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "T-2"

    engine._wait_until_flat = lambda: False
    engine._safe_cancel_all_open_orders = lambda **kw: True

    paused_with = {}

    def capture_pause(reason, **data):
        paused_with["reason"] = reason
        engine.paused = True

    engine._pause_engine = capture_pause
    engine._record_operational_state = lambda *a, **kw: None

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.001"),
        entry_price=Decimal("50000"),
        unrealized_pnl=Decimal("0"),
    )

    engine._close_position(position, "test_fail_close")

    assert paused_with.get("reason") == "position_close_not_confirmed"


def test_wait_for_position_after_entry_uses_budget_and_timeout(
    monkeypatch, config_factory, symbol_rules
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = TimeoutRecordingClient()
    client.direction = PositionDirection.LONG
    client.quantity = Decimal("0.001")
    client.entry_price = Decimal("50000")
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    captured = {}

    def fake_run_polling(operation, **kwargs):
        captured.update(kwargs)
        return operation()

    monkeypatch.setattr("execution.engine_position_mixin.run_polling", fake_run_polling)

    result = engine._wait_for_position_after_entry(action=TradeAction.OPEN_LONG)

    assert result is not None
    assert captured["budget"] is POSITION_CONFIRMATION_BUDGET
    assert captured["budget_key"] == "position-confirmation:BTCUSDT"
    assert captured["max_time_seconds"] == POSITION_CONFIRMATION_MAX_TIME_SECONDS
    assert client.position_risk_timeouts == [POSITION_CONFIRMATION_REQUEST_TIMEOUT]


def test_wait_until_flat_uses_budget_and_timeout(monkeypatch, config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = TimeoutRecordingClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    captured = {}

    def fake_run_polling(operation, **kwargs):
        captured.update(kwargs)
        return operation()

    monkeypatch.setattr("execution.engine_position_mixin.run_polling", fake_run_polling)

    assert engine._wait_until_flat() is True
    assert captured["budget"] is POSITION_CONFIRMATION_BUDGET
    assert captured["budget_key"] == "flat-confirmation:BTCUSDT"
    assert captured["max_time_seconds"] == FLAT_CONFIRMATION_MAX_TIME_SECONDS
    assert client.position_risk_timeouts == [FLAT_CONFIRMATION_REQUEST_TIMEOUT]


# ---------------------------------------------------------------------------
# Problem 2: _configure_exchange now delegates to configure_margin_leverage_safely
# The old mixin had divergent error handling (missing -4047, position messages).
# After the fix, _configure_exchange calls the shared function directly.
# ---------------------------------------------------------------------------


def test_configure_exchange_detects_open_state_error_code_4047(config_factory, symbol_rules):
    """configure_margin_leverage_safely must handle Binance error -4047 gracefully."""
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    make_engine(config, client, symbol_rules=symbol_rules)

    error_4047 = BinanceFuturesAPIError(
        code=-4047,
        message="No need to change margin type.",
        status_code=400,
    )
    client.change_margin_type = MagicMock(side_effect=error_4047)

    logger = MagicMock()
    events = []

    result = configure_margin_leverage_safely(
        client=client,
        symbol="BTCUSDT",
        margin_type="ISOLATED",
        leverage=10,
        logger=logger,
        record_event=lambda event_type, **kw: events.append(event_type),
    )

    assert result is False
    assert any("exchange_config_skipped" in e for e in events)


def test_configure_exchange_detects_open_state_from_position_message(config_factory, symbol_rules):
    """configure_margin_leverage_safely must handle Binance open-position message."""
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    make_engine(config, client, symbol_rules=symbol_rules)

    error_pos = BinanceFuturesAPIError(
        code=-4000,
        message="Cannot change margin type with open position.",
        status_code=400,
    )
    client.change_margin_type = MagicMock(side_effect=error_pos)

    logger = MagicMock()
    events = []

    result = configure_margin_leverage_safely(
        client=client,
        symbol="BTCUSDT",
        margin_type="ISOLATED",
        leverage=10,
        logger=logger,
        record_event=lambda event_type, **kw: events.append(event_type),
    )

    assert result is False
    assert any("exchange_config_skipped" in e for e in events)


def test_configure_exchange_uses_shared_function(config_factory, symbol_rules):
    """_configure_exchange must delegate to configure_margin_leverage_safely."""
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    called_with = {}

    with patch(
        "execution.engine_exchange_mixin.configure_margin_leverage_safely",
        side_effect=lambda **kw: called_with.update(kw) or True,
    ):
        engine._configure_exchange()

    assert called_with.get("symbol") == config.symbol
    assert called_with.get("margin_type") == config.margin_type.value
    assert called_with.get("leverage") == config.leverage


# ---------------------------------------------------------------------------
# Problem 3: entry_reason must not be hardcoded as empty string in record_completed
# Omitting entry_reason lets the audit recorder's _merge_lifecycle_context
# infer it from context (e.g. trade_plan_id, reason). Explicitly passing ""
# blocked that inference.
# ---------------------------------------------------------------------------


def test_close_position_does_not_pass_empty_entry_reason(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)
    engine.active_trade_id = "T-3"

    engine._wait_until_flat = lambda: True
    engine._safe_cancel_all_open_orders = lambda **kw: True
    engine._cancel_robot_open_orders = lambda **kw: None
    engine._resolve_live_close_pnl = lambda **kw: {
        "net_pnl": Decimal("5"),
        "pnl_status": "confirmed_income_history",
        "pnl_source": "binance_income_history",
    }
    engine._clear_runtime_state = lambda: None
    engine._record_operational_state = lambda *a, **kw: None

    recorded = {}

    def capture_completed(**kw):
        recorded.update(kw)

    engine.trade_audit_recorder.record_completed = capture_completed

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.001"),
        entry_price=Decimal("50000"),
        unrealized_pnl=Decimal("0"),
    )

    engine._close_position(position, "stop_triggered")

    assert "entry_reason" not in recorded or recorded.get("entry_reason") != ""


# ---------------------------------------------------------------------------
# Problem 4: _write_reconciliation_report must not swallow generic Exception
# After the fix, only OSError, ValueError, and TypeError are caught.
# An unexpected RuntimeError should propagate instead of being silently logged.
# ---------------------------------------------------------------------------


def test_write_reconciliation_report_propagates_unexpected_errors(
    config_factory, symbol_rules, tmp_path
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    report_path = tmp_path / "reports" / "reconciliation_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with patch("execution.engine_exchange_mixin.get_run_dir", return_value=tmp_path):
        with patch.object(
            Path,
            "write_text",
            side_effect=RuntimeError("unexpected disk error"),
        ):
            with pytest.raises(RuntimeError, match="unexpected disk error"):
                engine._write_reconciliation_report({"stage": "TEST"})


def test_write_reconciliation_report_handles_os_error_gracefully(
    config_factory, symbol_rules, tmp_path
):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    with patch("execution.engine_exchange_mixin.get_run_dir", return_value=tmp_path):
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            result = engine._write_reconciliation_report({"stage": "TEST"})

    assert result is None


# ---------------------------------------------------------------------------
# Problem 5: Log messages must use English
# Verify that log messages in engine components are in English.
# ---------------------------------------------------------------------------


def test_engine_error_mixin_pause_log_is_in_english(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.DRY_RUN)
    client = FakeFuturesClient()
    engine = make_engine(config, client, symbol_rules=symbol_rules)

    log_messages = []

    def capture_error(msg, *args, **kwargs):
        log_messages.append(msg % args if args else msg)

    engine.logger.error = capture_error
    engine._pause_engine("test_reason")

    assert any("paused" in m.lower() for m in log_messages), (
        f"Expected English 'paused' in engine log, got: {log_messages!r}"
    )
    assert not any("pausado" in m.lower() for m in log_messages), (
        "Portuguese 'pausado' must not appear in engine logs"
    )
