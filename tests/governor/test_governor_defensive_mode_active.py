from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, PositionDirection
from governor.cycle_context import CycleContext
from governor.defensive_mode import DefensiveModeEvaluation
from governor.services import DefensiveModeService
from scanner.signal_score import SignalPriority


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type, **data):
        self.events.append((event_type, data))


class FakeCoordinator:
    global_strategy_name = "test_strategy"


def _service(**config_overrides):
    config = {
        "governor_defensive_mode_enforcement_enabled": True,
        "governor_defensive_mode_block_new_entries": True,
    }
    config.update(config_overrides)

    return DefensiveModeService(
        config=SimpleNamespace(**config),
        journal=FakeJournal(),
        strategy=FakeCoordinator(),
    )


def _score(action="OPEN_LONG"):
    return SimpleNamespace(
        score=90,
        priority=SignalPriority.READY,
        action=action,
    )


def _position(*, open_position=False):
    if not open_position:
        return FuturesPosition(
            symbol="BTCUSDT",
            direction=PositionDirection.NONE,
            quantity=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
        )

    return FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.01"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )


def _evaluation(
    *,
    shadow_only=False,
    would_block_new_entries=True,
    mode="MARKET_SHOCK",
):
    return DefensiveModeEvaluation(
        mode=mode,
        reason="TEST_DEFENSIVE_MODE",
        severity="HIGH",
        shadow_only=shadow_only,
        would_block_new_entries=would_block_new_entries,
        would_reduce_new_entries=True,
        would_prioritize_protection=True,
        would_allow_closing=True,
        would_allow_stop_take_adjustment=True,
        diagnostics={"test": True},
    )


def test_defensive_mode_active_blocks_new_entry_when_enabled():
    service = _service()
    context = CycleContext(
        now=100.0,
        defensive_mode=_evaluation(shadow_only=False),
    )
    symbol_config = SimpleNamespace(symbol="BTCUSDT")

    decision = service.new_entry_block_decision(
        symbol_config=symbol_config,
        score=_score("OPEN_LONG"),
        position=_position(open_position=False),
        context=context,
    )

    assert decision is not None
    assert decision["status"] == "BLOCKED"
    assert decision["reason"] == "DEFENSIVE_MODE_MARKET_SHOCK"
    assert decision["execution_changed"] is True
    assert decision["order_mutation"] is False


def test_defensive_mode_does_not_block_while_shadow_only():
    service = _service()
    context = CycleContext(
        now=100.0,
        defensive_mode=_evaluation(shadow_only=True),
    )
    symbol_config = SimpleNamespace(symbol="BTCUSDT")

    decision = service.new_entry_block_decision(
        symbol_config=symbol_config,
        score=_score("OPEN_LONG"),
        position=_position(open_position=False),
        context=context,
    )

    assert decision is None


def test_defensive_mode_does_not_block_when_enforcement_disabled():
    service = _service(governor_defensive_mode_enforcement_enabled=False)
    context = CycleContext(
        now=100.0,
        defensive_mode=_evaluation(shadow_only=False),
    )
    symbol_config = SimpleNamespace(symbol="BTCUSDT")

    decision = service.new_entry_block_decision(
        symbol_config=symbol_config,
        score=_score("OPEN_LONG"),
        position=_position(open_position=False),
        context=context,
    )

    assert decision is None


def test_defensive_mode_does_not_block_open_position_management():
    service = _service()
    context = CycleContext(
        now=100.0,
        defensive_mode=_evaluation(shadow_only=False),
    )
    symbol_config = SimpleNamespace(symbol="BTCUSDT")

    decision = service.new_entry_block_decision(
        symbol_config=symbol_config,
        score=_score("OPEN_LONG"),
        position=_position(open_position=True),
        context=context,
    )

    assert decision is None


def test_defensive_mode_does_not_block_close_or_no_action():
    service = _service()
    context = CycleContext(
        now=100.0,
        defensive_mode=_evaluation(shadow_only=False),
    )
    symbol_config = SimpleNamespace(symbol="BTCUSDT")

    close_decision = service.new_entry_block_decision(
        symbol_config=symbol_config,
        score=_score("CLOSE_LONG"),
        position=_position(open_position=False),
        context=context,
    )
    no_action_decision = service.new_entry_block_decision(
        symbol_config=symbol_config,
        score=_score("NO_ACTION"),
        position=_position(open_position=False),
        context=context,
    )

    assert close_decision is None
    assert no_action_decision is None


def test_defensive_mode_active_block_records_audit_event():
    service = _service()
    symbol_config = SimpleNamespace(symbol="BTCUSDT")
    decision = {
        "status": "BLOCKED",
        "reason": "DEFENSIVE_MODE_MARKET_SHOCK",
        "mode": "MARKET_SHOCK",
        "mode_reason": "MULTI_SYMBOL_SHOCK_DETECTED",
        "severity": "HIGH",
        "action": "OPEN_LONG",
        "shadow_only": False,
        "execution_changed": True,
        "order_mutation": False,
        "diagnostics": {"test": True},
        "stage": "POST_HEAVY_INTENT",
    }

    service.record_active_block(
        symbol_config=symbol_config,
        score=_score("OPEN_LONG"),
        decision=decision,
    )

    assert service.journal.events

    event_type, data = service.journal.events[0]

    assert event_type == "trade_blocked_by_defensive_mode"
    assert data["reason"] == "DEFENSIVE_MODE_MARKET_SHOCK"
    assert data["stage"] == "POST_HEAVY_INTENT"
    assert data["execution_changed"] is True
    assert data["order_mutation"] is False


def test_defensive_mode_blocks_using_intent_action_when_score_action_is_empty():
    service = _service()
    context = CycleContext(
        now=100.0,
        defensive_mode=_evaluation(shadow_only=False),
    )
    symbol_config = SimpleNamespace(symbol="BTCUSDT")

    decision = service.new_entry_block_decision(
        symbol_config=symbol_config,
        score=_score(action=None),
        position=_position(open_position=False),
        context=context,
        action="OPEN_LONG",
        stage="POST_HEAVY_INTENT",
    )

    assert decision is not None
    assert decision["action"] == "OPEN_LONG"
    assert decision["stage"] == "POST_HEAVY_INTENT"
    assert decision["reason"] == "DEFENSIVE_MODE_MARKET_SHOCK"


def test_defensive_mode_registers_block_in_cycle_context():
    context = CycleContext(now=100.0)
    decision = {
        "symbol": "BTCUSDT",
        "reason": "DEFENSIVE_MODE_MARKET_SHOCK",
    }

    DefensiveModeService.register_block(
        context=context,
        decision=decision,
    )

    assert context.defensive_blocks == [decision]
