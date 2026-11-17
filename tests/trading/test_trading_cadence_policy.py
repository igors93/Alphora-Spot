"""Anti-HFT cadence policy: Alphora trades day/swing, never seconds or milliseconds."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import futures.risk_guard as risk_guard_module
from backtesting.adapters.decision_stages import SimulatedAccountPort
from config import ExecutionMode
from config.symbol_context import symbol_config_from_app_config
from config.validators import _validate_trading_cadence
from domain.strategy_requirements import StrategyDataRequirements
from domain.timeframe import is_signal_timeframe_allowed_for_real
from futures.risk_guard import FuturesRiskGuard
from governor.strategy_coordinator import StrategyCoordinator
from strategies.futures.fixed.execution_probe import ExecutionProbeStrategy
from strategies.futures.fixed.random_timed_signal import RandomTimedSignalStrategy
from strategies.loader import StrategyLoadError
from trading.intent import TradeIntent
from trading_cadence import (
    DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES,
    MAX_DAILY_TRADES_CEILING,
    MIN_SECONDS_BETWEEN_ENTRIES_FLOOR,
    effective_max_daily_trades,
    effective_min_seconds_between_entries,
)

FROZEN_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW


@pytest.fixture
def frozen_risk_clock(monkeypatch):
    monkeypatch.setattr(risk_guard_module, "datetime", _FrozenDatetime)


def _write_events(path: Path, events: list[tuple[str, dict, datetime]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        for event_type, data, at in events:
            payload = {"timestamp": at.isoformat(), "event_type": event_type, "data": data}
            file.write(json.dumps(payload) + "\n")


def test_effective_limits_are_never_unlimited():
    assert effective_max_daily_trades(0) == MAX_DAILY_TRADES_CEILING
    assert effective_max_daily_trades(-5) == MAX_DAILY_TRADES_CEILING
    assert effective_max_daily_trades(10) == 10
    assert effective_max_daily_trades(10_000) == MAX_DAILY_TRADES_CEILING

    assert effective_min_seconds_between_entries(None) == DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES
    assert effective_min_seconds_between_entries(0) == MIN_SECONDS_BETWEEN_ENTRIES_FLOOR
    assert effective_min_seconds_between_entries(0.001) == MIN_SECONDS_BETWEEN_ENTRIES_FLOOR
    assert (
        effective_min_seconds_between_entries(float("nan")) == DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES
    )
    assert effective_min_seconds_between_entries(900) == 900


def test_real_money_signal_timeframe_floor_is_day_trade():
    assert not is_signal_timeframe_allowed_for_real("1m")
    assert not is_signal_timeframe_allowed_for_real("3m")
    assert not is_signal_timeframe_allowed_for_real("")
    assert is_signal_timeframe_allowed_for_real("5m")
    assert is_signal_timeframe_allowed_for_real("15m")
    assert is_signal_timeframe_allowed_for_real("4h")
    assert is_signal_timeframe_allowed_for_real("1d")


def test_risk_guard_blocks_entry_too_soon_after_previous_entry(config_factory, frozen_risk_clock):
    config = config_factory()
    _write_events(
        Path(config.trade_journal_path),
        [("position_opened", {"symbol": "BTCUSDT"}, FROZEN_NOW - timedelta(seconds=30))],
    )

    decision = FuturesRiskGuard(config).can_open_position(notional=Decimal("50"))

    assert decision.allowed is False
    assert decision.reason == "min_seconds_between_entries"


def test_risk_guard_allows_entry_after_spacing(config_factory, frozen_risk_clock):
    config = config_factory()
    _write_events(
        Path(config.trade_journal_path),
        [
            (
                "position_opened",
                {"symbol": "BTCUSDT"},
                FROZEN_NOW - timedelta(seconds=DEFAULT_MIN_SECONDS_BETWEEN_ENTRIES + 1),
            )
        ],
    )

    assert FuturesRiskGuard(config).can_open_position(notional=Decimal("50")).allowed


def test_risk_guard_counts_rolled_back_entry_attempts(config_factory, frozen_risk_clock):
    # An entry that was sent and then rolled back never journals position_opened;
    # the attempt itself must still respect the spacing.
    config = config_factory()
    _write_events(
        Path(config.trade_journal_path),
        [("position_open_requested", {"symbol": "BTCUSDT"}, FROZEN_NOW - timedelta(seconds=5))],
    )

    decision = FuturesRiskGuard(config).can_open_position(notional=Decimal("50"))

    assert decision.reason == "min_seconds_between_entries"


def test_risk_guard_spacing_is_per_symbol(config_factory, frozen_risk_clock):
    config = config_factory()
    _write_events(
        Path(config.trade_journal_path),
        [("position_opened", {"symbol": "ETHUSDT"}, FROZEN_NOW - timedelta(seconds=5))],
    )

    assert FuturesRiskGuard(config).can_open_position(notional=Decimal("50")).allowed


def test_risk_guard_zero_daily_trades_means_ceiling_not_unlimited(
    config_factory,
    frozen_risk_clock,
):
    config = config_factory(max_daily_trades=0)
    start = FROZEN_NOW - timedelta(hours=10)
    _write_events(
        Path(config.trade_journal_path),
        [
            ("position_opened", {"symbol": "BTCUSDT"}, start + timedelta(minutes=10 * index))
            for index in range(MAX_DAILY_TRADES_CEILING)
        ],
    )

    decision = FuturesRiskGuard(config).can_open_position(notional=Decimal("50"))

    assert decision.allowed is False
    assert decision.reason == "max_daily_trades"


def test_risk_guard_sees_paper_journal(config_factory, frozen_risk_clock, tmp_path):
    config = config_factory(execution_mode=ExecutionMode.PAPER_LIVE)
    paper_journal = tmp_path / "data" / "paper_trades_btcusdt_testnet.jsonl"
    _write_events(
        paper_journal,
        [("paper_position_opened", {"symbol": "BTCUSDT"}, FROZEN_NOW - timedelta(seconds=30))],
    )

    without_paper = FuturesRiskGuard(config)
    with_paper = FuturesRiskGuard(config, extra_journal_paths=[paper_journal])

    assert without_paper.can_open_position(notional=Decimal("50")).allowed
    assert with_paper.can_open_position(notional=Decimal("50")).allowed is False


def test_risk_guard_paper_losses_trigger_daily_loss_limit(
    config_factory,
    frozen_risk_clock,
    tmp_path,
):
    config = config_factory(execution_mode=ExecutionMode.PAPER_LIVE, max_daily_loss_usdt=10.0)
    paper_journal = tmp_path / "data" / "paper_trades_btcusdt_testnet.jsonl"
    _write_events(
        paper_journal,
        [("paper_position_closed", {"net_pnl": "-12.5"}, FROZEN_NOW - timedelta(hours=1))],
    )

    decision = FuturesRiskGuard(config, extra_journal_paths=[paper_journal]).can_open_position(
        notional=Decimal("50")
    )

    assert decision.reason == "max_daily_loss_usdt"


def test_validator_rejects_high_frequency_cadence(config_factory):
    config = replace(config_factory(), loop_interval_seconds=3.0)
    _validate_trading_cadence(config)

    with pytest.raises(ValueError, match="LOOP_INTERVAL"):
        _validate_trading_cadence(replace(config, loop_interval_seconds=0.2))
    with pytest.raises(ValueError, match="MIN_SECONDS_BETWEEN_ENTRIES"):
        _validate_trading_cadence(replace(config, min_seconds_between_entries=10.0))
    with pytest.raises(ValueError, match="MAX_DAILY_TRADES"):
        _validate_trading_cadence(
            replace(config, max_daily_trades=MAX_DAILY_TRADES_CEILING + 1),
        )


@pytest.mark.parametrize("strategy_class", [ExecutionProbeStrategy, RandomTimedSignalStrategy])
def test_scripted_test_strategies_refuse_sub_minute_timers(strategy_class):
    with pytest.raises(ValueError, match="signal_interval_seconds"):
        strategy_class(signal_interval_seconds=5)
    with pytest.raises(ValueError, match="signal_interval_seconds"):
        strategy_class(signal_interval_seconds=0.5)

    assert strategy_class(signal_interval_seconds=60).signal_interval_seconds == 60


def test_real_live_refuses_scripted_test_strategies(config_factory):
    config = config_factory(execution_mode=ExecutionMode.REAL_LIVE)
    profile = symbol_config_from_app_config(config)

    with pytest.raises(StrategyLoadError, match="REAL_LIVE"):
        StrategyCoordinator(config=config, symbol_configs=[profile])


def _real_live_coordinator(config_factory) -> tuple[StrategyCoordinator, object]:
    config = replace(
        config_factory(execution_mode=ExecutionMode.REAL_LIVE),
        strategy_module="strategies.futures.fixed.donchian_adx_breakout",
        strategy_class="DonchianAdxBreakoutStrategy",
    )
    profile = symbol_config_from_app_config(config)
    return StrategyCoordinator(config=config, symbol_configs=[profile]), profile


def _intent(action: str) -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT",
        action=action,
        confidence=95,
        reason="setup",
        strategy_name="test",
    )


def test_real_live_allows_day_trade_timeframe_entries(config_factory):
    coordinator, profile = _real_live_coordinator(config_factory)

    result = coordinator._enforce_real_money_cadence(profile, _intent("OPEN_LONG"))

    assert result.action == "OPEN_LONG"


def test_real_live_blocks_entries_driven_by_one_minute_candles(config_factory, monkeypatch):
    coordinator, profile = _real_live_coordinator(config_factory)
    monkeypatch.setattr(
        coordinator,
        "requirements_for_symbol",
        lambda *args, **kwargs: StrategyDataRequirements(
            primary_timeframe="1m",
            required_candles={"1m": 10},
        ),
    )

    blocked = coordinator._enforce_real_money_cadence(profile, _intent("OPEN_SHORT"))
    close = coordinator._enforce_real_money_cadence(profile, _intent("CLOSE_POSITION"))

    assert blocked.action == "HOLD"
    assert blocked.metadata["cadence_policy_blocked_action"] == "OPEN_SHORT"
    # Exits must never be blocked by the cadence policy.
    assert close.action == "CLOSE_POSITION"


def _backtest_simulator(trades, *, max_daily_trades=2):
    return SimulatedAccountPort(
        SimpleNamespace(trades=trades),
        signal_timing_policy="ON_CLOSE",
        min_seconds_between_entries=300.0,
        max_daily_trades=max_daily_trades,
    )


def _event(symbol: str, close_after_seconds: int):
    base_ms = 1_767_225_600_000  # 2026-01-01T00:00:00Z
    close_ms = base_ms + close_after_seconds * 1000 - 1
    return SimpleNamespace(
        symbol=symbol,
        candle=SimpleNamespace(open_time_ms=close_ms - 299_999, close_time_ms=close_ms),
    )


def test_backtest_applies_live_entry_spacing():
    trades = [SimpleNamespace(symbol="BTCUSDT", entry_time="2026-01-01T00:00:00+00:00")]
    simulator = _backtest_simulator(trades)

    blocked = simulator.cadence_block(_event("BTCUSDT", 60))

    assert blocked is not None
    assert blocked[0] == "MIN_SECONDS_BETWEEN_ENTRIES"
    assert simulator.cadence_block(_event("BTCUSDT", 600)) is None
    assert simulator.cadence_block(_event("ETHUSDT", 60)) is None


def test_backtest_applies_daily_entry_limit():
    trades = [
        SimpleNamespace(symbol="BTCUSDT", entry_time="2026-01-01T00:00:00+00:00"),
        SimpleNamespace(symbol="BTCUSDT", entry_time="2026-01-01T01:00:00+00:00"),
    ]
    simulator = _backtest_simulator(trades, max_daily_trades=2)

    blocked = simulator.cadence_block(_event("BTCUSDT", 3 * 3600))

    assert blocked is not None
    assert blocked[0] == "MAX_DAILY_TRADES"
    # A new UTC day resets the daily counter.
    assert simulator.cadence_block(_event("BTCUSDT", 25 * 3600)) is None
