from enum import Enum


class TradeAction(str, Enum):
    HOLD = "HOLD"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_POSITION = "CLOSE_POSITION"


class TradeActionCategory(str, Enum):
    RISK_INCREASING = "RISK_INCREASING"
    RISK_REDUCING = "RISK_REDUCING"
    PROTECTIVE_REPAIR = "PROTECTIVE_REPAIR"
    NEUTRAL = "NEUTRAL"


class TradePlanStatus(str, Enum):
    BLOCK_SAFETY = "BLOCK_SAFETY"
    BLOCK_ECONOMIC = "BLOCK_ECONOMIC"
    ALLOW_MIN_SIZE = "ALLOW_MIN_SIZE"
    ALLOW_REDUCED = "ALLOW_REDUCED"
    ALLOW_NORMAL = "ALLOW_NORMAL"
    ALLOW_AGGRESSIVE = "ALLOW_AGGRESSIVE"


class OperationalMode(str, Enum):
    EXPLORATORY = "EXPLORATORY"
    NORMAL = "NORMAL"
    CONSERVATIVE = "CONSERVATIVE"
    PROTECTED = "PROTECTED"


OPEN_ACTIONS = {TradeAction.OPEN_LONG.value, TradeAction.OPEN_SHORT.value}
SUPPORTED_ACTIONS = {item.value for item in TradeAction}
RISK_INCREASING_ACTIONS = frozenset({TradeAction.OPEN_LONG.value, TradeAction.OPEN_SHORT.value})
RISK_REDUCING_ACTIONS = frozenset({TradeAction.CLOSE_POSITION.value})
PROTECTIVE_REPAIR_ACTIONS = frozenset({"PROTECT", "RECOVERY", "REPAIR_PROTECTION"})
NEUTRAL_ACTIONS = frozenset({TradeAction.HOLD.value})


def normalize_trade_action(action: str | TradeAction) -> str:
    if isinstance(action, TradeAction):
        return action.value
    return str(action or "").upper().strip()


def classify_trade_action(action: str | TradeAction) -> TradeActionCategory:
    normalized = normalize_trade_action(action)
    if normalized in RISK_INCREASING_ACTIONS:
        return TradeActionCategory.RISK_INCREASING
    if normalized in RISK_REDUCING_ACTIONS:
        return TradeActionCategory.RISK_REDUCING
    if normalized in PROTECTIVE_REPAIR_ACTIONS:
        return TradeActionCategory.PROTECTIVE_REPAIR
    return TradeActionCategory.NEUTRAL


def action_increases_risk(action: str | TradeAction) -> bool:
    return classify_trade_action(action) is TradeActionCategory.RISK_INCREASING


def action_reduces_or_repairs_risk(action: str | TradeAction) -> bool:
    return classify_trade_action(action) in {
        TradeActionCategory.RISK_REDUCING,
        TradeActionCategory.PROTECTIVE_REPAIR,
    }
