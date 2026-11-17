"""Fakes shared by the stage unit tests: no Governor is built here."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from domain.models import FuturesPosition, PositionDirection
from governor.symbol_evaluation_pipeline import SymbolEvaluationContext, SymbolEvaluationInput
from scanner.signal_score import SignalScore


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, event_type: str, **data) -> None:
        self.events.append((event_type, data))

    def types(self) -> list[str]:
        return [event_type for event_type, _ in self.events]


class TransitionRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, evaluation, **kwargs) -> None:
        self.calls.append(kwargs)


def make_position(*, open_position: bool = False) -> FuturesPosition:
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


def make_score(action: str = "OPEN_LONG", score: int = 90, reason: str = "signal") -> SignalScore:
    return SignalScore(symbol="BTCUSDT", score=score, action=action, reason=reason)


def make_evaluation(**fields) -> SymbolEvaluationContext:
    return SymbolEvaluationContext(
        evaluation_input=SymbolEvaluationInput(
            symbol_config=SimpleNamespace(symbol="BTCUSDT", bot_order_prefix="ALPHORA"),
            now=1_000.0,
            cycle_id="cycle-1",
            symbol_eval_id="eval-1",
        ),
        **fields,
    )
