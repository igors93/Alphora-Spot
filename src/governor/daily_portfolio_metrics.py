import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from config import AppConfig
from config.symbol_context import SymbolTradingConfig
from futures.risk_guard import daily_journal_event_types
from governor.governor_helpers import _journal_signature


class DailyPortfolioMetricsReader:
    def __init__(
        self,
        *,
        config: AppConfig,
        symbol_configs: list[SymbolTradingConfig],
        events_journal,
    ):
        self.config = config
        self.symbol_configs = symbol_configs
        self.events_journal = events_journal
        self._cache: tuple[str, tuple[tuple[str, int, int], ...], dict[str, Any]] | None = None

    def read(self, *, context: Any | None = None) -> dict[str, Any]:
        if context is not None and context.daily_metrics is not None:
            return dict(context.daily_metrics)

        today = datetime.now(timezone.utc).date().isoformat()
        paths = self._journal_paths()
        signature = _journal_signature(paths)

        if self._cache is not None:
            cached_today, cached_signature, cached_metrics = self._cache
            if cached_today == today and cached_signature == signature:
                metrics = dict(cached_metrics)
                if context is not None:
                    context.daily_metrics = metrics
                return metrics

        events = self._read_events(paths, today)
        metrics = self._calculate_metrics(events)

        self._cache = (today, signature, dict(metrics))

        if context is not None:
            context.daily_metrics = dict(metrics)

        return metrics

    def _journal_paths(self) -> list[Path]:
        paper = self.config.execution_mode.is_paper
        paths: list[Path] = []
        seen_paths: set[Path] = set()

        for symbol_config in self.symbol_configs:
            # Same journals as the per-symbol FuturesRiskGuard: paper fills are
            # journaled apart and must never mix with exchange fills.
            journal_path = Path(symbol_config.journal_path)
            path = (
                journal_path.parent
                / f"paper_trades_{symbol_config.symbol.lower()}_{self.config.environment_key}.jsonl"
                if paper
                else journal_path
            )
            if path in seen_paths:
                continue

            seen_paths.add(path)
            paths.append(path)

        return paths

    def _read_events(
        self,
        paths: list[Path],
        today: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        event_types = daily_journal_event_types(self.config.execution_mode)
        counted_types = event_types.opens | event_types.closes

        for path in paths:
            if not path.exists():
                continue

            try:
                with path.open("r", encoding="utf-8") as file:
                    for line in file:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        timestamp = str(event.get("timestamp", ""))
                        if not timestamp.startswith(today):
                            continue

                        event_type = event.get("event_type")
                        if event_type not in counted_types:
                            continue

                        data = event.get("data", {})
                        events.append(
                            {
                                "timestamp": timestamp,
                                "event_type": event_type,
                                # Missing net_pnl means unknown, never zero — a zero
                                # default would silently corrupt the daily PnL gate.
                                "net_pnl": data.get("net_pnl"),
                            }
                        )
            except Exception as error:
                self.events_journal.append(
                    "daily_metrics_file_failed",
                    path=str(path),
                    reason=str(error),
                )

        return events

    def _calculate_metrics(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        net_pnl = Decimal("0")
        # High-water mark of the cumulative realized PnL since the UTC day start
        # (which is 0): the reference for the intraday realized drawdown.
        peak_net_pnl = Decimal("0")
        daily_trades = 0
        consecutive_losses = 0
        pnl_unknown_closes = 0
        event_types = daily_journal_event_types(self.config.execution_mode)

        for event in sorted(events, key=lambda item: item["timestamp"]):
            event_type = event["event_type"]

            if event_type in event_types.opens:
                daily_trades += 1

            if event_type in event_types.closes:
                if event["net_pnl"] in {None, ""}:
                    pnl_unknown_closes += 1
                    self.events_journal.append(
                        "daily_metrics_pnl_incomplete",
                        reason="position_closed_sem_net_pnl",
                    )
                    continue

                try:
                    value = Decimal(str(event["net_pnl"]))
                    if not value.is_finite():
                        raise ValueError(f"net_pnl nao finito: {event['net_pnl']}")
                except Exception as error:
                    # An unreadable PnL is unknown, not zero: count it so the
                    # daily-loss gate fails closed instead of under-counting.
                    pnl_unknown_closes += 1
                    self.events_journal.append(
                        "daily_metrics_event_failed",
                        reason=str(error),
                    )
                    continue

                net_pnl += value
                peak_net_pnl = max(peak_net_pnl, net_pnl)

                if value < 0:
                    consecutive_losses += 1
                elif value > 0:
                    consecutive_losses = 0

        return {
            "daily_realized_pnl": str(net_pnl),
            "daily_realized_pnl_peak": str(peak_net_pnl),
            "daily_trades": daily_trades,
            "consecutive_losses": consecutive_losses,
            "pnl_unknown_closes": pnl_unknown_closes,
        }
