from enum import Enum


class ExecutionMode(Enum):
    DRY_RUN = "DRY_RUN"
    PAPER_LIVE = "PAPER_LIVE"
    TESTNET_LIVE = "TESTNET_LIVE"
    REAL_LIVE = "REAL_LIVE"

    @property
    def is_live(self) -> bool:
        return self in {ExecutionMode.TESTNET_LIVE, ExecutionMode.REAL_LIVE}

    @property
    def is_paper(self) -> bool:
        return self is ExecutionMode.PAPER_LIVE


class ExchangeEnvironment(Enum):
    TESTNET = "TESTNET"
    REAL = "REAL"


class MarginType(Enum):
    ISOLATED = "ISOLATED"
    CROSSED = "CROSSED"


class RecoveryAction(Enum):
    PROTECT = "PROTECT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


class ProtectionPolicy(Enum):
    AUTO_REPAIR = "AUTO_REPAIR"
    CLOSE_ON_UNPROTECTED = "CLOSE_ON_UNPROTECTED"
    PAUSE = "PAUSE"


class ProtectionWorkingType(Enum):
    MARK_PRICE = "MARK_PRICE"
    CONTRACT_PRICE = "CONTRACT_PRICE"


class ExitModelType(Enum):
    FIXED_PCT = "FIXED_PCT"
    ATR = "ATR"


class ArchitectureMode(Enum):
    INTELLIGENT_FLOW = "INTELLIGENT_FLOW"


class IntelligentActivationLevel(Enum):
    SHADOW_ONLY = "SHADOW_ONLY"
    PAPER_ACTIVE = "PAPER_ACTIVE"
    TESTNET_OBSERVE = "TESTNET_OBSERVE"
    TESTNET_REDUCE = "TESTNET_REDUCE"
    TESTNET_BLOCK = "TESTNET_BLOCK"
    REAL_OBSERVE = "REAL_OBSERVE"
    REAL_REDUCE = "REAL_REDUCE"
    REAL_BLOCK = "REAL_BLOCK"
