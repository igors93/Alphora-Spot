"""Tests for ExecutionProbeStrategy.

Verifica:
- Primeiro sinal imediato (sem espera inicial)
- Sequência determinística: flat→OPEN_LONG, LONG→CLOSE, flat→OPEN_SHORT, SHORT→CLOSE
- Intervalo configurável respeitado
- Sem aleatoriedade
- Metadados com sequência e ação esperada
- decide_symbol delega corretamente ao last_score_action
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from domain.models import FuturesPosition, MarketSnapshot, PositionDirection, TradeAction
from strategies.futures.fixed.execution_probe import ExecutionProbeStrategy


def _market(now: float | None = None) -> MarketSnapshot:
    indicators: dict[str, Any] = {}
    if now is not None:
        indicators["timestamp"] = now
    return MarketSnapshot(
        symbol="BTCUSDT",
        price=Decimal("50000"),
        prices=[Decimal("50000")],
        indicators=indicators,
    )


def _flat(symbol: str = "BTCUSDT") -> FuturesPosition:
    return FuturesPosition(
        symbol=symbol,
        direction=PositionDirection.NONE,
        quantity=Decimal("0"),
        entry_price=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


def _long(symbol: str = "BTCUSDT") -> FuturesPosition:
    return FuturesPosition(
        symbol=symbol,
        direction=PositionDirection.LONG,
        quantity=Decimal("0.001"),
        entry_price=Decimal("50000"),
        unrealized_pnl=Decimal("0"),
    )


def _short(symbol: str = "BTCUSDT") -> FuturesPosition:
    return FuturesPosition(
        symbol=symbol,
        direction=PositionDirection.SHORT,
        quantity=Decimal("0.001"),
        entry_price=Decimal("50000"),
        unrealized_pnl=Decimal("0"),
    )


# ---------------------------------------------------------------------------
# Construção
# ---------------------------------------------------------------------------


def test_instantiation_with_defaults():
    strategy = ExecutionProbeStrategy()
    assert strategy.signal_interval_seconds == 120.0
    assert strategy.ready_score == 95
    assert strategy.idle_score == 20


def test_instantiation_with_custom_params():
    strategy = ExecutionProbeStrategy(
        signal_interval_seconds=60.0,
        ready_score=92,
        idle_score=10,
    )
    assert strategy.signal_interval_seconds == 60.0
    assert strategy.ready_score == 92
    assert strategy.idle_score == 10


def test_invalid_interval_raises():
    with pytest.raises(ValueError, match="signal_interval_seconds"):
        ExecutionProbeStrategy(signal_interval_seconds=0.0)


def test_from_params():
    strategy = ExecutionProbeStrategy.from_params({"signal_interval_seconds": "60"})
    assert strategy.signal_interval_seconds == 60.0


# ---------------------------------------------------------------------------
# Primeiro sinal: imediato
# ---------------------------------------------------------------------------


def test_first_signal_is_immediate():
    strategy = ExecutionProbeStrategy(signal_interval_seconds=120.0)
    state: dict[str, Any] = {}
    now = 1_000.0

    score = strategy.score_symbol("BTCUSDT", _market(now), _flat(), state)

    assert score.score == strategy.ready_score
    assert score.action == "OPEN_LONG", "Primeiro sinal deve ser OPEN_LONG quando flat"
    assert state["probe_sequence"] == 1
    assert state["probe_last_signal_at"] == now


# ---------------------------------------------------------------------------
# Sequência determinística
# ---------------------------------------------------------------------------


def test_deterministic_sequence_flat_long_flat_short():
    """flat→OPEN_LONG, LONG→CLOSE, flat→OPEN_SHORT, SHORT→CLOSE."""
    strategy = ExecutionProbeStrategy(signal_interval_seconds=120.0)
    state: dict[str, Any] = {}
    t = 1_000.0

    # 1) flat → OPEN_LONG
    s1 = strategy.score_symbol("BTCUSDT", _market(t), _flat(), state)
    assert s1.action == "OPEN_LONG"
    assert state["probe_sequence"] == 1
    assert state["probe_next_flat_action"] == "OPEN_SHORT"

    # 2) LONG → CLOSE_POSITION (após intervalo)
    t += 120.0
    s2 = strategy.score_symbol("BTCUSDT", _market(t), _long(), state)
    assert s2.action == "CLOSE_POSITION"
    assert state["probe_sequence"] == 2

    # 3) flat → OPEN_SHORT
    t += 120.0
    s3 = strategy.score_symbol("BTCUSDT", _market(t), _flat(), state)
    assert s3.action == "OPEN_SHORT"
    assert state["probe_sequence"] == 3
    assert state["probe_next_flat_action"] == "OPEN_LONG"

    # 4) SHORT → CLOSE_POSITION
    t += 120.0
    s4 = strategy.score_symbol("BTCUSDT", _market(t), _short(), state)
    assert s4.action == "CLOSE_POSITION"
    assert state["probe_sequence"] == 4

    # 5) ciclo reinicia: flat → OPEN_LONG
    t += 120.0
    s5 = strategy.score_symbol("BTCUSDT", _market(t), _flat(), state)
    assert s5.action == "OPEN_LONG"
    assert state["probe_sequence"] == 5


# ---------------------------------------------------------------------------
# Intervalo respeitado
# ---------------------------------------------------------------------------


def test_idle_score_when_interval_not_elapsed():
    strategy = ExecutionProbeStrategy(signal_interval_seconds=120.0)
    state: dict[str, Any] = {}
    t = 1_000.0

    strategy.score_symbol("BTCUSDT", _market(t), _flat(), state)
    t += 60.0  # apenas metade do intervalo

    idle = strategy.score_symbol("BTCUSDT", _market(t), _flat(), state)

    assert idle.score == strategy.idle_score
    assert idle.action == "NO_ACTION"
    assert idle.metadata["seconds_until_next_signal"] == pytest.approx(60.0, abs=0.1)


def test_ready_after_full_interval():
    strategy = ExecutionProbeStrategy(signal_interval_seconds=120.0)
    state: dict[str, Any] = {}
    t = 1_000.0

    strategy.score_symbol("BTCUSDT", _market(t), _flat(), state)
    t += 120.0

    ready = strategy.score_symbol("BTCUSDT", _market(t), _flat(), state)

    assert ready.score == strategy.ready_score
    assert ready.action in {"OPEN_LONG", "OPEN_SHORT", "CLOSE_POSITION"}


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_ready_score_contains_sequence_and_action_metadata():
    strategy = ExecutionProbeStrategy()
    state: dict[str, Any] = {}

    score = strategy.score_symbol("BTCUSDT", _market(1_000.0), _flat(), state)

    assert score.metadata["probe_sequence"] == 1
    assert score.metadata["probe_action"] == "OPEN_LONG"
    assert score.metadata["source"] == "FIXED_EXECUTION_PROBE"


def test_idle_score_contains_sequence_metadata():
    strategy = ExecutionProbeStrategy()
    state: dict[str, Any] = {}
    t = 1_000.0

    strategy.score_symbol("BTCUSDT", _market(t), _flat(), state)
    idle = strategy.score_symbol("BTCUSDT", _market(t + 10), _flat(), state)

    assert idle.metadata["probe_sequence"] == 1
    assert "next_flat_action" in idle.metadata


# ---------------------------------------------------------------------------
# decide_symbol
# ---------------------------------------------------------------------------


def test_decide_open_long_when_flat():
    strategy = ExecutionProbeStrategy()
    state: dict[str, Any] = {}
    state["last_score_action"] = "OPEN_LONG"
    state["last_score_reason"] = "probe"

    decision = strategy.decide_symbol("BTCUSDT", _market(), _flat(), state)

    assert decision.action is TradeAction.OPEN_LONG


def test_decide_open_short_when_flat():
    strategy = ExecutionProbeStrategy()
    state: dict[str, Any] = {}
    state["last_score_action"] = "OPEN_SHORT"
    state["last_score_reason"] = "probe"

    decision = strategy.decide_symbol("BTCUSDT", _market(), _flat(), state)

    assert decision.action is TradeAction.OPEN_SHORT


def test_decide_close_when_long():
    strategy = ExecutionProbeStrategy()
    state: dict[str, Any] = {}
    state["last_score_action"] = "CLOSE_POSITION"
    state["last_score_reason"] = "probe"

    decision = strategy.decide_symbol("BTCUSDT", _market(), _long(), state)

    assert decision.action is TradeAction.CLOSE_POSITION


def test_decide_hold_when_action_is_open_long_but_already_long():
    """Se last_score_action=OPEN_LONG mas já tem LONG → hold (proteção contra dupla entrada)."""
    strategy = ExecutionProbeStrategy()
    state: dict[str, Any] = {}
    state["last_score_action"] = "OPEN_LONG"
    state["last_score_reason"] = "probe"

    decision = strategy.decide_symbol("BTCUSDT", _market(), _long(), state)

    assert decision.action is TradeAction.HOLD


def test_decide_hold_when_close_but_flat():
    """Se last_score_action=CLOSE_POSITION mas já está flat → hold."""
    strategy = ExecutionProbeStrategy()
    state: dict[str, Any] = {}
    state["last_score_action"] = "CLOSE_POSITION"
    state["last_score_reason"] = "probe"

    decision = strategy.decide_symbol("BTCUSDT", _market(), _flat(), state)

    assert decision.action is TradeAction.HOLD


# ---------------------------------------------------------------------------
# Sem aleatoriedade
# ---------------------------------------------------------------------------


def test_no_randomness_across_instances():
    """Duas instâncias com mesmo estado produzem idêntica sequência."""
    strategy_a = ExecutionProbeStrategy()
    strategy_b = ExecutionProbeStrategy()
    state_a: dict[str, Any] = {}
    state_b: dict[str, Any] = {}
    t = 2_000.0

    for _ in range(4):
        t += 120.0
        pos_a, pos_b = _flat(), _flat()
        sa = strategy_a.score_symbol("BTCUSDT", _market(t), pos_a, state_a)
        sb = strategy_b.score_symbol("BTCUSDT", _market(t), pos_b, state_b)
        assert sa.action == sb.action, (
            f"Instâncias diferentes divergiram: {sa.action} vs {sb.action}"
        )
