from dataclasses import dataclass, field
from enum import Enum
from typing import Any

OPEN_ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}


class SymbolQuarantineLevel(Enum):
    NONE = "NONE"
    WATCH = "WATCH"
    COOLDOWN = "COOLDOWN"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class SymbolQuarantineSettings:
    watch_after_errors: int = 2
    cooldown_after_errors: int = 3
    quarantine_after_errors: int = 5
    cooldown_seconds: float = 600.0
    quarantine_seconds: float = 1800.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "watch_after_errors", max(1, int(self.watch_after_errors)))
        object.__setattr__(self, "cooldown_after_errors", max(1, int(self.cooldown_after_errors)))
        object.__setattr__(
            self,
            "quarantine_after_errors",
            max(1, int(self.quarantine_after_errors)),
        )
        object.__setattr__(self, "cooldown_seconds", max(0.0, float(self.cooldown_seconds)))
        object.__setattr__(
            self,
            "quarantine_seconds",
            max(0.0, float(self.quarantine_seconds)),
        )


@dataclass(frozen=True)
class SymbolQuarantineState:
    symbol: str
    level: SymbolQuarantineLevel = SymbolQuarantineLevel.NONE
    error_count: int = 0
    reason: str = ""
    updated_at: float = 0.0
    expires_at: float | None = None
    last_event: str = ""

    def expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "level": self.level.value,
            "error_count": self.error_count,
            "reason": self.reason,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "last_event": self.last_event,
        }


@dataclass(frozen=True)
class SymbolQuarantineDecision:
    symbol: str
    action: str
    allowed: bool
    level: SymbolQuarantineLevel
    reason: str
    blocks_new_entry: bool
    position_open: bool
    state: SymbolQuarantineState
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "allowed": self.allowed,
            "level": self.level.value,
            "reason": self.reason,
            "blocks_new_entry": self.blocks_new_entry,
            "position_open": self.position_open,
            "state": self.state.to_dict(),
            "diagnostics": dict(self.diagnostics),
        }


class SymbolQuarantine:
    def __init__(self, settings: SymbolQuarantineSettings | None = None):
        self.settings = settings or SymbolQuarantineSettings()
        self._states: dict[str, SymbolQuarantineState] = {}

    def state_for(self, symbol: str, *, now: float) -> SymbolQuarantineState:
        normalized = _normalize_symbol(symbol)
        state = self._states.get(normalized)
        if state is None:
            return SymbolQuarantineState(symbol=normalized)
        if state.expired(now):
            reset_state = SymbolQuarantineState(
                symbol=normalized,
                level=SymbolQuarantineLevel.NONE,
                error_count=0,
                reason="QUARANTINE_EXPIRED",
                updated_at=now,
                expires_at=None,
                last_event=state.last_event,
            )
            self._states[normalized] = reset_state
            return reset_state
        return state

    def record_event(
        self,
        symbol: str,
        *,
        event: str,
        reason: str,
        now: float,
    ) -> tuple[SymbolQuarantineState, SymbolQuarantineState]:
        normalized = _normalize_symbol(symbol)
        previous = self.state_for(normalized, now=now)
        error_count = previous.error_count + 1
        level = self._level_for_count(error_count)
        expires_at = self._expires_at_for_level(level, now)
        new_state = SymbolQuarantineState(
            symbol=normalized,
            level=level,
            error_count=error_count,
            reason=str(reason or event or "").upper().strip(),
            updated_at=now,
            expires_at=expires_at,
            last_event=str(event or "").upper().strip(),
        )
        self._states[normalized] = new_state
        return previous, new_state

    def evaluate(
        self,
        symbol: str,
        *,
        action: str | None,
        position_open: bool,
        now: float,
    ) -> SymbolQuarantineDecision:
        normalized = _normalize_symbol(symbol)
        normalized_action = str(action or "").upper().strip()
        state = self.state_for(normalized, now=now)
        is_new_entry = normalized_action in OPEN_ENTRY_ACTIONS
        blocks_new_entry = bool(
            state.level is SymbolQuarantineLevel.QUARANTINED and is_new_entry and not position_open
        )
        return SymbolQuarantineDecision(
            symbol=normalized,
            action=normalized_action,
            allowed=not blocks_new_entry,
            level=state.level,
            reason=(
                "SYMBOL_QUARANTINED_NEW_ENTRY_BLOCKED"
                if blocks_new_entry
                else "SYMBOL_QUARANTINE_OBSERVED"
            ),
            blocks_new_entry=blocks_new_entry,
            position_open=bool(position_open),
            state=state,
            diagnostics={
                "is_new_entry": is_new_entry,
                "quarantine_blocks_only_new_entries": True,
            },
        )

    def penalty_for(self, symbol: str, *, now: float) -> int:
        level = self.state_for(symbol, now=now).level
        if level is SymbolQuarantineLevel.WATCH:
            return 5
        if level is SymbolQuarantineLevel.COOLDOWN:
            return 15
        if level is SymbolQuarantineLevel.QUARANTINED:
            return 30
        return 0

    def _level_for_count(self, error_count: int) -> SymbolQuarantineLevel:
        if error_count >= self.settings.quarantine_after_errors:
            return SymbolQuarantineLevel.QUARANTINED
        if error_count >= self.settings.cooldown_after_errors:
            return SymbolQuarantineLevel.COOLDOWN
        if error_count >= self.settings.watch_after_errors:
            return SymbolQuarantineLevel.WATCH
        return SymbolQuarantineLevel.NONE

    def _expires_at_for_level(
        self,
        level: SymbolQuarantineLevel,
        now: float,
    ) -> float | None:
        if level is SymbolQuarantineLevel.QUARANTINED:
            return now + self.settings.quarantine_seconds
        if level in {SymbolQuarantineLevel.WATCH, SymbolQuarantineLevel.COOLDOWN}:
            return now + self.settings.cooldown_seconds
        return None


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").upper().strip()
