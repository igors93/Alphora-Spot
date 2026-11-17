import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import ExecutionMode
from futures.risk_guard import FuturesRiskGuard
from governor.daily_portfolio_metrics import DailyPortfolioMetricsReader


class FakeJournal:
    def __init__(self):
        self.events = []

    def append(self, event_type: str, **data):
        self.events.append((event_type, data))


def _write_event(path, *, timestamp, event_type, data=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": timestamp,
        "event_type": event_type,
        "data": data or {},
    }
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload))
        file.write("\n")


def _reader(tmp_path, execution_mode):
    trade_journal_path = tmp_path / "data" / "trades_btcusdt_testnet.jsonl"
    config = SimpleNamespace(
        trade_journal_path=str(trade_journal_path),
        environment_key="testnet",
        execution_mode=execution_mode,
    )
    symbol_configs = [
        SimpleNamespace(
            symbol="BTCUSDT",
            journal_path=str(trade_journal_path),
        )
    ]
    return DailyPortfolioMetricsReader(
        config=config,
        symbol_configs=symbol_configs,
        events_journal=FakeJournal(),
    )


def _write_mixed_day(tmp_path, today):
    trade_journal_path = tmp_path / "data" / "trades_btcusdt_testnet.jsonl"
    paper_journal_path = tmp_path / "data" / "paper_trades_btcusdt_testnet.jsonl"
    _write_event(
        trade_journal_path,
        timestamp=f"{today}T00:00:00+00:00",
        event_type="position_opened",
    )
    _write_event(
        trade_journal_path,
        timestamp=f"{today}T00:01:00+00:00",
        event_type="position_closed",
        data={"net_pnl": "-1.50"},
    )
    _write_event(
        paper_journal_path,
        timestamp=f"{today}T00:02:00+00:00",
        event_type="paper_position_opened",
    )
    _write_event(
        paper_journal_path,
        timestamp=f"{today}T00:03:00+00:00",
        event_type="paper_position_closed",
        data={"net_pnl": "3.00"},
    )
    _write_event(
        trade_journal_path,
        timestamp="2098-12-31T00:00:00+00:00",
        event_type="position_closed",
        data={"net_pnl": "999"},
    )
    _write_event(
        trade_journal_path,
        timestamp=f"{today}T00:04:00+00:00",
        event_type="ignored_event",
        data={"net_pnl": "999"},
    )


@pytest.mark.parametrize(
    "execution_mode",
    [ExecutionMode.DRY_RUN, ExecutionMode.TESTNET_LIVE, ExecutionMode.REAL_LIVE],
)
def test_daily_metrics_count_only_exchange_fills_outside_paper(tmp_path, execution_mode):
    today = "2099-01-01"
    _write_mixed_day(tmp_path, today)
    reader = _reader(tmp_path, execution_mode)

    metrics = reader._calculate_metrics(reader._read_events(reader._journal_paths(), today))

    assert metrics == {
        "daily_realized_pnl": "-1.50",
        "daily_realized_pnl_peak": "0",
        "daily_trades": 1,
        "consecutive_losses": 1,
        "pnl_unknown_closes": 0,
    }


def test_daily_metrics_count_only_paper_fills_in_paper_live(tmp_path):
    today = "2099-01-01"
    _write_mixed_day(tmp_path, today)
    reader = _reader(tmp_path, ExecutionMode.PAPER_LIVE)

    metrics = reader._calculate_metrics(reader._read_events(reader._journal_paths(), today))

    assert metrics == {
        "daily_realized_pnl": "3.00",
        "daily_realized_pnl_peak": "3.00",
        "daily_trades": 1,
        "consecutive_losses": 0,
        "pnl_unknown_closes": 0,
    }


def test_daily_portfolio_metrics_reader_records_unknown_pnl():
    journal = FakeJournal()

    reader = DailyPortfolioMetricsReader(
        config=SimpleNamespace(
            trade_journal_path="data/trades.jsonl",
            environment_key="testnet",
            execution_mode=ExecutionMode.TESTNET_LIVE,
        ),
        symbol_configs=[],
        events_journal=journal,
    )

    metrics = reader._calculate_metrics(
        [
            {
                "timestamp": "2099-01-01T00:00:00+00:00",
                "event_type": "position_closed",
                "net_pnl": None,
            },
            {
                "timestamp": "2099-01-01T00:01:00+00:00",
                "event_type": "position_closed_externally",
                "net_pnl": "",
            },
        ]
    )

    assert metrics == {
        "daily_realized_pnl": "0",
        "daily_realized_pnl_peak": "0",
        "daily_trades": 0,
        "consecutive_losses": 0,
        "pnl_unknown_closes": 2,
    }

    assert journal.events == [
        ("daily_metrics_pnl_incomplete", {"reason": "position_closed_sem_net_pnl"}),
        ("daily_metrics_pnl_incomplete", {"reason": "position_closed_sem_net_pnl"}),
    ]


