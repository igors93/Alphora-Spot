"""Acceptance, consistency and degradation policies of each snapshot usage profile.

Evaluation tolerates older and stale data; entries need fresh, consistent, sufficiently
authoritative private state; exits accept slightly older data and degrade to a reduce-only
snapshot rather than leave a position open.
"""

from __future__ import annotations

from collections.abc import Iterable

from market_data.market_snapshots import normalize_symbol
from market_data.snapshot_engine import (
    RequestDegradationPolicy,
    ResourceAcceptanceRule,
    ResourceKey,
    SnapshotAcceptancePolicy,
    SnapshotConsistencyPolicy,
    SnapshotRequirement,
)
from market_data.snapshot_provider.keys import (
    ACCOUNT,
    ALL_POSITIONS,
    exchange_rules,
    mark_price,
    market_state,
    normalized_symbols,
    open_algo_orders,
    open_orders,
    position,
)
from market_data.snapshot_provider.settings import SnapshotProviderSettings
from market_data.snapshot_provider.sources import (
    MIN_ENTRY_AUTHORITY_RANK,
    MIN_EVALUATION_AUTHORITY_RANK,
    MIN_EXIT_AUTHORITY_RANK,
)

_READ_ACTIONS = ("read", "evaluate")


class SnapshotPolicies:
    """Build the policies of every profile from the provider settings."""

    def __init__(self, settings: SnapshotProviderSettings) -> None:
        self.settings = settings

    # --- acceptance -------------------------------------------------------------------------

    def baseline_acceptance(self, symbols: Iterable[str] = ()) -> SnapshotAcceptancePolicy:
        position_keys = [position(symbol) for symbol in normalized_symbols(symbols)]
        requirements: list[SnapshotRequirement] = [
            SnapshotRequirement.all_of([ACCOUNT], name="baseline-account")
        ]
        if position_keys:
            requirements.append(
                SnapshotRequirement.any_of(position_keys, name="baseline-position-view")
            )
        return SnapshotAcceptancePolicy(
            default_rule=ResourceAcceptanceRule(
                max_age_seconds=self.settings.max_data_age_seconds,
                allow_stale=True,
            ),
            requirements=tuple(requirements),
            include_optional_resources=True,
        )

    def evaluation_acceptance(
        self,
        symbols: Iterable[str] = (),
        *,
        include_optional_resources: bool = True,
    ) -> SnapshotAcceptancePolicy:
        settings = self.settings
        symbols = normalized_symbols(symbols)
        # exchange_rules: 6-hour TTL, bootstrapped at startup — mirrors the exception in
        # entry_acceptance so it is never rejected as too_old during evaluation.
        # mark_price: observed_at comes from the local response receipt. Binance Testnet mark-price
        # timestamps can lag behind wall clock in quiet markets, so max_data_age_seconds (30s)
        # is used here instead of the tighter evaluation_max_age_seconds. Execution
        # revalidation enforces tight freshness before any order is sent.
        resource_rules: dict[ResourceKey, ResourceAcceptanceRule] = {}
        for symbol in symbols:
            resource_rules[exchange_rules(symbol)] = ResourceAcceptanceRule(
                max_age_seconds=settings.exchange_info_ttl_seconds,
                allow_stale=settings.evaluation_allow_stale,
                minimum_authority_rank=MIN_EVALUATION_AUTHORITY_RANK,
            )
            resource_rules[mark_price(symbol)] = ResourceAcceptanceRule(
                max_age_seconds=settings.max_data_age_seconds,
                allow_stale=settings.evaluation_allow_stale,
                minimum_authority_rank=MIN_EVALUATION_AUTHORITY_RANK,
            )
        requirements = [
            SnapshotRequirement.all_of(
                [market_state(symbol), mark_price(symbol), exchange_rules(symbol)],
                name=f"evaluation-market-and-rules:{symbol}",
            )
            for symbol in symbols
        ]
        return SnapshotAcceptancePolicy(
            default_rule=ResourceAcceptanceRule(
                max_age_seconds=settings.evaluation_max_age_seconds,
                allow_stale=settings.evaluation_allow_stale,
                minimum_authority_rank=MIN_EVALUATION_AUTHORITY_RANK,
            ),
            resource_rules=resource_rules,
            requirements=tuple(requirements),
            include_optional_resources=include_optional_resources,
        )

    def entry_acceptance(self, symbol: str) -> SnapshotAcceptancePolicy:
        settings = self.settings
        normalized = normalize_symbol(symbol)
        rules_key = exchange_rules(normalized)
        critical = (
            market_state(normalized),
            mark_price(normalized),
            rules_key,
            ACCOUNT,
            position(normalized),
            open_orders(normalized),
            open_algo_orders(normalized),
        )
        return SnapshotAcceptancePolicy(
            default_rule=ResourceAcceptanceRule(
                max_age_seconds=settings.execution_max_age_seconds,
                allow_stale=settings.execution_allow_stale,
                minimum_authority_rank=MIN_ENTRY_AUTHORITY_RANK,
            ),
            resource_rules={
                rules_key: ResourceAcceptanceRule(
                    max_age_seconds=settings.exchange_info_ttl_seconds,
                    allow_stale=settings.execution_allow_stale,
                    minimum_authority_rank=MIN_ENTRY_AUTHORITY_RANK,
                )
            },
            requirements=(
                SnapshotRequirement.all_of(critical, name=f"entry-critical:{normalized}"),
            ),
            include_optional_resources=True,
        )

    def exit_acceptance(self, symbol: str) -> SnapshotAcceptancePolicy:
        normalized = normalize_symbol(symbol)
        return SnapshotAcceptancePolicy(
            default_rule=ResourceAcceptanceRule(
                max_age_seconds=self.settings.exit_max_age_seconds,
                allow_stale=self.settings.exit_allow_stale,
                minimum_authority_rank=MIN_EXIT_AUTHORITY_RANK,
            ),
            requirements=(
                SnapshotRequirement.any_of(
                    [position(normalized), ALL_POSITIONS],
                    name=f"exit-position-view:{normalized}",
                ),
                SnapshotRequirement.at_least(
                    2,
                    [position(normalized), open_orders(normalized), open_algo_orders(normalized)],
                    name=f"exit-private-state:{normalized}",
                ),
            ),
            include_optional_resources=True,
        )

    def execution_acceptance(self) -> SnapshotAcceptancePolicy:
        return self.entry_acceptance("")

    # --- consistency ------------------------------------------------------------------------

    def evaluation_consistency(self) -> SnapshotConsistencyPolicy:
        # exchange_rules is derived from EXCHANGE_INFO (6h TTL, bootstrapped at startup).
        # Its observed_at grows increasingly old relative to fresh operational data, so the
        # skew limit must accommodate the full TTL window — otherwise the check fires every cycle.
        # Individual resource freshness (evaluation_max_age_seconds) and execution revalidation
        # (execution_max_skew_seconds) provide the actual safety boundaries.
        return SnapshotConsistencyPolicy(
            max_observation_skew_seconds=self.settings.exchange_info_ttl_seconds,
            include_optional_resources=True,
        )

    def entry_consistency(self) -> SnapshotConsistencyPolicy:
        return SnapshotConsistencyPolicy(
            max_observation_skew_seconds=self.settings.execution_max_skew_seconds,
            include_optional_resources=True,
        )

    def exit_consistency(self) -> SnapshotConsistencyPolicy:
        return SnapshotConsistencyPolicy(
            max_observation_skew_seconds=self.settings.exit_max_skew_seconds,
            include_optional_resources=True,
        )

    def execution_consistency(self) -> SnapshotConsistencyPolicy:
        return self.entry_consistency()

    # --- degradation ------------------------------------------------------------------------

    def _read_only_degraded(self, minimum_resolved_resources: int) -> RequestDegradationPolicy:
        if not self.settings.operational_degradation_enabled:
            return RequestDegradationPolicy.fail_closed_policy(accepted_actions=_READ_ACTIONS)
        return RequestDegradationPolicy.read_only_degraded(
            minimum_resolved_resources=minimum_resolved_resources,
            accepted_actions=_READ_ACTIONS,
            degraded_actions=_READ_ACTIONS,
        )

    def baseline_degradation(self) -> RequestDegradationPolicy:
        return self._read_only_degraded(self.settings.baseline_degraded_min_resources)

    def evaluation_degradation(self) -> RequestDegradationPolicy:
        return self._read_only_degraded(self.settings.evaluation_degraded_min_resources)

    def risk_reduction_degradation(self) -> RequestDegradationPolicy:
        if not self.settings.operational_degradation_enabled:
            return RequestDegradationPolicy.fail_closed_policy(
                accepted_actions=("read", "evaluate", "cancel_orders", "reduce_only_exit"),
            )
        return RequestDegradationPolicy.risk_reduction(
            minimum_resolved_resources=self.settings.risk_reduction_degraded_min_resources,
        )


__all__ = ["SnapshotPolicies"]
