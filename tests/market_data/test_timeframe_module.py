from __future__ import annotations

import pytest

from domain.timeframe import (
    SUPPORTED_TIMEFRAMES,
    approximate_horizon_label,
    expected_close_time_ms,
    expected_open_time_ms,
    is_supported_timeframe,
    normalize_timeframe,
    timeframe_duration_ms,
    timeframe_label,
)

_BASE_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z — aligned to all timeframes


class TestNormalizeTimeframe:
    def test_valid_lowercase(self):
        assert normalize_timeframe("1m") == "1m"

    def test_valid_uppercase_normalized(self):
        assert normalize_timeframe("1M") == "1m"

    def test_valid_with_spaces(self):
        assert normalize_timeframe("  5m  ") == "5m"

    def test_all_supported(self):
        for tf in SUPPORTED_TIMEFRAMES:
            assert normalize_timeframe(tf) == tf

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Timeframe invalido"):
            normalize_timeframe("2m")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            normalize_timeframe("")

    def test_none_like_raises(self):
        with pytest.raises(ValueError):
            normalize_timeframe("none")


class TestIsSupportedTimeframe:
    def test_valid(self):
        assert is_supported_timeframe("1m") is True
        assert is_supported_timeframe("1h") is True
        assert is_supported_timeframe("1d") is True

    def test_invalid(self):
        assert is_supported_timeframe("2m") is False
        assert is_supported_timeframe("") is False
        assert is_supported_timeframe("1y") is False

    def test_case_insensitive(self):
        assert is_supported_timeframe("1M") is True
        assert is_supported_timeframe("1H") is True


class TestTimeframeDurationMs:
    def test_1m(self):
        assert timeframe_duration_ms("1m") == 60_000

    def test_5m(self):
        assert timeframe_duration_ms("5m") == 300_000

    def test_1h(self):
        assert timeframe_duration_ms("1h") == 3_600_000

    def test_1d(self):
        assert timeframe_duration_ms("1d") == 86_400_000

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            timeframe_duration_ms("2m")


class TestExpectedOpenTimeMs:
    def test_already_aligned(self):
        assert expected_open_time_ms(_BASE_MS, "1m") == _BASE_MS

    def test_floors_to_boundary(self):
        mid_candle = _BASE_MS + 30_000  # 30s into 1m candle
        assert expected_open_time_ms(mid_candle, "1m") == _BASE_MS

    def test_5m_boundary(self):
        mid = _BASE_MS + 120_000  # 2m into 5m candle
        assert expected_open_time_ms(mid, "5m") == _BASE_MS

    def test_1h_boundary(self):
        mid = _BASE_MS + 1_800_000  # 30min into hour
        assert expected_open_time_ms(mid, "1h") == _BASE_MS


class TestExpectedCloseTimeMs:
    def test_1m_close(self):
        assert expected_close_time_ms(_BASE_MS, "1m") == _BASE_MS + 60_000 - 1

    def test_5m_close(self):
        assert expected_close_time_ms(_BASE_MS, "5m") == _BASE_MS + 300_000 - 1

    def test_1h_close(self):
        assert expected_close_time_ms(_BASE_MS, "1h") == _BASE_MS + 3_600_000 - 1


class TestTimeframeLabel:
    def test_1m(self):
        assert timeframe_label("1m") == "1 minuto"

    def test_5m(self):
        assert timeframe_label("5m") == "5 minutos"

    def test_1h(self):
        assert timeframe_label("1h") == "1 hora"

    def test_1d(self):
        assert timeframe_label("1d") == "1 dia"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            timeframe_label("2m")


class TestApproximateHorizonLabel:
    def test_minutes(self):
        assert approximate_horizon_label("1m", 30) == "~30 minutos"

    def test_hours(self):
        assert approximate_horizon_label("5m", 12) == "~1 horas"

    def test_days(self):
        assert approximate_horizon_label("1h", 48) == "~2 dias"

    def test_1m_5_candles(self):
        assert approximate_horizon_label("1m", 5) == "~5 minutos"

    def test_15m_4_candles(self):
        assert approximate_horizon_label("15m", 4) == "~1 horas"
