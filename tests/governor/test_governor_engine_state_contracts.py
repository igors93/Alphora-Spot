"""Regression tests for ExecutionEngine and EngineStateMixin behavioural contracts.

Covers:
- P1: dead aliases recover_symbol_on_boot / configure_symbol_exchange removed
- P2: _current_price logs a warning and falls back instead of swallowing Exception silently
- P3: _write_manual_reconcile_required narrows exception to known API/network types
- P4: _record_journal logs event_type and error instead of silently swallowing
- P5: _runtime_state_data no longer contains None-identity expressions
- P6: record_open / record_protected do not pass duplicate trade_id kwarg
- P7: missing paper rules returns explicit failed execution result
- P8: _protection_state_payload docstring is in English
"""

import logging
from decimal import Decimal
from unittest.mock import patch

import pytest

from binance.futures_client import BinanceFuturesAPIError, BinanceFuturesNetworkError
from config import ExecutionMode
from config.symbol_context import symbol_config_from_app_config
from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeAction
from futures.position_execution import PositionExecutionStatus
from futures.state import RuntimeState
from governor.execution_engine import ExecutionEngine
from governor.market_data_router import HeavyMarketSnapshot

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class FakeClient:
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

    def change_margin_type(self, *, symbol, margin_type):
        return {"symbol": symbol, "marginType": margin_type}

    def change_leverage(self, *, symbol, leverage):
        return {"symbol": symbol, "leverage": leverage}

    def ticker_price(self, symbol, *, timeout=None):
        return {"symbol": symbol, "price": "100"}


def _heavy_snapshot():
    from futures.exchange_rules import extract_symbol_rules

    client = FakeClient()
    return HeavyMarketSnapshot(
        symbol="BTCUSDT",
        market=MarketSnapshot(
            symbol="BTCUSDT",
            price=Decimal("100"),
            prices=[Decimal("100")],
        ),
        mark_price=Decimal("100"),
        exchange_rules=extract_symbol_rules(client.exchange_info(), "BTCUSDT"),
        position=FuturesPosition(
            symbol="BTCUSDT",
            direction=PositionDirection.NONE,
            quantity=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        ),
        open_orders=[],
        open_algo_orders=[],
        account_info={},
        timestamp=1,
        data_age_seconds=0,
        stale=False,
    )


# ---------------------------------------------------------------------------
# P1 — Dead aliases are gone
# ---------------------------------------------------------------------------


def test_recover_symbol_on_boot_alias_removed(config_factory):
    """recover_symbol_on_boot must not exist; it was an untested, dead delegate."""
    config = config_factory()
    execution = ExecutionEngine(base_config=config, client=FakeClient())
    assert not hasattr(execution, "recover_symbol_on_boot")


def test_configure_symbol_exchange_alias_removed(config_factory):
    """configure_symbol_exchange must not exist; configure_symbol_exchange_safely is the API."""
    config = config_factory()
    execution = ExecutionEngine(base_config=config, client=FakeClient())
    assert not hasattr(execution, "configure_symbol_exchange")


# ---------------------------------------------------------------------------
# P2 — _current_price narrows and logs instead of swallowing silently
# ---------------------------------------------------------------------------


