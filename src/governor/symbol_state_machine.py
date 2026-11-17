from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from governor.events import SYMBOL_OPERATIONAL_STATE_TRANSITION
from governor.safe_journal import safe_append


class SymbolOperationalState(str, Enum):
    FLAT = "FLAT"
    LIGHT_EVALUATED = "LIGHT_EVALUATED"
    HEAVY_REQUIRED = "HEAVY_REQUIRED"
    HEAVY_EVALUATED = "HEAVY_EVALUATED"
    SIGNAL_CANDIDATE = "SIGNAL_CANDIDATE"
    TRADE_PLANNED = "TRADE_PLANNED"
    ECONOMIC_APPROVED = "ECONOMIC_APPROVED"
    INTELLIGENT_LAYERS_APPLIED = "INTELLIGENT_LAYERS_APPLIED"
    FINAL_ECONOMIC_APPROVED = "FINAL_ECONOMIC_APPROVED"
    PORTFOLIO_APPROVED = "PORTFOLIO_APPROVED"
    LEARNING_APPLIED = "LEARNING_APPLIED"
    EXECUTION_WAITING = "EXECUTION_WAITING"
    EXECUTION_APPROVED = "EXECUTION_APPROVED"
    ENTRY_SENT = "ENTRY_SENT"
    POSITION_OPEN = "POSITION_OPEN"
    PROTECTIONS_PENDING = "PROTECTIONS_PENDING"
    PROTECTED = "PROTECTED"
    CLOSE_REQUESTED = "CLOSE_REQUESTED"
    CLOSE_SENT = "CLOSE_SENT"
    FLAT_CONFIRMED = "FLAT_CONFIRMED"
    MANUAL_RECONCILE_REQUIRED = "MANUAL_RECONCILE_REQUIRED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


FLAT = SymbolOperationalState.FLAT.value
LIGHT_EVALUATED = SymbolOperationalState.LIGHT_EVALUATED.value
HEAVY_REQUIRED = SymbolOperationalState.HEAVY_REQUIRED.value
HEAVY_EVALUATED = SymbolOperationalState.HEAVY_EVALUATED.value
SIGNAL_CANDIDATE = SymbolOperationalState.SIGNAL_CANDIDATE.value
TRADE_PLANNED = SymbolOperationalState.TRADE_PLANNED.value
ECONOMIC_APPROVED = SymbolOperationalState.ECONOMIC_APPROVED.value
INTELLIGENT_LAYERS_APPLIED = SymbolOperationalState.INTELLIGENT_LAYERS_APPLIED.value
FINAL_ECONOMIC_APPROVED = SymbolOperationalState.FINAL_ECONOMIC_APPROVED.value
PORTFOLIO_APPROVED = SymbolOperationalState.PORTFOLIO_APPROVED.value
LEARNING_APPLIED = SymbolOperationalState.LEARNING_APPLIED.value
EXECUTION_WAITING = SymbolOperationalState.EXECUTION_WAITING.value
EXECUTION_APPROVED = SymbolOperationalState.EXECUTION_APPROVED.value
ENTRY_SENT = SymbolOperationalState.ENTRY_SENT.value
POSITION_OPEN = SymbolOperationalState.POSITION_OPEN.value
PROTECTIONS_PENDING = SymbolOperationalState.PROTECTIONS_PENDING.value
PROTECTED = SymbolOperationalState.PROTECTED.value
CLOSE_REQUESTED = SymbolOperationalState.CLOSE_REQUESTED.value
CLOSE_SENT = SymbolOperationalState.CLOSE_SENT.value
FLAT_CONFIRMED = SymbolOperationalState.FLAT_CONFIRMED.value
MANUAL_RECONCILE_REQUIRED = SymbolOperationalState.MANUAL_RECONCILE_REQUIRED.value
BLOCKED = SymbolOperationalState.BLOCKED.value
ERROR = SymbolOperationalState.ERROR.value


@dataclass(frozen=True)
class SymbolOperationalTransition:
    symbol: str
    from_state: str
    to_state: str
    reason: str
    stage: str
    cycle_id: str
    symbol_eval_id: str
    trade_plan_id: str | None = None
    action: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


class SymbolStateTracker:
    """Observational journey tracker for Governor symbol evaluations.

    This tracker describes the Governor's evaluation path, not a financial
    position. PositionLedger owns local position state; Binance is the external
    authority reconciled into that ledger. A transition such as
    BLOCKED -> LIGHT_EVALUATED is expected when a new evaluation cycle starts;
    it does not mean a market position changed.
    """

    def __init__(self, journal: Any = None, logger: Any = None) -> None:
        self.journal = journal
        self.logger = logger
        self._states: dict[str, str] = {}

    def state_for(self, symbol: str) -> str:
        return self._states.get(
            _normalize_symbol(symbol),
            SymbolOperationalState.FLAT.value,
        )

    def transition(
        self,
        *,
        symbol: str,
        to_state: SymbolOperationalState | str,
        reason: str,
        stage: str,
        cycle_id: str = "",
        symbol_eval_id: str = "",
        trade_plan_id: str | None = None,
        action: str | None = None,
        diagnostics: dict[str, Any] | None = None,
        journal=None,
    ) -> SymbolOperationalTransition:
        normalized_symbol = _normalize_symbol(symbol)
        previous_state = self.state_for(normalized_symbol)
        normalized_to_state = _normalize_state(to_state)
        valid_transition = _is_known_state(previous_state) and _is_known_state(
            normalized_to_state,
        )
        payload_diagnostics = {
            **(diagnostics or {}),
            "valid_transition": valid_transition,
            "observational_only": True,
        }
        transition = SymbolOperationalTransition(
            symbol=normalized_symbol,
            from_state=previous_state,
            to_state=normalized_to_state,
            reason=str(reason or ""),
            stage=str(stage or ""),
            cycle_id=str(cycle_id or ""),
            symbol_eval_id=str(symbol_eval_id or ""),
            trade_plan_id=trade_plan_id,
            action=action,
            diagnostics=payload_diagnostics,
        )
        self._states[normalized_symbol] = normalized_to_state

        target_journal = journal or self.journal
        if target_journal is not None:
            safe_append(
                target_journal,
                SYMBOL_OPERATIONAL_STATE_TRANSITION,
                logger=self.logger,
                **asdict(transition),
            )
        return transition


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "UNKNOWN").upper().strip() or "UNKNOWN"


def _normalize_state(state: SymbolOperationalState | str) -> str:
    if isinstance(state, SymbolOperationalState):
        return state.value
    return str(state or SymbolOperationalState.ERROR.value).upper().strip()


def _is_known_state(state: str) -> bool:
    return state in {item.value for item in SymbolOperationalState}
