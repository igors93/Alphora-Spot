from __future__ import annotations

from decimal import Decimal

import pytest

from domain.candle import Candle
from market_data.candle_validation import (
    CandleValidationError,
    is_valid_candle,
    validate_candle,
)

_BASE_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z — aligned to all timeframes
_1M_CLOSE = _BASE_MS + 60_000 - 1
_FAR_FUTURE = _BASE_MS + 10 * 365 * 24 * 3_600_000  # ~10 years ahead


def _candle(**overrides) -> Candle:
    defaults = dict(
        symbol="BTCUSDT",
        interval="1m",
        open_time_ms=_BASE_MS,
        close_time_ms=_1M_CLOSE,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("95"),
        close=Decimal("102"),
        volume=Decimal("10"),
        is_closed=True,
        source="test",
    )
    defaults.update(overrides)
    return Candle(**defaults)


class TestValidateCandle:
    def test_valid_candle_passes(self):
        validate_candle(_candle(), now_ms=_BASE_MS + 120_000)

    def test_empty_symbol_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(_candle(symbol=""), now_ms=_BASE_MS + 120_000)

    def test_empty_interval_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(_candle(interval=""), now_ms=_BASE_MS + 120_000)

    def test_unsupported_interval_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_TIMEFRAME_MISMATCH"):
            validate_candle(_candle(interval="2m"), now_ms=_BASE_MS + 120_000)

    def test_open_time_zero_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(_candle(open_time_ms=0), now_ms=_BASE_MS + 120_000)

    def test_close_before_open_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(
                _candle(open_time_ms=_BASE_MS, close_time_ms=_BASE_MS - 1),
                now_ms=_BASE_MS + 120_000,
            )

    def test_timestamp_in_future_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_TIMESTAMP_IN_FUTURE"):
            validate_candle(
                _candle(open_time_ms=_FAR_FUTURE, close_time_ms=_FAR_FUTURE + 60_000 - 1),
                now_ms=_BASE_MS,
            )

    def test_negative_open_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(_candle(open=Decimal("-1")), now_ms=_BASE_MS + 120_000)

    def test_zero_open_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(_candle(open=Decimal("0")), now_ms=_BASE_MS + 120_000)

    def test_negative_volume_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(_candle(volume=Decimal("-1")), now_ms=_BASE_MS + 120_000)

    def test_high_less_than_low_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(
                _candle(high=Decimal("90"), low=Decimal("95")),
                now_ms=_BASE_MS + 120_000,
            )

    def test_high_less_than_open_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(
                _candle(open=Decimal("110"), high=Decimal("105"), low=Decimal("95")),
                now_ms=_BASE_MS + 120_000,
            )

    def test_high_less_than_close_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(
                _candle(close=Decimal("110"), high=Decimal("105"), low=Decimal("95")),
                now_ms=_BASE_MS + 120_000,
            )

    def test_low_greater_than_open_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(
                _candle(open=Decimal("90"), low=Decimal("95")),
                now_ms=_BASE_MS + 120_000,
            )

    def test_low_greater_than_close_raises(self):
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(
                _candle(close=Decimal("90"), low=Decimal("95")),
                now_ms=_BASE_MS + 120_000,
            )

    def test_unaligned_open_time_raises(self):
        unaligned = _BASE_MS + 1  # 1ms off from the 1m boundary
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(
                _candle(open_time_ms=unaligned, close_time_ms=unaligned + 60_000 - 1),
                now_ms=_BASE_MS + 120_000,
            )

    def test_wrong_duration_raises(self):
        # close_time = open_time + 30_000 (half of 1m)
        with pytest.raises(CandleValidationError, match="CANDLE_INVALID_OHLC"):
            validate_candle(
                _candle(open_time_ms=_BASE_MS, close_time_ms=_BASE_MS + 30_000),
                now_ms=_BASE_MS + 120_000,
            )

    def test_code_attribute_set(self):
        try:
            validate_candle(_candle(symbol=""), now_ms=_BASE_MS + 120_000)
        except CandleValidationError as exc:
            assert exc.code == "CANDLE_INVALID_OHLC"
        else:
            pytest.fail("Expected CandleValidationError")


class TestIsValidCandle:
    def test_valid_returns_true(self):
        assert is_valid_candle(_candle(), now_ms=_BASE_MS + 120_000) is True

    def test_invalid_returns_false(self):
        assert is_valid_candle(_candle(symbol=""), now_ms=_BASE_MS + 120_000) is False


class TestCandleTimestampProperty:
    def test_timestamp_iso_format(self):
        candle = _candle()
        ts = candle.timestamp
        assert ts == "2020-01-01T00:00:00Z"

    def test_timestamp_is_string(self):
        assert isinstance(_candle().timestamp, str)


class TestCandleImmutability:
    def test_frozen_dataclass(self):
        candle = _candle()
        with pytest.raises((AttributeError, TypeError)):
            candle.close = Decimal("999")  # type: ignore[misc]
