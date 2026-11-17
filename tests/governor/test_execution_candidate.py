"""Tests for ExecutionCandidate: immutability, revision tracking, and validation."""

from decimal import Decimal

import pytest

from domain.models import TradeAction
from governor.execution_candidate import ExecutionCandidate


def _open_long(**overrides) -> ExecutionCandidate:
    defaults = dict(
        intent_id="intent_1",
        trade_plan_id="plan_1",
        symbol="BTCUSDT",
        action=TradeAction.OPEN_LONG,
        quantity=Decimal("0.01"),
        entry_price=Decimal("50000"),
        stop_price=Decimal("49000"),
        take_profit_price=Decimal("52000"),
        planned_exit_source="TRADE_PLAN",
        notional=Decimal("500"),
        reason="READY",
    )
    defaults.update(overrides)
    return ExecutionCandidate.from_intent_and_plan(**defaults)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def test_valid_open_long_creates_candidate():
    c = _open_long()
    assert c.symbol == "BTCUSDT"
    assert c.action is TradeAction.OPEN_LONG
    assert c.revision == 0
    assert isinstance(c.candidate_id, str)
    assert c.candidate_id.startswith("cand_")
    assert c.notional == Decimal("500")


def test_symbol_is_normalised_to_upper():
    c = _open_long(symbol="btcusdt")
    assert c.symbol == "BTCUSDT"


def test_action_string_is_coerced_to_enum():
    c = _open_long(action="OPEN_LONG")
    assert c.action is TradeAction.OPEN_LONG


def test_metadata_is_immutable():
    c = _open_long(metadata={"key": "value"})
    with pytest.raises((TypeError, AttributeError)):
        c.metadata["new_key"] = "x"  # type: ignore[index]


def test_close_position_allows_none_quantity():
    c = ExecutionCandidate.from_intent_and_plan(
        intent_id="i1",
        trade_plan_id=None,
        symbol="ETHUSDT",
        action=TradeAction.CLOSE_POSITION,
        quantity=None,
        entry_price=Decimal("3000"),
        stop_price=None,
        take_profit_price=None,
        notional=Decimal("300"),
        reason="CLOSE",
    )
    assert c.quantity is None
    assert c.action is TradeAction.CLOSE_POSITION


# ---------------------------------------------------------------------------
# Validation — bad inputs are rejected
# ---------------------------------------------------------------------------


def test_zero_quantity_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        _open_long(quantity=Decimal("0"))


def test_negative_quantity_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        _open_long(quantity=Decimal("-1"))


def test_long_stop_above_entry_is_rejected():
    with pytest.raises(ValueError, match="stop"):
        _open_long(stop_price=Decimal("51000"))  # stop >= entry 50000


def test_long_take_below_entry_is_rejected():
    with pytest.raises(ValueError, match="take"):
        _open_long(take_profit_price=Decimal("49000"))  # take <= entry 50000


def test_short_stop_below_entry_is_rejected():
    c_kwargs = dict(
        action=TradeAction.OPEN_SHORT,
        entry_price=Decimal("50000"),
        stop_price=Decimal("49000"),  # short stop must be ABOVE entry
        take_profit_price=Decimal("48000"),
    )
    with pytest.raises(ValueError, match="stop"):
        _open_long(**c_kwargs)


def test_short_take_above_entry_is_rejected():
    c_kwargs = dict(
        action=TradeAction.OPEN_SHORT,
        entry_price=Decimal("50000"),
        stop_price=Decimal("51000"),
        take_profit_price=Decimal("52000"),  # short take must be BELOW entry
    )
    with pytest.raises(ValueError, match="take"):
        _open_long(**c_kwargs)


def test_zero_entry_price_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        _open_long(entry_price=Decimal("0"))


# ---------------------------------------------------------------------------
# revise()
# ---------------------------------------------------------------------------


def test_revise_increments_revision():
    c = _open_long()
    c2 = c.revise(quantity=Decimal("0.02"), reason="SMART_SIZING_APPLIED")
    assert c2.revision == 1
    assert c.revision == 0  # original unchanged


def test_revise_keeps_candidate_id():
    c = _open_long()
    c2 = c.revise(quantity=Decimal("0.005"), reason="REDUCE")
    assert c2.candidate_id == c.candidate_id


def test_revise_recalculates_notional():
    c = _open_long(quantity=Decimal("0.01"), entry_price=Decimal("50000"), notional=Decimal("500"))
    c2 = c.revise(quantity=Decimal("0.02"), reason="SIZE_UP")
    assert c2.notional == Decimal("0.02") * Decimal("50000")


def test_revise_original_instance_unchanged():
    c = _open_long()
    orig_qty = c.quantity
    _ = c.revise(quantity=Decimal("0.005"), reason="REDUCE")
    assert c.quantity == orig_qty  # original untouched


def test_revise_records_history_in_metadata():
    c = _open_long()
    c2 = c.revise(quantity=Decimal("0.005"), reason="LEARNING_APPLIED")
    history = c2.metadata.get("revision_history", [])
    assert len(history) == 1
    assert history[0]["from_revision"] == 0
    assert history[0]["reason"] == "LEARNING_APPLIED"


def test_chained_revisions_accumulate_history():
    c = _open_long()
    c2 = c.revise(quantity=Decimal("0.02"), reason="SMART_SIZING")
    c3 = c2.revise(stop_price=Decimal("48000"), reason="DYNAMIC_EXIT")
    assert c3.revision == 2
    history = c3.metadata["revision_history"]
    assert len(history) == 2


def test_revise_stop_price_without_changing_notional():
    c = _open_long()
    c2 = c.revise(stop_price=Decimal("48000"), reason="TIGHTER_STOP")
    assert c2.notional == c.notional
    assert c2.stop_price == Decimal("48000")


def test_revise_preserves_other_fields():
    c = _open_long()
    c2 = c.revise(quantity=Decimal("0.005"), reason="REDUCE")
    assert c2.symbol == c.symbol
    assert c2.intent_id == c.intent_id
    assert c2.trade_plan_id == c.trade_plan_id
    assert c2.action == c.action
