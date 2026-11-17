from decimal import Decimal

from domain.models import FuturesPosition, PositionDirection, TradeAction
from futures.protection import (
    audit_protections,
    build_expected_protection,
    build_expected_protection_for_entry,
)
from futures.risk_manager import FuturesRiskManager


def test_audit_protections_accepts_expected_stop_and_take(symbol_rules):
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.5"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )
    risk_manager = FuturesRiskManager(50, 0.01, 0.02)
    orders = [
        {
            "clientAlgoId": "ALPHORA_STOP_1",
            "orderType": "STOP_MARKET",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "closePosition": True,
            "workingType": "MARK_PRICE",
            "triggerPrice": "99.00",
            "algoStatus": "NEW",
        },
        {
            "clientAlgoId": "ALPHORA_TAKE_1",
            "orderType": "TAKE_PROFIT_MARKET",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "closePosition": True,
            "workingType": "MARK_PRICE",
            "triggerPrice": "102.00",
            "algoStatus": "NEW",
        },
    ]

    result = audit_protections(
        position=position,
        algo_orders=orders,
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="MARK_PRICE",
        symbol="BTCUSDT",
        robot_id_prefix="ALPHORA_",
    )

    assert result.ok is True
    assert result.missing == []
    assert result.invalid == []


def test_audit_protections_reports_missing_take(symbol_rules):
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.LONG,
        quantity=Decimal("0.5"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )
    risk_manager = FuturesRiskManager(50, 0.01, 0.02)

    result = audit_protections(
        position=position,
        algo_orders=[],
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="MARK_PRICE",
        symbol="BTCUSDT",
        robot_id_prefix="ALPHORA_",
    )

    assert result.ok is False
    assert result.missing == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]


def test_pre_entry_preview_matches_post_fill_static_protection(symbol_rules):
    risk_manager = FuturesRiskManager(50, 0.003, 0.004)
    preview = build_expected_protection_for_entry(
        action=TradeAction.OPEN_SHORT,
        entry_price=Decimal("100"),
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="MARK_PRICE",
    )
    position = FuturesPosition(
        symbol="BTCUSDT",
        direction=PositionDirection.SHORT,
        quantity=Decimal("0.5"),
        entry_price=Decimal("100"),
        unrealized_pnl=Decimal("0"),
    )
    post_fill = build_expected_protection(
        position=position,
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="MARK_PRICE",
    )

    assert preview == post_fill
    assert preview.stop_price == Decimal("100.30")
    assert preview.take_profit_price == Decimal("99.60")


def test_post_fill_revalidation_rebases_static_protection(symbol_rules):
    from futures.protection import revalidate_expected_protection_after_fill

    risk_manager = FuturesRiskManager(50, 0.003, 0.004)
    preview = build_expected_protection_for_entry(
        action=TradeAction.OPEN_LONG,
        entry_price=Decimal("100"),
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="CONTRACT_PRICE",
    )

    result = revalidate_expected_protection_after_fill(
        action=TradeAction.OPEN_LONG,
        planned_entry_price=Decimal("100"),
        actual_entry_price=Decimal("100.20"),
        trigger_price=Decimal("100.20"),
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="CONTRACT_PRICE",
        prevalidated=preview,
    )

    assert result.allowed is True
    assert result.adjusted is False
    assert result.expected is not None
    assert result.expected.stop_price == Decimal("99.80")
    assert result.expected.take_profit_price == Decimal("100.70")


def test_post_fill_revalidation_allows_small_bounded_stop_adjustment(symbol_rules):
    from futures.protection import revalidate_expected_protection_after_fill

    risk_manager = FuturesRiskManager(50, 0.01, 0.02)
    preview = build_expected_protection_for_entry(
        action=TradeAction.OPEN_SHORT,
        entry_price=Decimal("100"),
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="CONTRACT_PRICE",
    )

    result = revalidate_expected_protection_after_fill(
        action=TradeAction.OPEN_SHORT,
        planned_entry_price=Decimal("100"),
        actual_entry_price=Decimal("100"),
        trigger_price=Decimal("101.00"),
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="CONTRACT_PRICE",
        prevalidated=preview,
        max_risk_increase_pct=Decimal("10"),
        trigger_buffer_bps=Decimal("3"),
    )

    assert result.allowed is True
    assert result.adjusted is True
    assert result.expected is not None
    assert result.expected.stop_price == Decimal("101.10")
    assert result.risk_increase_pct == Decimal("10.0")


def test_post_fill_revalidation_rejects_large_adjustment(symbol_rules):
    from futures.protection import revalidate_expected_protection_after_fill

    risk_manager = FuturesRiskManager(50, 0.003, 0.004)
    preview = build_expected_protection_for_entry(
        action=TradeAction.OPEN_SHORT,
        entry_price=Decimal("100"),
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="CONTRACT_PRICE",
    )

    result = revalidate_expected_protection_after_fill(
        action=TradeAction.OPEN_SHORT,
        planned_entry_price=Decimal("100"),
        actual_entry_price=Decimal("100"),
        trigger_price=Decimal("101"),
        risk_manager=risk_manager,
        rules=symbol_rules,
        working_type="CONTRACT_PRICE",
        prevalidated=preview,
        max_risk_increase_pct=Decimal("10"),
        trigger_buffer_bps=Decimal("3"),
    )

    assert result.allowed is False
    assert result.reason == "POST_FILL_PROTECTION_ADJUSTMENT_EXCEEDS_LIMIT"
    assert result.expected is None
