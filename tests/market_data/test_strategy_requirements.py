from __future__ import annotations

import pytest

from domain.strategy_requirements import (
    EVALUATION_EVERY_CYCLE,
    EVALUATION_ON_ANY_REQUIRED_CANDLE_CLOSE,
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    StrategyDataRequirements,
    requirements_from_legacy_metadata,
)


class TestStrategyDataRequirements:
    def test_valid_single_timeframe(self):
        req = StrategyDataRequirements(
            primary_timeframe="1m",
            required_candles={"1m": 120},
        )
        assert req.primary_timeframe == "1m"
        assert req.required_candles == {"1m": 120}
        assert req.evaluation_policy == EVALUATION_EVERY_CYCLE
        assert req.closed_candles_only is True

    def test_multi_timeframe(self):
        req = StrategyDataRequirements(
            primary_timeframe="1m",
            required_candles={"1m": 120, "5m": 60},
            evaluation_policy=EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
        )
        assert req.required_candles["5m"] == 60

    def test_primary_timeframe_normalized(self):
        req = StrategyDataRequirements(
            primary_timeframe="1M",
            required_candles={"1m": 10},
        )
        assert req.primary_timeframe == "1m"

    def test_invalid_primary_timeframe_raises(self):
        with pytest.raises(ValueError, match="Timeframe invalido"):
            StrategyDataRequirements(
                primary_timeframe="2m",
                required_candles={"2m": 10},
            )

    def test_primary_not_in_required_candles_raises(self):
        with pytest.raises(ValueError, match="primary_timeframe"):
            StrategyDataRequirements(
                primary_timeframe="1m",
                required_candles={"5m": 60},
            )

    def test_invalid_evaluation_policy_raises(self):
        with pytest.raises(ValueError, match="evaluation_policy invalida"):
            StrategyDataRequirements(
                primary_timeframe="1m",
                required_candles={"1m": 10},
                evaluation_policy="INVALID_POLICY",
            )

    def test_zero_candle_count_raises(self):
        with pytest.raises(ValueError, match="deve ser > 0"):
            StrategyDataRequirements(
                primary_timeframe="1m",
                required_candles={"1m": 0},
            )

    def test_negative_candle_count_raises(self):
        with pytest.raises(ValueError, match="deve ser > 0"):
            StrategyDataRequirements(
                primary_timeframe="1m",
                required_candles={"1m": -5},
            )

    def test_invalid_required_timeframe_raises(self):
        with pytest.raises(ValueError):
            StrategyDataRequirements(
                primary_timeframe="1m",
                required_candles={"1m": 10, "2m": 5},
            )

    def test_all_evaluation_policies_accepted(self):
        for policy in (
            EVALUATION_EVERY_CYCLE,
            EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
            EVALUATION_ON_ANY_REQUIRED_CANDLE_CLOSE,
        ):
            req = StrategyDataRequirements(
                primary_timeframe="1m",
                required_candles={"1m": 10},
                evaluation_policy=policy,
            )
            assert req.evaluation_policy == policy

    def test_frozen_immutable(self):
        req = StrategyDataRequirements(
            primary_timeframe="1m",
            required_candles={"1m": 10},
        )
        with pytest.raises((AttributeError, TypeError)):
            req.primary_timeframe = "5m"  # type: ignore[misc]


class _FakeMeta:
    def __init__(self, *, required_history=20, required_timeframes=None):
        self.required_history = required_history
        self.required_timeframes = required_timeframes


class TestRequirementsFromLegacyMetadata:
    def test_basic_conversion(self):
        meta = _FakeMeta(required_history=20, required_timeframes=["1m"])
        req = requirements_from_legacy_metadata(meta)
        assert req.primary_timeframe == "1m"
        assert req.required_candles == {"1m": 20}
        assert req.evaluation_policy == EVALUATION_EVERY_CYCLE

    def test_defaults_to_1m_when_no_timeframes(self):
        meta = _FakeMeta(required_history=10, required_timeframes=None)
        req = requirements_from_legacy_metadata(meta)
        assert req.primary_timeframe == "1m"
        assert req.required_candles["1m"] == 10

    def test_first_timeframe_is_primary(self):
        meta = _FakeMeta(required_history=15, required_timeframes=["5m", "1m"])
        req = requirements_from_legacy_metadata(meta)
        assert req.primary_timeframe == "5m"

    def test_all_timeframes_get_same_history(self):
        meta = _FakeMeta(required_history=30, required_timeframes=["1m", "5m"])
        req = requirements_from_legacy_metadata(meta)
        assert req.required_candles["1m"] == 30
        assert req.required_candles["5m"] == 30

    def test_preserves_every_cycle_policy(self):
        meta = _FakeMeta()
        req = requirements_from_legacy_metadata(meta)
        assert req.evaluation_policy == EVALUATION_EVERY_CYCLE

    def test_history_at_least_1(self):
        meta = _FakeMeta(required_history=0)
        req = requirements_from_legacy_metadata(meta)
        assert req.required_candles["1m"] >= 1