def test_current_price_falls_back_to_entry_price_on_api_error(config_factory, caplog):
    config = config_factory(execution_mode=ExecutionMode.PAPER_LIVE)
    profile = symbol_config_from_app_config(config)
    execution = ExecutionEngine(base_config=config, client=FakeClient())
    engine = execution._get_engine(profile)
    execution._ensure_symbol_rules(engine, profile)

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.001"),
        entry_price=Decimal("99"),
        unrealized_pnl=Decimal("0"),
    )

    with (
        patch.object(
            engine,
            "get_current_price",
            side_effect=BinanceFuturesAPIError(400, -1000, "bad request"),
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = execution._current_price(engine, position)

    assert result == Decimal("99")
    assert any("falling back to entry_price" in record.message for record in caplog.records)


def test_current_price_falls_back_on_network_error(config_factory):
    config = config_factory(execution_mode=ExecutionMode.PAPER_LIVE)
    profile = symbol_config_from_app_config(config)
    execution = ExecutionEngine(base_config=config, client=FakeClient())
    engine = execution._get_engine(profile)
    execution._ensure_symbol_rules(engine, profile)

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.001"),
        entry_price=Decimal("55"),
        unrealized_pnl=Decimal("0"),
    )

    with patch.object(
        engine,
        "get_current_price",
        side_effect=BinanceFuturesNetworkError("timeout"),
    ):
        result = execution._current_price(engine, position)

    assert result == Decimal("55")


def test_current_price_propagates_unexpected_errors(config_factory):
    """Errors outside the narrowed tuple must propagate, not be swallowed."""
    config = config_factory(execution_mode=ExecutionMode.PAPER_LIVE)
    profile = symbol_config_from_app_config(config)
    execution = ExecutionEngine(base_config=config, client=FakeClient())
    engine = execution._get_engine(profile)
    execution._ensure_symbol_rules(engine, profile)

    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.001"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )

    with patch.object(engine, "get_current_price", side_effect=RuntimeError("unexpected")):
        with pytest.raises(RuntimeError, match="unexpected"):
            execution._current_price(engine, position)


# ---------------------------------------------------------------------------
# P3 — _write_manual_reconcile_required narrows exception scope
# ---------------------------------------------------------------------------


def _make_engine_state_mixin_instance(config_factory):
    """Return a minimal object that exercises EngineStateMixin._write_manual_reconcile_required."""
    config = config_factory()
    profile = symbol_config_from_app_config(config)
    execution = ExecutionEngine(base_config=config, client=FakeClient())
    engine = execution._get_engine(profile)
    execution._ensure_symbol_rules(engine, profile)
    return engine


def test_write_manual_reconcile_handles_api_error_gracefully(config_factory, tmp_path):
    engine = _make_engine_state_mixin_instance(config_factory)

    with patch.object(
        engine,
        "_get_position",
        side_effect=BinanceFuturesAPIError(500, -1001, "server error"),
    ):
        path = engine._write_manual_reconcile_required("test_reason", {"key": "val"})

    assert path.exists()
    import json

    data = json.loads(path.read_text())
    assert "error" in data.get("last_known_position", {})


def test_write_manual_reconcile_propagates_unexpected_errors(config_factory):
    engine = _make_engine_state_mixin_instance(config_factory)

    with patch.object(
        engine,
        "_get_position",
        side_effect=RuntimeError("should not be swallowed"),
    ):
        with pytest.raises(RuntimeError, match="should not be swallowed"):
            engine._write_manual_reconcile_required("test_reason", {})


# ---------------------------------------------------------------------------
# P4 — _record_journal logs event_type and error in English
# ---------------------------------------------------------------------------


def test_record_journal_logs_event_and_error_on_os_error(config_factory, caplog):
    engine = _make_engine_state_mixin_instance(config_factory)

    with patch.object(engine.journal, "append", side_effect=OSError("disk full")):
        with caplog.at_level(logging.WARNING):
            engine._record_journal("test_event", key="value")

    messages = [r.message for r in caplog.records]
    assert any("test_event" in m for m in messages)
    assert any("disk full" in m for m in messages)


def test_record_journal_propagates_unexpected_errors(config_factory):
    engine = _make_engine_state_mixin_instance(config_factory)

    with patch.object(engine.journal, "append", side_effect=AttributeError("bad attr")):
        with pytest.raises(AttributeError, match="bad attr"):
            engine._record_journal("test_event")


# ---------------------------------------------------------------------------
# P5 — _runtime_state_data returns plain attribute values (no redundant identity)
# ---------------------------------------------------------------------------


def test_runtime_state_data_returns_none_fields_directly(config_factory):
    engine = _make_engine_state_mixin_instance(config_factory)

    engine.runtime_state = RuntimeState(
        active_trade_id="TRADE-X",
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        opened_at=1234.0,
        entry_price=Decimal("100"),
        quantity=Decimal("0.001"),
    )

    data = engine._runtime_state_data()

    assert data is not None
    assert data["planned_stop_price"] is None
    assert data["planned_take_profit_price"] is None
    assert data["trade_plan_id"] is None
    assert data["active_trade_id"] == "TRADE-X"


def test_runtime_state_data_returns_none_when_no_state(config_factory):
    engine = _make_engine_state_mixin_instance(config_factory)
    engine.runtime_state = None

    assert engine._runtime_state_data() is None


# ---------------------------------------------------------------------------
# P6 — record_open / record_protected do not receive duplicate trade_id kwarg
# ---------------------------------------------------------------------------


def test_paper_open_does_not_pass_duplicate_trade_id(config_factory):
    """record_open and record_protected must not receive trade_id alongside trade_plan_id."""
    config = config_factory(execution_mode=ExecutionMode.PAPER_LIVE)
    profile = symbol_config_from_app_config(config)
    execution = ExecutionEngine(base_config=config, client=FakeClient())

    record_open_calls = []
    record_protected_calls = []

    original_open = execution.trade_audit_recorder.record_open
    original_protected = execution.trade_audit_recorder.record_protected

    def capturing_record_open(**kwargs):
        record_open_calls.append(kwargs)
        return original_open(**kwargs)

    def capturing_record_protected(**kwargs):
        record_protected_calls.append(kwargs)
        return original_protected(**kwargs)

    execution.trade_audit_recorder.record_open = capturing_record_open
    execution.trade_audit_recorder.record_protected = capturing_record_protected

    heavy = _heavy_snapshot()
    execution.open_position(
        symbol_config=profile,
        action=TradeAction.OPEN_LONG,
        price=Decimal("100"),
        reason="test",
        heavy_snapshot=heavy,
        trade_plan_id="tp-001",
    )

    assert record_open_calls, "record_open was not called"
    assert "trade_id" not in record_open_calls[0], (
        "trade_id kwarg must not be passed to record_open — it duplicates trade_plan_id"
    )

    assert record_protected_calls, "record_protected was not called"
    assert "trade_id" not in record_protected_calls[0], (
        "trade_id kwarg must not be passed to record_protected — it duplicates trade_plan_id"
    )


# ---------------------------------------------------------------------------
# P7 — missing paper rules returns explicit failed execution result
# ---------------------------------------------------------------------------


def test_paper_open_position_returns_failed_result_when_rules_missing(config_factory):
    config = config_factory(execution_mode=ExecutionMode.PAPER_LIVE)
    profile = symbol_config_from_app_config(config)
    execution = ExecutionEngine(base_config=config, client=FakeClient())
    engine = execution._get_engine(profile)
    engine.rules = None  # simulate missing rules

    result = execution._paper_open_position(
        engine,
        action=TradeAction.OPEN_LONG,
        price=Decimal("100"),
        reason="test",
    )

    assert result.status is PositionExecutionStatus.FAILED
    assert result.position_changed is False
    assert result.reason == "SYMBOL_RULES_NOT_LOADED"


# ---------------------------------------------------------------------------
# P8 — _protection_state_payload docstring is in English
# ---------------------------------------------------------------------------


def test_protection_state_payload_docstring_is_in_english(config_factory):
    from execution.engine_state_mixin import EngineStateMixin

    doc = EngineStateMixin._protection_state_payload.__doc__ or ""
    portuguese_words = ["Preservar", "Protecoes", "plano", "adaptativa", "perdida"]
    for word in portuguese_words:
        assert word not in doc, f"Portuguese word {word!r} found in docstring"

    assert len(doc.strip()) > 0, "_protection_state_payload must have a docstring"
