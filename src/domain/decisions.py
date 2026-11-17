"""Common contract for every gate that can allow, block or reduce a trade.

The Governor gates return different types (economic gate, portfolio risk,
pre-execution guard, risk guard, defensive mode, AI probability, regime policy).
Each one has an adapter to ``GateDecision`` so stages translate a block into a
terminal result in a single place.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class GateVerdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    REDUCE = "reduce"


@dataclass(frozen=True)
class GateDecision:
    """Outcome of one gate.

    ``code`` is the stable machine reason (journal and metrics); ``message`` is
    the reason a stage reports when the gate stops the evaluation (it defaults
    to ``code``). ``values`` and ``limits`` carry what was measured and the
    thresholds it was measured against; ``source_age_seconds`` is the age of the
    data the gate read, when known.
    """

    gate: str
    verdict: GateVerdict
    code: str
    message: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, Any] = field(default_factory=dict)
    source_age_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.gate:
            raise ValueError("GateDecision needs a gate name")
        if not self.code:
            raise ValueError("GateDecision needs a stable reason code")
        if not self.message:
            object.__setattr__(self, "message", self.code)
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))

    @property
    def blocked(self) -> bool:
        return self.verdict is GateVerdict.BLOCK

    @property
    def allowed(self) -> bool:
        return not self.blocked

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "verdict": self.verdict.value,
            "code": self.code,
            "message": self.message,
            "values": dict(self.values),
            "limits": dict(self.limits),
            "source_age_seconds": self.source_age_seconds,
        }


__all__ = ["GateDecision", "GateVerdict"]
