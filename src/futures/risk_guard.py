import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from config import AppConfig
from trading_cadence import (
    entry_cadence_block,
)
from utils.events import EventChannel, observe_event


@dataclass(frozen=True)
class DailyJournalEventTypes:
    opens: frozenset[str]
    closes: frozenset[str]
    entry_attempts: frozenset[str]


_LIVE_EVENT_TYPES = DailyJournalEventTypes(
    opens=frozenset({"position_opened"}),
    closes=frozenset({"position_closed", "position_closed_externally"}),
    # An entry order sent to the exchange counts for entry spacing even when the
    # position is rolled back afterwards; otherwise a failing entry could be
    # retried (and paid for) every cycle.
    entry_attempts=frozenset({"position_opened", "position_open_requested"}),
)
_PAPER_EVENT_TYPES = DailyJournalEventTypes(
    opens=frozenset({"paper_position_opened"}),
    closes=frozenset({"paper_position_closed"}),
    entry_attempts=frozenset({"paper_position_opened"}),
)


def daily_journal_event_types(execution_mode: Any) -> DailyJournalEventTypes:
    """Return the journal events that count toward the daily limits of a mode.

    Simulated and exchange fills must never offset each other: PAPER_LIVE counts
    only paper fills, and every other mode (DRY_RUN reads the real account) only
    exchange fills. Both daily gates use this single definition.
    """

    return _PAPER_EVENT_TYPES if getattr(execution_mode, "is_paper", False) else _LIVE_EVENT_TYPES


@dataclass(frozen=True)
class RiskGuardDecision:
    allowed: bool
    reason: str
    metrics: dict[str, str | int]


