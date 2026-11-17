from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from futures.planning.economic_gate import EconomicGateDecision
from trading.enums import OPEN_ACTIONS


class ExecutionStyle(str, Enum):
    MARKET_SAFE = "MARKET_SAFE"
    LIMIT_MAKER_FIRST = "LIMIT_MAKER_FIRST"
    LIMIT_WITH_MARKET_FALLBACK = "LIMIT_WITH_MARKET_FALLBACK"
    MARKET_ONLY = "MARKET_ONLY"


@dataclass(frozen=True)
class OrderExecutionPlan:
    allowed: bool
    reason: str
    symbol: str | None
    action: str
    execution_style: str
    entry_order_type: str | None
    assumes_entry_liquidity: str | None
    assumes_exit_liquidity: str | None
    market_price: Decimal | None
    reference_price: Decimal | None
    limit_price: Decimal | None = None
    post_only: bool = False
    timeout_seconds: Decimal | None = None
    fallback_to_market: bool = False
    operational_entry_order_type: str | None = None
    executor_behavior_unchanged: bool = True
    note: str = "LIMIT styles are planned but not operational in this release"
    diagnostics: dict[str, Any] = field(default_factory=dict)


class OrderExecutionPolicy:
    """Selects entry execution style without changing the current executor contract."""

    def __init__(
        self,
        *,
        execution_style: str = ExecutionStyle.MARKET_SAFE.value,
        limit_entry_enabled: bool = False,
        limit_entry_timeout_seconds: Decimal = Decimal("3"),
        limit_entry_max_price_drift_bps: Decimal = Decimal("5"),
        limit_entry_fallback_to_market: bool = False,
        limit_entry_post_only: bool = True,
        market_entry_require_economic_gate: bool = True,
    ):
        self.execution_style = _normalize_style(execution_style)
        self.limit_entry_enabled = bool(limit_entry_enabled)
        self.limit_entry_timeout_seconds = Decimal(str(limit_entry_timeout_seconds))
        self.limit_entry_max_price_drift_bps = Decimal(str(limit_entry_max_price_drift_bps))
        self.limit_entry_fallback_to_market = bool(limit_entry_fallback_to_market)
        self.limit_entry_post_only = bool(limit_entry_post_only)
        self.market_entry_require_economic_gate = bool(market_entry_require_economic_gate)

    @classmethod
    def from_config(cls, config) -> "OrderExecutionPolicy":
        return cls(
            execution_style=getattr(
                config,
                "entry_execution_style",
                ExecutionStyle.MARKET_SAFE.value,
            ),
            limit_entry_enabled=getattr(config, "limit_entry_enabled", False),
            limit_entry_timeout_seconds=Decimal(
                str(getattr(config, "limit_entry_timeout_seconds", "3"))
            ),
            limit_entry_max_price_drift_bps=Decimal(
                str(getattr(config, "limit_entry_max_price_drift_bps", "5"))
            ),
            limit_entry_fallback_to_market=getattr(
                config,
                "limit_entry_fallback_to_market",
                False,
            ),
            limit_entry_post_only=getattr(config, "limit_entry_post_only", True),
            market_entry_require_economic_gate=getattr(
                config,
                "market_entry_require_economic_gate",
                True,
            ),
        )

    def build_entry_plan(
        self,
        *,
        action: str,
        symbol: str | None = None,
        market_price: Decimal | None,
        reference_price: Decimal | None = None,
        economic_decision: EconomicGateDecision | None = None,
        spread_bps: Decimal | None = None,
        signal_confidence: int | None = None,
        user_style: str | None = None,
    ) -> OrderExecutionPlan:
        normalized_action = str(action or "").upper().strip()
        requested_style = _normalize_style(user_style or self.execution_style)
        diagnostics = self._base_diagnostics(
            requested_style=requested_style,
            economic_decision=economic_decision,
            spread_bps=spread_bps,
            signal_confidence=signal_confidence,
        )

        if normalized_action not in OPEN_ACTIONS:
            return OrderExecutionPlan(
                allowed=True,
                reason="ACTION_NOT_OPEN",
                symbol=symbol,
                action=normalized_action,
                execution_style=requested_style,
                entry_order_type=None,
                assumes_entry_liquidity=None,
                assumes_exit_liquidity=None,
                market_price=market_price,
                reference_price=reference_price,
                diagnostics=diagnostics,
            )

        gate_block = self._market_gate_block(economic_decision)
        if gate_block is not None:
            return self._blocked(
                reason=gate_block,
                symbol=symbol,
                action=normalized_action,
                requested_style=requested_style,
                market_price=market_price,
                reference_price=reference_price,
                diagnostics=diagnostics,
            )

        if requested_style in {
            ExecutionStyle.MARKET_SAFE.value,
            ExecutionStyle.MARKET_ONLY.value,
        }:
            # MARKET_SAFE preserves current behavior and must assume taker on both legs.
            return OrderExecutionPlan(
                allowed=True,
                reason=f"{requested_style}_SELECTED",
                symbol=symbol,
                action=normalized_action,
                execution_style=requested_style,
                entry_order_type="MARKET",
                assumes_entry_liquidity="TAKER",
                assumes_exit_liquidity="TAKER",
                market_price=market_price,
                reference_price=reference_price,
                fallback_to_market=False,
                operational_entry_order_type="MARKET",
                executor_behavior_unchanged=True,
                note="LIMIT styles are planned but not operational in this release",
                diagnostics={
                    **diagnostics,
                    "cost_assumption": "TAKER_ENTRY_TAKER_EXIT",
                    "executor_behavior_unchanged": True,
                    "operational_entry_order_type": "MARKET",
                    "note": ("LIMIT styles are planned but not operational in this release"),
                },
            )

        if not self.limit_entry_enabled:
            return self._blocked(
                reason="LIMIT_ENTRY_DISABLED",
                symbol=symbol,
                action=normalized_action,
                requested_style=requested_style,
                market_price=market_price,
                reference_price=reference_price,
                diagnostics=diagnostics,
            )

        if requested_style == ExecutionStyle.LIMIT_MAKER_FIRST.value:
            return self._blocked(
                reason="LIMIT_MAKER_FIRST_NOT_OPERATIONAL_WITHOUT_FILL_RECONCILIATION",
                symbol=symbol,
                action=normalized_action,
                requested_style=requested_style,
                market_price=market_price,
                reference_price=reference_price,
                diagnostics={
                    **diagnostics,
                    "safety_note": (
                        "LIMIT real exige fill parcial, timeout, cancelamento, "
                        "reconciliacao e protecao da quantidade preenchida."
                    ),
                },
            )

        if requested_style == ExecutionStyle.LIMIT_WITH_MARKET_FALLBACK.value:
            return self._blocked(
                reason="LIMIT_ENTRY_NOT_OPERATIONAL_WITHOUT_FILL_RECONCILIATION",
                symbol=symbol,
                action=normalized_action,
                requested_style=requested_style,
                market_price=market_price,
                reference_price=reference_price,
                diagnostics={
                    **diagnostics,
                    "safety_note": (
                        "Fallback para MARKET so pode existir depois de LIMIT real "
                        "com fill, timeout, cancelamento, parcial, reconciliacao "
                        "e revalidacao economica taker/taker."
                    ),
                },
            )

        return self._blocked(
            reason="UNSUPPORTED_EXECUTION_STYLE",
            symbol=symbol,
            action=normalized_action,
            requested_style=requested_style,
            market_price=market_price,
            reference_price=reference_price,
            operational_entry_order_type=None,
            executor_behavior_unchanged=True,
            note="LIMIT styles are planned but not operational in this release",
            diagnostics=diagnostics,
        )

    def _market_gate_block(
        self,
        economic_decision: EconomicGateDecision | None,
    ) -> str | None:
        if not self.market_entry_require_economic_gate:
            return None
        if economic_decision is None:
            return "ECONOMIC_GATE_DECISION_REQUIRED"
        if not economic_decision.allowed:
            return "ECONOMIC_GATE_NOT_APPROVED"
        return None

    def _blocked(
        self,
        *,
        reason: str,
        symbol: str | None,
        action: str,
        requested_style: str,
        market_price: Decimal | None,
        reference_price: Decimal | None,
        diagnostics: dict[str, Any],
        operational_entry_order_type: str | None = None,
        executor_behavior_unchanged: bool = True,
        note: str = "LIMIT styles are planned but not operational in this release",
    ) -> OrderExecutionPlan:
        return OrderExecutionPlan(
            allowed=False,
            reason=reason,
            symbol=symbol,
            action=action,
            execution_style=requested_style,
            entry_order_type=None,
            assumes_entry_liquidity=None,
            assumes_exit_liquidity=None,
            market_price=market_price,
            reference_price=reference_price,
            operational_entry_order_type=operational_entry_order_type,
            executor_behavior_unchanged=executor_behavior_unchanged,
            note=note,
            diagnostics=diagnostics,
        )

    def _base_diagnostics(
        self,
        *,
        requested_style: str,
        economic_decision: EconomicGateDecision | None,
        spread_bps: Decimal | None,
        signal_confidence: int | None,
    ) -> dict[str, Any]:
        return {
            "order_policy_version": "1.0.0",
            "configured_execution_style": self.execution_style,
            "requested_execution_style": requested_style,
            "limit_entry_enabled": self.limit_entry_enabled,
            "limit_entry_timeout_seconds": str(self.limit_entry_timeout_seconds),
            "limit_entry_max_price_drift_bps": str(self.limit_entry_max_price_drift_bps),
            "limit_entry_fallback_to_market": self.limit_entry_fallback_to_market,
            "limit_entry_post_only": self.limit_entry_post_only,
            "market_entry_require_economic_gate": (self.market_entry_require_economic_gate),
            "executor_behavior_unchanged": True,
            "operational_entry_order_type": "MARKET",
            "note": "LIMIT styles are planned but not operational in this release",
            "economic_gate_allowed": (
                economic_decision.allowed if economic_decision is not None else None
            ),
            "economic_gate_reason": (
                economic_decision.reason if economic_decision is not None else None
            ),
            "spread_bps": str(spread_bps) if spread_bps is not None else None,
            "signal_confidence": signal_confidence,
        }


def _normalize_style(value: str) -> str:
    normalized = str(value or ExecutionStyle.MARKET_SAFE.value).upper().strip()
    allowed = {item.value for item in ExecutionStyle}
    if normalized not in allowed:
        raise ValueError(
            "ALPHORA_ENTRY_EXECUTION_STYLE invalido: "
            f"{normalized}. Valores aceitos: {', '.join(sorted(allowed))}."
        )
    return normalized
