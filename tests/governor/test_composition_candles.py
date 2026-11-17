"""ADR-004: the candle hub fetches klines from the configured endpoint and says so."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import governor.composition as composition
from config.enums import ExecutionMode
from domain.strategy_requirements import (
    EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
    StrategyDataRequirements,
)
from futures.trend import TrendContextEngine


class _Coordinator:
    def requirements_by_symbol(self) -> dict[str, StrategyDataRequirements]:
        return {
            "BTCUSDT": StrategyDataRequirements(
                primary_timeframe="15m",
                required_candles={"15m": 20},
                evaluation_policy=EVALUATION_ON_PRIMARY_CANDLE_CLOSE,
            )
        }


class _Hub:
    def __init__(self, provider: Any, *, requirements_by_symbol: Any, mode: str) -> None:
        self.provider = provider
        self.mode = mode
        self.requirements_by_symbol = requirements_by_symbol

    def bootstrap(self) -> None:
        pass


@pytest.fixture
def audits(
    monkeypatch: pytest.MonkeyPatch, capture_audit_events
) -> list[tuple[str, dict[str, Any]]]:
    monkeypatch.setattr(composition, "CandleDataHub", _Hub)
    return capture_audit_events("governor.composition")


@pytest.mark.parametrize(
    "url",
    [
        "https://fapi.binance.com/fapi/v1/klines",
        "https://testnet.binancefuture.com/fapi/v1/klines",
    ],
)
def test_candle_hub_uses_the_configured_klines_endpoint(config_factory, audits, url):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        candle_hub_mode="AUTO",
        candle_klines_url=url,
    )

    hub = composition._build_candle_data_hub(config, _Coordinator())

    assert isinstance(hub, _Hub)
    assert hub.provider._base_url == url
    started = [payload for event_type, payload in audits if event_type == "candle_hub_started"]
    assert started and started[0]["klines_url"] == url
    assert started[0]["environment"] == config.environment.value


def test_candle_hub_fetches_enough_closed_candles_for_the_trend(config_factory, audits):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE), candle_hub_mode="AUTO"
    )

    hub = composition._build_candle_data_hub(config, _Coordinator())

    window = TrendContextEngine.from_config(config).required_price_window()
    assert hub.requirements_by_symbol["BTCUSDT"].required_candles == {"15m": window}


def test_configured_trend_timeframe_adds_its_own_series(config_factory, audits):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        candle_hub_mode="AUTO",
        trend_timeframe="1h",
    )

    hub = composition._build_candle_data_hub(config, _Coordinator())

    window = TrendContextEngine.from_config(config).required_price_window()
    assert hub.requirements_by_symbol["BTCUSDT"].required_candles == {"15m": 20, "1h": window}


def test_invalid_trend_timeframe_is_rejected(config_factory, audits):
    config = replace(
        config_factory(execution_mode=ExecutionMode.TESTNET_LIVE),
        candle_hub_mode="AUTO",
        trend_timeframe="7x",
    )

    with pytest.raises(ValueError, match="ALPHORA_TREND_TIMEFRAME"):
        composition._build_candle_data_hub(config, _Coordinator())
