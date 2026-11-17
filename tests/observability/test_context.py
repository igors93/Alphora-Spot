"""Tests for observability diagnostic context."""

from __future__ import annotations

import asyncio
import threading

import pytest

from observability.domain.context import (
    DiagnosticContext,
    bind_diagnostic_context,
    clear_diagnostic_context,
    diagnostic_context,
    get_diagnostic_context,
)


def test_basic_context_set_get():
    """Context variables can be set and retrieved."""
    with diagnostic_context(run_id="run-123", symbol="BTCUSDT"):
        ctx = get_diagnostic_context()
        assert ctx.run_id == "run-123"
        assert ctx.symbol == "BTCUSDT"


def test_context_restored_after_exit():
    """Context is restored to previous values after exiting the manager."""
    with diagnostic_context(run_id="outer"):
        with diagnostic_context(run_id="inner"):
            assert get_diagnostic_context().run_id == "inner"
        assert get_diagnostic_context().run_id == "outer"


def test_nested_context_restores_on_exception():
    """Context is restored even when an exception occurs inside the manager."""
    with diagnostic_context(run_id="outer"):
        with pytest.raises(ValueError):
            with diagnostic_context(run_id="inner"):
                raise ValueError("boom")
        assert get_diagnostic_context().run_id == "outer"


def test_context_cleared_on_exit():
    """After a top-level context exits, values return to None."""
    with diagnostic_context(cycle_id="c1"):
        pass
    assert get_diagnostic_context().cycle_id is None


def test_new_threads_do_not_inherit_context():
    """New threads start with empty context (ContextVar semantics)."""
    bind_diagnostic_context(run_id="main-thread-run")
    captured: list[str | None] = []

    def _target():
        captured.append(get_diagnostic_context().run_id)

    t = threading.Thread(target=_target)
    t.start()
    t.join()
    assert captured[0] is None
    clear_diagnostic_context()


def test_asyncio_tasks_inherit_context():
    """asyncio tasks inherit context from their creation point."""

    async def _inner():
        return get_diagnostic_context().symbol

    async def _outer():
        with diagnostic_context(symbol="ETHUSDT"):
            result = await asyncio.create_task(_inner())
        return result

    symbol = asyncio.run(_outer())
    assert symbol == "ETHUSDT"


def test_explicit_ids_in_context_manager():
    """All supported fields can be set via diagnostic_context."""
    with diagnostic_context(
        run_id="r1",
        cycle_id="c1",
        symbol="SOLUSDT",
        strategy="my_strategy",
        trade_plan_id="tp1",
        decision_id="d1",
        order_client_id="oc1",
        backtest_run_id="br1",
        request_id="req1",
    ):
        ctx = get_diagnostic_context()
        assert ctx.run_id == "r1"
        assert ctx.cycle_id == "c1"
        assert ctx.symbol == "SOLUSDT"
        assert ctx.strategy == "my_strategy"
        assert ctx.trade_plan_id == "tp1"
        assert ctx.decision_id == "d1"
        assert ctx.order_client_id == "oc1"
        assert ctx.backtest_run_id == "br1"
        assert ctx.request_id == "req1"


def test_empty_fields_omitted_from_dict():
    """as_dict() omits None fields."""
    with diagnostic_context(run_id="r1"):
        ctx = get_diagnostic_context()
        d = ctx.as_dict()
        assert "run_id" in d
        assert "symbol" not in d
        assert "cycle_id" not in d


def test_bind_diagnostic_context():
    """bind_diagnostic_context sets values without a context manager."""
    bind_diagnostic_context(request_id="req-42")
    assert get_diagnostic_context().request_id == "req-42"
    clear_diagnostic_context()
    assert get_diagnostic_context().request_id is None


def test_diagnostic_context_frozen():
    """DiagnosticContext is frozen and cannot be mutated."""
    ctx = DiagnosticContext(run_id="r1")
    with pytest.raises((AttributeError, TypeError)):
        ctx.run_id = "r2"  # type: ignore[misc]
