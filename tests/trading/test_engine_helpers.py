"""Unit tests for engine_position_mixin helpers and error constants.

Covers:
- _extract_average_order_price static method (P6)
- _realized_pnl_from_income_history filtering/summing (P7)
- ProtectionTriggerAlreadyBreached exception (P3)
- ORDER_NOT_FOUND_CODES constant (P4)
- _new_trade_id / _new_client_order_id 36-char limit (P8)
- getattr-free config field access (P2)
"""

from decimal import Decimal

from binance.futures_client import BinanceFuturesAPIError, BinanceFuturesNetworkError
from config import ExecutionMode
from execution.engine import FuturesTradingEngine
from execution.engine_position_mixin import EnginePositionMixin
from execution.errors import ORDER_NOT_FOUND_CODES, ProtectionTriggerAlreadyBreached
from futures.risk_manager import FuturesRiskManager
from tests.fakes import FakeFuturesClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BINANCE_CLIENT_ORDER_ID_MAX = 36


def make_engine(config, client, symbol_rules=None):
    engine = FuturesTradingEngine(
        config=config,
        client=client,
        risk_manager=FuturesRiskManager(
            position_notional_usdt=config.position_notional_usdt,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
        ),
    )
    if symbol_rules is not None:
        engine.rules = symbol_rules
    return engine


# ---------------------------------------------------------------------------
# P3 – ProtectionTriggerAlreadyBreached is a RuntimeError and importable
# ---------------------------------------------------------------------------


def test_protection_trigger_already_breached_is_runtime_error():
    exc = ProtectionTriggerAlreadyBreached("test breach")
    assert isinstance(exc, RuntimeError)
    assert "test breach" in str(exc)


def test_protection_trigger_already_breached_is_not_binance_api_error():
    exc = ProtectionTriggerAlreadyBreached("test")
    assert not isinstance(exc, BinanceFuturesAPIError)


# ---------------------------------------------------------------------------
# P4 – ORDER_NOT_FOUND_CODES constant covers -2011 and -2013
# ---------------------------------------------------------------------------


def test_order_not_found_codes_contains_expected_values():
    assert -2011 in ORDER_NOT_FOUND_CODES
    assert -2013 in ORDER_NOT_FOUND_CODES


def test_order_not_found_codes_is_frozenset():
    assert isinstance(ORDER_NOT_FOUND_CODES, frozenset)


def test_order_not_found_codes_does_not_include_other_fatal_codes():
    # These are real fatal codes that must NOT be silently treated as "order gone"
    for fatal in (-2015, -1121, -4061):
        assert fatal not in ORDER_NOT_FOUND_CODES


# ---------------------------------------------------------------------------
# P6 – _extract_average_order_price: all fallback paths
# ---------------------------------------------------------------------------


def test_extract_average_price_from_avg_price_key():
    order = {"avgPrice": "45000.5"}
    result = EnginePositionMixin._extract_average_order_price(order)
    assert result == Decimal("45000.5")


def test_extract_average_price_falls_back_to_average_price_key():
    order = {"averagePrice": "45001.0"}
    result = EnginePositionMixin._extract_average_order_price(order)
    assert result == Decimal("45001.0")


def test_extract_average_price_falls_back_to_avg_price_lower():
    order = {"avg_price": "45002.0"}
    result = EnginePositionMixin._extract_average_order_price(order)
    assert result == Decimal("45002.0")


def test_extract_average_price_falls_back_to_cum_quote_divided_by_executed():
    order = {"executedQty": "0.5", "cumQuote": "22500.0"}
    result = EnginePositionMixin._extract_average_order_price(order)
    assert result == Decimal("45000.0")


def test_extract_average_price_uses_cum_quote_qty_as_fallback_key():
    order = {"executedQty": "1.0", "cumQuoteQty": "46000.0"}
    result = EnginePositionMixin._extract_average_order_price(order)
    assert result == Decimal("46000.0")


def test_extract_average_price_skips_zero_avg_price():
    # avgPrice=0 is an invalid price and must be skipped
    order = {"avgPrice": "0", "cumQuote": "45000.0", "executedQty": "1.0"}
    result = EnginePositionMixin._extract_average_order_price(order)
    assert result == Decimal("45000.0")


def test_extract_average_price_returns_none_for_empty_dict():
    assert EnginePositionMixin._extract_average_order_price({}) is None


def test_extract_average_price_returns_none_for_none_input():
    assert EnginePositionMixin._extract_average_order_price(None) is None


def test_extract_average_price_returns_none_for_non_numeric():
    order = {"avgPrice": "NOT_A_NUMBER"}
    # non-numeric avgPrice falls through to cumQuote path; both absent → None
    assert EnginePositionMixin._extract_average_order_price(order) is None


def test_extract_average_price_returns_none_when_executed_qty_is_zero():
    order = {"executedQty": "0", "cumQuote": "45000.0"}
    assert EnginePositionMixin._extract_average_order_price(order) is None


# ---------------------------------------------------------------------------
# P7 – _realized_pnl_from_income_history: filtering and aggregation
# ---------------------------------------------------------------------------