def test_daily_portfolio_metrics_reader_uses_context_cache():
    journal = FakeJournal()
    context = SimpleNamespace(
        daily_metrics={
            "daily_realized_pnl": "5",
            "daily_trades": 1,
            "consecutive_losses": 0,
            "pnl_unknown_closes": 0,
        }
    )

    reader = DailyPortfolioMetricsReader(
        config=SimpleNamespace(
            trade_journal_path="data/trades.jsonl",
            environment_key="testnet",
            execution_mode=ExecutionMode.TESTNET_LIVE,
        ),
        symbol_configs=[],
        events_journal=journal,
    )

    metrics = reader.read(context=context)

    assert metrics == {
        "daily_realized_pnl": "5",
        "daily_trades": 1,
        "consecutive_losses": 0,
        "pnl_unknown_closes": 0,
    }


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@pytest.mark.parametrize("execution_mode", [ExecutionMode.TESTNET_LIVE, ExecutionMode.REAL_LIVE])
def test_paper_profit_never_offsets_a_live_loss_in_either_daily_gate(
    config_factory,
    execution_mode,
):
    config = replace(config_factory(execution_mode=execution_mode), max_daily_loss_usdt=25.0)
    live_path = config.trade_journal_path
    paper_path = str(
        Path(live_path).parent / f"paper_trades_btcusdt_{config.environment_key}.jsonl"
    )
    today = _today()

    _write_event(
        Path(paper_path),
        timestamp=f"{today}T00:30:00+00:00",
        event_type="paper_position_closed",
        data={"net_pnl": "50"},
    )
    _write_event(
        Path(live_path),
        timestamp=f"{today}T01:00:00+00:00",
        event_type="position_closed",
        data={"net_pnl": "-30", "symbol": "BTCUSDT"},
    )
    reader = DailyPortfolioMetricsReader(
        config=config,
        symbol_configs=[SimpleNamespace(symbol="BTCUSDT", journal_path=live_path)],
        events_journal=FakeJournal(),
    )
    # Even when handed the paper journal, the live guard must ignore paper fills.
    guard = FuturesRiskGuard(config, extra_journal_paths=(paper_path,))

    metrics = reader.read()
    decision = guard.can_open_position(notional=Decimal("10"))

    assert metrics["daily_realized_pnl"] == "-30"
    assert decision.allowed is False
    assert decision.reason == "max_daily_loss_usdt"
    assert decision.metrics["net_pnl"] == "-30"


def test_live_loss_never_counts_toward_paper_daily_gates(config_factory):
    config = replace(
        config_factory(execution_mode=ExecutionMode.PAPER_LIVE),
        max_daily_loss_usdt=25.0,
    )
    live_path = config.trade_journal_path
    paper_path = str(
        Path(live_path).parent / f"paper_trades_btcusdt_{config.environment_key}.jsonl"
    )
    today = _today()

    _write_event(
        Path(live_path),
        timestamp=f"{today}T00:30:00+00:00",
        event_type="position_closed",
        data={"net_pnl": "-30", "symbol": "BTCUSDT"},
    )
    _write_event(
        Path(paper_path),
        timestamp=f"{today}T01:00:00+00:00",
        event_type="paper_position_closed",
        data={"net_pnl": "5"},
    )
    reader = DailyPortfolioMetricsReader(
        config=config,
        symbol_configs=[SimpleNamespace(symbol="BTCUSDT", journal_path=live_path)],
        events_journal=FakeJournal(),
    )
    guard = FuturesRiskGuard(config, extra_journal_paths=(paper_path,))

    metrics = reader.read()
    decision = guard.can_open_position(notional=Decimal("10"))

    assert metrics["daily_realized_pnl"] == "5"
    assert decision.allowed is True
    assert decision.metrics["net_pnl"] == "5"