class FuturesRiskGuard:
    def __init__(
        self,
        config: AppConfig,
        *,
        extra_journal_paths: Iterable[str | Path] = (),
    ):
        self.config = config
        # Paper trades are journaled outside ``trade_journal_path``; without
        # these extra paths the daily limits would never trigger in PAPER_LIVE.
        primary = Path(config.trade_journal_path)
        extras: list[Path] = []
        for raw_path in extra_journal_paths:
            path = Path(raw_path)
            if path != primary and path not in extras:
                extras.append(path)
        self.extra_journal_paths = tuple(extras)

    @property
    def journal_paths(self) -> tuple[Path, ...]:
        return (Path(self.config.trade_journal_path), *self.extra_journal_paths)

    def can_open_position(self, *, notional: Decimal) -> RiskGuardDecision:
        observe_event(
            "risk_check_started",
            component=__name__,
            category="risk",
            channel=EventChannel.AUDIT,
            symbol=self.config.symbol,
            notional=notional,
            trade_journal_path=self.config.trade_journal_path,
        )
        metrics = self._load_daily_metrics()
        pnl_limits_enabled = (
            self.config.max_daily_loss_usdt > 0
            or self.config.stop_trading_after_daily_profit_usdt > 0
            or self.config.max_consecutive_losses_guard > 0
            or self.config.cooldown_after_loss_seconds > 0
        )
        if pnl_limits_enabled and int(metrics["pnl_unknown_closes"]) > 0:
            return self._blocked("daily_pnl_incomplete", metrics, notional=notional)

        if self.config.max_exposure_usdt > 0 and notional > Decimal(
            str(self.config.max_exposure_usdt)
        ):
            return self._blocked("max_exposure_usdt", metrics, notional=notional)

        if self.config.max_daily_loss_usdt > 0 and metrics["net_pnl"] <= -Decimal(
            str(self.config.max_daily_loss_usdt)
        ):
            return self._blocked("max_daily_loss_usdt", metrics, notional=notional)

        if self.config.stop_trading_after_daily_profit_usdt > 0 and metrics["net_pnl"] >= Decimal(
            str(self.config.stop_trading_after_daily_profit_usdt)
        ):
            return self._blocked(
                "stop_trading_after_daily_profit_usdt",
                metrics,
                notional=notional,
            )

        cadence_block = entry_cadence_block(
            opened_trades_today=int(metrics["opened_trades"]),
            seconds_since_entry=metrics["seconds_since_entry"],
            max_daily_trades=self.config.max_daily_trades,
            min_seconds_between_entries=getattr(self.config, "min_seconds_between_entries", None),
        )
        if cadence_block is not None:
            return self._blocked(cadence_block, metrics, notional=notional)

        if (
            self.config.max_consecutive_losses_guard > 0
            and metrics["consecutive_losses"] >= self.config.max_consecutive_losses_guard
        ):
            return self._blocked("max_consecutive_losses", metrics, notional=notional)

        if self.config.cooldown_after_loss_seconds > 0 and metrics["seconds_since_loss"] >= 0:
            if metrics["seconds_since_loss"] < Decimal(
                str(self.config.cooldown_after_loss_seconds)
            ):
                return self._blocked("cooldown_after_loss_seconds", metrics, notional=notional)

        decision = RiskGuardDecision(True, "ok", _stringify_metrics(metrics))
        observe_event(
            "risk_check_passed",
            component=__name__,
            category="risk",
            channel=EventChannel.AUDIT,
            symbol=self.config.symbol,
            reason=decision.reason,
            metrics=decision.metrics,
            notional=notional,
        )
        return decision

    def should_close_for_max_hold(self, *, holding_seconds: float | None) -> bool:
        return (
            self.config.max_position_hold_seconds > 0
            and holding_seconds is not None
            and holding_seconds >= self.config.max_position_hold_seconds
        )

    def _load_daily_metrics(self) -> dict[str, Decimal | int]:
        now = datetime.now(timezone.utc)
        today = now.date()
        symbol = str(self.config.symbol or "").upper()
        metrics: dict[str, Decimal | int] = {
            "opened_trades": 0,
            "net_pnl": Decimal("0"),
            "consecutive_losses": 0,
            "seconds_since_loss": Decimal("-1"),
            "seconds_since_entry": Decimal("-1"),
            "pnl_unknown_closes": 0,
        }
        last_loss_at: datetime | None = None
        last_entry_at: datetime | None = None
        event_types = daily_journal_event_types(self.config.execution_mode)

        events: list[tuple[datetime, int, dict[str, Any]]] = []
        invalid_lines = 0
        sequence = 0
        for path in self.journal_paths:
            if not path.exists():
                continue
            for event, invalid in _iter_journal_events(path):
                if invalid:
                    invalid_lines += 1
                    continue
                event_at = _parse_timestamp(str(event.get("timestamp", "")))
                if event_at is None:
                    invalid_lines += 1
                    continue
                events.append((event_at, sequence, event))
                sequence += 1

        # Several journals may be merged: consecutive losses need time order.
        events.sort(key=lambda item: (item[0], item[1]))

        for event_at, _, event in events:
            event_type = event.get("event_type")
            data = event.get("data", {})
            if not isinstance(data, dict):
                data = {}

            if event_type in event_types.entry_attempts and _event_matches_symbol(data, symbol):
                # Entry spacing is checked across days; daily counters are not.
                if last_entry_at is None or event_at > last_entry_at:
                    last_entry_at = event_at

            if event_at.date() != today:
                continue

            if event_type in event_types.opens:
                if _event_matches_symbol(data, symbol):
                    metrics["opened_trades"] = int(metrics["opened_trades"]) + 1
            elif event_type in event_types.closes:
                net_pnl = _optional_decimal(data.get("net_pnl"))
                if net_pnl is None:
                    metrics["pnl_unknown_closes"] = int(metrics["pnl_unknown_closes"]) + 1
                    continue
                metrics["net_pnl"] = Decimal(metrics["net_pnl"]) + net_pnl
                if net_pnl < 0:
                    metrics["consecutive_losses"] = int(metrics["consecutive_losses"]) + 1
                    last_loss_at = event_at
                elif net_pnl > 0:
                    metrics["consecutive_losses"] = 0

        if last_loss_at is not None:
            seconds = (now - last_loss_at).total_seconds()
            metrics["seconds_since_loss"] = Decimal(str(max(seconds, 0)))

        if last_entry_at is not None:
            seconds = (now - last_entry_at).total_seconds()
            metrics["seconds_since_entry"] = Decimal(str(max(seconds, 0)))

        journal_paths = [str(path) for path in self.journal_paths]
        observe_event(
            "risk_guard_daily_metrics_loaded",
            component=__name__,
            category="risk",
            channel=EventChannel.AUDIT,
            symbol=self.config.symbol,
            journal_path=journal_paths[0],
            journal_paths=journal_paths,
            metrics=_stringify_metrics(metrics),
        )
        if invalid_lines:
            observe_event(
                "risk_guard_journal_invalid_lines_detected",
                component=__name__,
                category="risk",
                channel=EventChannel.AUDIT,
                severity="WARNING",
                symbol=self.config.symbol,
                journal_path=journal_paths[0],
                journal_paths=journal_paths,
                invalid_lines=invalid_lines,
            )

        return metrics

    def _blocked(
        self,
        reason: str,
        metrics: dict[str, Decimal | int],
        *,
        notional: Decimal,
    ) -> RiskGuardDecision:
        decision = RiskGuardDecision(False, reason, _stringify_metrics(metrics))
        observe_event(
            "risk_check_blocked",
            component=__name__,
            category="risk",
            channel=EventChannel.AUDIT,
            severity="WARNING",
            symbol=self.config.symbol,
            reason=reason,
            metrics=decision.metrics,
            notional=notional,
        )
        return decision


def _iter_journal_events(path: Path):
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                event: Any = json.loads(line)
            except json.JSONDecodeError:
                yield {}, True
                continue
            if isinstance(event, dict):
                yield event, False


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Journals are written in UTC; a naive timestamp is treated as UTC
        # instead of the host's local timezone.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_matches_symbol(data: dict[str, Any], symbol: str) -> bool:
    event_symbol = str(data.get("symbol") or "").upper().strip()
    # Events without a symbol are counted conservatively.
    return not event_symbol or not symbol or event_symbol == symbol


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _stringify_metrics(metrics: dict[str, Decimal | int]) -> dict[str, str | int]:
    return {
        key: str(value) if isinstance(value, Decimal) else value for key, value in metrics.items()
    }