def test_income_history_returns_none_when_empty(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.income_history_response = []
    engine = make_engine(config, client, symbol_rules)

    result = engine._realized_pnl_from_income_history(closed_at=1_000_000.0)
    assert result is None


def test_income_history_sums_multiple_rows(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.income_history_response = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "3.00"},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "1.50"},
    ]
    engine = make_engine(config, client, symbol_rules)

    result = engine._realized_pnl_from_income_history(closed_at=1_000_000.0)
    assert result == Decimal("4.50")


def test_income_history_filters_out_wrong_symbol(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.income_history_response = [
        {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "9.99"},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "2.00"},
    ]
    engine = make_engine(config, client, symbol_rules)

    result = engine._realized_pnl_from_income_history(closed_at=1_000_000.0)
    assert result == Decimal("2.00")


def test_income_history_filters_out_wrong_income_type(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.income_history_response = [
        {"symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "income": "99.0"},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "5.00"},
    ]
    engine = make_engine(config, client, symbol_rules)

    result = engine._realized_pnl_from_income_history(closed_at=1_000_000.0)
    assert result == Decimal("5.00")


def test_income_history_skips_rows_with_invalid_income_value(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.income_history_response = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "INVALID"},
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "3.00"},
    ]
    engine = make_engine(config, client, symbol_rules)

    result = engine._realized_pnl_from_income_history(closed_at=1_000_000.0)
    assert result == Decimal("3.00")


def test_income_history_returns_none_when_api_raises(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)

    class _ErrorClient(FakeFuturesClient):
        def income_history(self, **params):
            raise BinanceFuturesNetworkError("timeout")

    engine = make_engine(config, _ErrorClient(), symbol_rules)
    result = engine._realized_pnl_from_income_history(closed_at=1_000_000.0)
    assert result is None


def test_income_history_returns_none_when_all_rows_filtered(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.income_history_response = [
        {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "1.0"},
    ]
    engine = make_engine(config, client, symbol_rules)

    result = engine._realized_pnl_from_income_history(closed_at=1_000_000.0)
    assert result is None


def test_income_history_net_pnl_includes_commission_and_funding(config_factory, symbol_rules):
    config = config_factory(execution_mode=ExecutionMode.TESTNET_LIVE)
    client = FakeFuturesClient()
    client.income_history_response = [
        {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "5.00"},
        {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.04"},
        {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.06"},
        {"symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "income": "-0.05"},
    ]
    engine = make_engine(config, client, symbol_rules)

    result = engine._income_pnl_components_from_history(
        opened_at=999_000.0,
        closed_at=1_000_000.0,
    )

    assert result["realized_pnl"] == Decimal("5.00")
    assert result["commission"] == Decimal("-0.10")
    assert result["funding_fee"] == Decimal("-0.05")
    assert result["net_pnl"] == Decimal("4.85")
    assert result["pnl_components"]["rows"] == {
        "realized_pnl": 1,
        "commission": 2,
        "funding_fee": 1,
    }


# ---------------------------------------------------------------------------
# P8 – _new_trade_id and _new_client_order_id always stay within 36 chars
# ---------------------------------------------------------------------------


def test_new_trade_id_respects_36_char_limit(config_factory, symbol_rules):
    config = config_factory()
    engine = make_engine(config, FakeFuturesClient(), symbol_rules)

    for purpose in ("ENTRY", "CLOSE", "PROTECTED", "RECOVERY", "REPAIR", "STATE", "OPEN_LONG"):
        trade_id = engine._new_trade_id(purpose)
        assert len(trade_id) <= _BINANCE_CLIENT_ORDER_ID_MAX, (
            f"_new_trade_id('{purpose}') produced {len(trade_id)}-char ID: {trade_id!r}"
        )


def test_new_client_order_id_respects_36_char_limit(config_factory, symbol_rules):
    config = config_factory()
    engine = make_engine(config, FakeFuturesClient(), symbol_rules)

    for purpose in ("ENTRY", "CLOSE", "STOP", "TAKE", "ORDER", "ALGO"):
        order_id = engine._new_client_order_id(purpose)
        assert len(order_id) <= _BINANCE_CLIENT_ORDER_ID_MAX, (
            f"_new_client_order_id('{purpose}') produced {len(order_id)}-char ID: {order_id!r}"
        )


# ---------------------------------------------------------------------------
# P2 – post_fill_protection_revalidation_enabled and related fields are
#       accessed directly on config (not via getattr), so a misspelling
#       raises AttributeError immediately
# ---------------------------------------------------------------------------


def test_config_has_post_fill_revalidation_fields(config_factory):
    config = config_factory()
    # These must be real attributes on AppConfig, not dynamic getattr lookups.
    assert hasattr(config, "post_fill_protection_revalidation_enabled")
    assert hasattr(config, "post_fill_max_risk_increase_pct")
    assert hasattr(config, "protection_trigger_buffer_bps")
    # Verify they have expected types
    assert isinstance(config.post_fill_protection_revalidation_enabled, bool)
    assert isinstance(config.post_fill_max_risk_increase_pct, float)
    assert isinstance(config.protection_trigger_buffer_bps, float)
