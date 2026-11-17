"""Tests for futures/planning/_math utilities and TradePlanBuilder defaults."""

from decimal import Decimal

from futures.planning._math import BPS, decimal_or_none, decimal_or_none_from_any
from futures.planning.trade_plan_builder import TradePlanBuilder


def test_bps_constant_is_ten_thousand():
    assert BPS == Decimal("10000")


def test_decimal_or_none_returns_string_for_decimal():
    assert decimal_or_none(Decimal("1.5")) == "1.5"


def test_decimal_or_none_returns_none_for_none():
    assert decimal_or_none(None) is None


def test_decimal_or_none_from_any_parses_string():
    assert decimal_or_none_from_any("3.14") == Decimal("3.14")


def test_decimal_or_none_from_any_returns_none_for_invalid():
    assert decimal_or_none_from_any("not-a-number") is None


def test_decimal_or_none_from_any_returns_none_for_none():
    assert decimal_or_none_from_any(None) is None


def test_trade_plan_builder_default_cost_model_is_not_from_env(monkeypatch):
    """TradePlanBuilder() must use fixed defaults, not environment variables."""
    monkeypatch.setenv("ALPHORA_TAKER_FEE_PCT", "0.9999")

    builder = TradePlanBuilder()

    assert builder.cost_model.taker_fee_pct != Decimal("0.9999"), (
        "TradePlanBuilder used CostModel.from_env() — environment variable leaked into default"
    )
    assert builder.cost_model.taker_fee_pct == Decimal("0.0004")
