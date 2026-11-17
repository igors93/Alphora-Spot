from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, PositionDirection
from governor.cycle_context import CycleContext
from governor.request_budget import RequestBudget
from governor.services import RequestBudgetService
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
        "governor_request_budget_enabled": True,
        "governor_request_budget_max_heavy_checks_per_cycle": 1,
        "governor_request_budget_open_position_override": True,
    }
    config.update(config_overrides)
    return RequestBudgetService(
        config=SimpleNamespace(**config),
        journal=FakeJournal(),
        strategy=FakeCoordinator(),
    )


def _score(*, priority=SignalPriority.HOT, action="OPEN_LONG"):
    return SimpleNamespace(
        score=80,
        priority=priority,
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


def test_governor_builds_request_budget_from_config():
    service = _service(
        governor_request_budget_max_heavy_checks_per_cycle=2,
        governor_request_budget_open_position_override=False,
        governor_request_budget_max_heavy_checks_by_category={"READY": 1},
        governor_request_budget_allow_borrowing=False,
    )

    budget = service.build_budget()

    assert budget is not None
    assert budget.max_heavy_checks_per_cycle == 2
    assert budget.open_position_override is False
    assert budget.max_heavy_checks_by_category == {"READY": 1}
    assert budget.allow_budget_borrowing is False


def test_governor_can_disable_request_budget():
    service = _service(governor_request_budget_enabled=False)

    assert service.build_budget() is None


def test_governor_records_skip_when_heavy_budget_is_exhausted():
    service = _service(governor_request_budget_max_heavy_checks_per_cycle=0)
    context = CycleContext(
        now=100.0,
        request_budget=RequestBudget(max_heavy_checks_per_cycle=0),
    )
    symbol_config = SimpleNamespace(symbol="BTCUSDT")

    decision = service.consume_heavy_check(
        symbol_config=symbol_config,
        score=_score(),
        position=_position(open_position=False),
        context=context,
    )

    assert decision is not None
    assert decision.allowed is False
    assert service.journal.events

    event_type, data = service.journal.events[0]
    assert event_type == "heavy_check_skipped_by_request_budget"
    assert data["reason"] == "HEAVY_CHECK_BUDGET_DISABLED_OR_ZERO"
    assert data["execution_changed"] is False
    assert data["order_mutation"] is False


def test_governor_request_budget_allows_open_position_override():
    service = _service(governor_request_budget_max_heavy_checks_per_cycle=0)
    context = CycleContext(
        now=100.0,
        request_budget=RequestBudget(
            max_heavy_checks_per_cycle=0,
            open_position_override=True,
        ),
    )
    symbol_config = SimpleNamespace(symbol="BTCUSDT")

    decision = service.consume_heavy_check(
        symbol_config=symbol_config,
        score=_score(priority=SignalPriority.COLD, action="NO_ACTION"),
        position=_position(open_position=True),
        context=context,
    )

    assert decision is not None
    assert decision.allowed is True
    assert decision.override is True
    assert service.journal.events == []
