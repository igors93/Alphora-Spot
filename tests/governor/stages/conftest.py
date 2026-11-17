"""Fixtures for the stage unit tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from governor.evaluation import StageOutcomes, SymbolRuntimeBoard
from tests.governor.stages.fakes import FakeJournal, TransitionRecorder


@pytest.fixture
def journal() -> FakeJournal:
    return FakeJournal()


@pytest.fixture
def snapshots() -> dict:
    return {}


@pytest.fixture
def outcomes(journal: FakeJournal, snapshots: dict) -> StageOutcomes:
    return StageOutcomes(
        journal=journal,
        trace_payload=lambda: {"cycle_id": "cycle-1", "symbol_eval_id": "eval-1"},
        board=SymbolRuntimeBoard(
            snapshots, strategy=SimpleNamespace(global_strategy_name="test_strategy")
        ),
    )


@pytest.fixture
def transitions() -> TransitionRecorder:
    return TransitionRecorder()
