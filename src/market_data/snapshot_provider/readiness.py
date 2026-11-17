"""Readiness of an operational snapshot for evaluation, entries and exits."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from market_data.market_snapshots import normalize_symbol
from market_data.snapshot_engine import (
    ResourceKey,
    Snapshot,
    SnapshotAcceptancePolicy,
    SnapshotConsistencyError,
    SnapshotResult,
)
from market_data.snapshot_provider.keys import normalized_symbols
from market_data.snapshot_provider.policies import SnapshotPolicies


class SnapshotReadiness(IntEnum):
    UNAVAILABLE = 0
    PARTIAL = 1
    READ_ONLY = 2
    EXIT_SAFE = 3
    ENTRY_SAFE = 4

    @property
    def allows_entry(self) -> bool:
        return self is SnapshotReadiness.ENTRY_SAFE

    @property
    def allows_exit(self) -> bool:
        return self >= SnapshotReadiness.EXIT_SAFE


@dataclass(frozen=True)
class SnapshotReadinessDiagnostic:
    readiness: SnapshotReadiness
    reason: str
    policy_profile: str
    snapshot_id: str = ""
    missing_resources: tuple[str, ...] = ()
    acceptance_violations: tuple[dict[str, Any], ...] = ()
    consistency_violations: tuple[dict[str, Any], ...] = ()
    max_age_seconds: float | None = None
    observation_skew_seconds: float | None = None
    authority_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "readiness": self.readiness.name,
            "reason": self.reason,
            "policy_profile": self.policy_profile,
            "snapshot_id": self.snapshot_id,
            "missing_resources": list(self.missing_resources),
            "acceptance_violations": list(self.acceptance_violations),
            "consistency_violations": list(self.consistency_violations),
            "max_age_seconds": self.max_age_seconds,
            "observation_skew_seconds": self.observation_skew_seconds,
            "authority_summary": dict(self.authority_summary),
        }


def not_evaluated_diagnostic(policy_profile: str) -> SnapshotReadinessDiagnostic:
    return SnapshotReadinessDiagnostic(
        readiness=SnapshotReadiness.UNAVAILABLE,
        reason="NOT_EVALUATED",
        policy_profile=policy_profile,
    )


_SATISFIED = {
    "entry": (SnapshotReadiness.ENTRY_SAFE, "ENTRY_POLICY_SATISFIED"),
    "exit": (SnapshotReadiness.EXIT_SAFE, "EXIT_POLICY_SATISFIED"),
    "evaluation": (SnapshotReadiness.READ_ONLY, "EVALUATION_POLICY_SATISFIED"),
    "baseline": (SnapshotReadiness.READ_ONLY, "BASELINE_POLICY_SATISFIED"),
}


class ReadinessDiagnostics:
    """Classify a snapshot (or a try-result) against the policy of one usage profile."""

    def __init__(self, policies: SnapshotPolicies) -> None:
        self.policies = policies

    def from_result(
        self,
        result: SnapshotResult,
        *,
        profile: str,
        symbol: str | None = None,
        symbols: Iterable[str] = (),
    ) -> SnapshotReadinessDiagnostic:
        normalized_profile = str(profile or "").lower().strip()
        normalized_symbol = normalize_symbol(symbol or "") if symbol is not None else ""
        policy, expected_keys = self._policy_and_keys(
            normalized_profile,
            symbol=normalized_symbol,
            symbols=symbols,
        )
        snapshot = result.snapshot
        readiness, reason = self._result_readiness(result, normalized_profile)
        max_age, skew = snapshot_age_and_skew(snapshot)
        return SnapshotReadinessDiagnostic(
            readiness=readiness,
            reason=reason,
            policy_profile=normalized_profile,
            snapshot_id=str(snapshot.snapshot_id),
            missing_resources=tuple(
                str(item) for item in missing_readiness_resources(snapshot, expected_keys, policy)
            ),
            acceptance_violations=tuple(
                _acceptance_violation_payload(violation)
                for violation in result.acceptance_violations
            ),
            consistency_violations=(
                (_consistency_violation_payload(result.consistency_violation),)
                if result.consistency_violation is not None
                else ()
            ),
            max_age_seconds=max_age,
            observation_skew_seconds=skew,
            authority_summary=authority_summary(snapshot),
        )

    @staticmethod
    def _result_readiness(
        result: SnapshotResult,
        profile: str,
    ) -> tuple[SnapshotReadiness, str]:
        if result.accepted:
            if profile in {"entry", "exit"}:
                return _SATISFIED[profile]
            return SnapshotReadiness.READ_ONLY, "EVALUATION_POLICY_SATISFIED"
        if result.degraded:
            allowed = set(result.allowed_actions)
            if "reduce_only_exit" in allowed or "cancel_orders" in allowed:
                return SnapshotReadiness.EXIT_SAFE, "DEGRADED_EXIT_SAFE"
            if "evaluate" in allowed:
                return SnapshotReadiness.READ_ONLY, "DEGRADED_READ_ONLY"
            if "read" in allowed:
                return SnapshotReadiness.PARTIAL, "DEGRADED_PARTIAL"
            return SnapshotReadiness.UNAVAILABLE, "DEGRADED_NO_ALLOWED_ACTIONS"
        readiness = (
            SnapshotReadiness.PARTIAL
            if result.snapshot.resources
            else SnapshotReadiness.UNAVAILABLE
        )
        return readiness, "REJECTED"

    def from_snapshot(
        self,
        snapshot: Snapshot | None,
        *,
        profile: str,
        symbol: str | None = None,
        symbols: Iterable[str] = (),
        error: BaseException | None = None,
    ) -> SnapshotReadinessDiagnostic:
        normalized_profile = str(profile or "").lower().strip()
        normalized_symbol = normalize_symbol(symbol or "") if symbol is not None else ""
        policy, expected_keys = self._policy_and_keys(
            normalized_profile,
            symbol=normalized_symbol,
            symbols=symbols,
        )
        missing = missing_readiness_resources(snapshot, expected_keys, policy)
        acceptance = [
            _acceptance_violation_payload(violation)
            for violation in tuple(getattr(error, "violations", ()) or ())
        ]
        consistency = (
            [_consistency_violation_payload(error)]
            if isinstance(error, SnapshotConsistencyError)
            else []
        )

        if snapshot is None:
            readiness, reason = SnapshotReadiness.UNAVAILABLE, "SNAPSHOT_UNAVAILABLE"
        elif acceptance or consistency:
            readiness, reason = SnapshotReadiness.PARTIAL, "POLICY_VIOLATION"
        elif missing:
            readiness = (
                SnapshotReadiness.READ_ONLY
                if normalized_profile in {"baseline", "evaluation"} and snapshot.resources
                else SnapshotReadiness.PARTIAL
            )
            reason = "REQUIRED_RESOURCES_MISSING"
        elif snapshot.errors:
            readiness, reason = SnapshotReadiness.PARTIAL, "RESOURCE_ERRORS_PRESENT"
        else:
            readiness, reason = _SATISFIED.get(
                normalized_profile, (SnapshotReadiness.PARTIAL, "UNKNOWN_PROFILE")
            )

        max_age, skew = snapshot_age_and_skew(snapshot)
        return SnapshotReadinessDiagnostic(
            readiness=readiness,
            reason=reason,
            policy_profile=normalized_profile,
            snapshot_id="" if snapshot is None else str(snapshot.snapshot_id),
            missing_resources=tuple(str(item) for item in missing),
            acceptance_violations=tuple(acceptance),
            consistency_violations=tuple(consistency),
            max_age_seconds=max_age,
            observation_skew_seconds=skew,
            authority_summary=authority_summary(snapshot),
        )

    def _policy_and_keys(
        self,
        profile: str,
        *,
        symbol: str,
        symbols: Iterable[str],
    ) -> tuple[SnapshotAcceptancePolicy, tuple[ResourceKey, ...]]:
        if profile == "entry":
            policy = self.policies.entry_acceptance(symbol)
        elif profile == "exit":
            policy = self.policies.exit_acceptance(symbol)
        elif profile == "evaluation":
            policy = self.policies.evaluation_acceptance(normalized_symbols(symbols))
        else:
            policy = self.policies.baseline_acceptance(symbols)
        return policy, tuple(policy.referenced_keys)


def missing_readiness_resources(
    snapshot: Snapshot | None,
    expected_keys: Iterable[ResourceKey],
    policy: SnapshotAcceptancePolicy,
) -> tuple[ResourceKey, ...]:
    if snapshot is None:
        return tuple(dict.fromkeys(expected_keys))
    resources = snapshot.resources
    requirement_keys = {key for requirement in policy.requirements for key in requirement.keys}
    missing: list[ResourceKey] = [
        key for key in expected_keys if key not in resources and key not in requirement_keys
    ]
    for requirement in policy.requirements:
        accepted_count = sum(1 for key in requirement.keys if key in resources)
        if accepted_count >= requirement.minimum_count:
            continue
        missing.extend(key for key in requirement.keys if key not in resources)
    return tuple(dict.fromkeys(missing))


def snapshot_age_and_skew(snapshot: Snapshot | None) -> tuple[float | None, float | None]:
    if snapshot is None or not snapshot.resources:
        return None, None
    ages = [
        max(0.0, float(getattr(item, "age_seconds", 0.0))) for item in snapshot.resources.values()
    ]
    observed = [float(item.observed_at) for item in snapshot.resources.values()]
    return max(ages), max(observed) - min(observed) if len(observed) > 1 else 0.0


def authority_summary(snapshot: Snapshot | None) -> dict[str, Any]:
    if snapshot is None or not snapshot.resources:
        return {"resource_count": 0}
    ranks = [int(getattr(item, "authority_rank", 0)) for item in snapshot.resources.values()]
    sources: dict[str, int] = {}
    for item in snapshot.resources.values():
        source = str(getattr(item, "source", ""))
        sources[source] = sources.get(source, 0) + 1
    return {
        "resource_count": len(ranks),
        "min_authority_rank": min(ranks),
        "max_authority_rank": max(ranks),
        "sources": sources,
    }


def _acceptance_violation_payload(violation: Any) -> dict[str, Any]:
    return {
        "reason": str(getattr(getattr(violation, "reason", ""), "value", "")),
        "message": str(getattr(violation, "message", "")),
        "keys": [str(key) for key in getattr(violation, "keys", ())],
        "current_age_seconds": getattr(violation, "current_age_seconds", None),
        "max_age_seconds": getattr(violation, "max_age_seconds", None),
        "authority_rank": getattr(violation, "authority_rank", None),
        "minimum_authority_rank": getattr(violation, "minimum_authority_rank", None),
        "required_count": getattr(violation, "required_count", None),
        "accepted_count": getattr(violation, "accepted_count", None),
    }


def _consistency_violation_payload(violation: Any) -> dict[str, Any]:
    return {
        "keys": [str(key) for key in getattr(violation, "keys", ())],
        "oldest_key": str(getattr(violation, "oldest_key", "")),
        "newest_key": str(getattr(violation, "newest_key", "")),
        "observation_skew_seconds": getattr(violation, "observation_skew_seconds", None),
        "max_observation_skew_seconds": getattr(violation, "max_observation_skew_seconds", None),
    }


__all__ = [
    "ReadinessDiagnostics",
    "SnapshotReadiness",
    "SnapshotReadinessDiagnostic",
    "authority_summary",
    "missing_readiness_resources",
    "not_evaluated_diagnostic",
    "snapshot_age_and_skew",
]
