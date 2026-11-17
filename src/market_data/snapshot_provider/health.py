"""Builder health: sample it, assess it, alert on it and record it in the audit log."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from market_data.snapshot_engine import BuilderHealth
from market_data.snapshot_provider import wire_names
from utils.events import EventChannel, observe_error, observe_event

COMPONENT = "market_data.snapshot_provider"
DERIVED_KEY = "alphora_derived"


def derived_health(
    engine_payload: Mapping[str, Any],
    *,
    consecutive_waiting_samples: int = 0,
) -> dict[str, Any]:
    """Alphora's operational reading of the engine health: submission backlog pressure."""

    max_pending = engine_payload.get("max_pending_submissions")
    pending = int(engine_payload.get("pending_submissions", 0) or 0)
    if max_pending is not None and int(max_pending) > 0:
        backlog_pct = pending / int(max_pending)
        if backlog_pct >= 1.0:
            severity = "critical"
        elif backlog_pct >= 0.8:
            severity = "warning"
        elif backlog_pct >= 0.5:
            severity = "attention"
        else:
            severity = "normal"
    else:
        backlog_pct = 0.0
        severity = "normal"
    return {
        "backlog_pct": round(backlog_pct, 4),
        "backlog_severity": severity,
        "consecutive_waiting_samples": max(0, int(consecutive_waiting_samples)),
    }


def normalize_builder_health(
    health: Any,
    *,
    previous: BuilderHealth | None = None,
) -> dict[str, Any]:
    """Return the engine's own health payload plus Alphora-derived annotations."""
    to_dict = getattr(health, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("health must expose BuilderHealth.to_dict()")
    engine_payload = to_dict(previous=previous)
    return {
        wire_names.PAYLOAD_KEY: engine_payload,
        DERIVED_KEY: derived_health(engine_payload),
    }


class HealthMonitor:
    """Compare each health sample with the previous one and raise the alerts it implies."""

    def __init__(self, health_snapshot: Callable[[], BuilderHealth]) -> None:
        self._health_snapshot = health_snapshot
        self._baseline: BuilderHealth | None = None
        self._waiting_for_capacity_samples = 0

    @property
    def baseline(self) -> BuilderHealth | None:
        """The last sample, the reference the next one is compared with."""
        return self._baseline

    def collect(self, label: str) -> dict[str, Any]:
        """Snapshot BuilderHealth, assess it, emit alerts, and record to audit log.

        Call this at cycle end, heartbeat, important errors, and shutdown.
        Returns a JSON-safe payload with the engine's own health separated
        from Alphora-derived operational annotations.
        """
        try:
            raw_health = self._health_snapshot()
        except Exception as error:
            return {
                "available": False,
                "label": label,
                "error_type": type(error).__name__,
            }

        previous_health = self._baseline
        self._baseline = raw_health

        engine_payload = raw_health.to_dict(previous=previous_health)
        assessment_payload = raw_health.assess(previous=previous_health).to_dict()

        self._alert_backlog(engine_payload)
        self._alert_findings(assessment_payload, label=label)
        self._track_waiting_for_capacity(engine_payload)

        health_payload: dict[str, Any] = {
            "available": True,
            "label": label,
            wire_names.PAYLOAD_KEY: engine_payload,
            DERIVED_KEY: derived_health(
                engine_payload,
                consecutive_waiting_samples=self._waiting_for_capacity_samples,
            ),
            "assessment": assessment_payload,
        }
        observe_event(
            wire_names.BUILDER_HEALTH,
            component=__name__,
            category="market_data",
            channel=EventChannel.AUDIT,
            **health_payload,
        )
        return health_payload

    def _alert_backlog(self, engine_payload: Mapping[str, Any]) -> None:
        derived = derived_health(
            engine_payload,
            consecutive_waiting_samples=self._waiting_for_capacity_samples,
        )
        severity = derived.get("backlog_severity", "normal")
        if severity not in ("warning", "critical"):
            return
        observe_error(
            wire_names.BACKLOG_HIGH,
            error=RuntimeError(
                f"snapshot submission backlog at {derived.get('backlog_pct', 0):.0%}"
                f" ({engine_payload.get('pending_submissions', 0)}"
                f"/{engine_payload.get('max_pending_submissions', '?')} pending)"
            ),
            component=COMPONENT,
            category="market_data",
            source=wire_names.ERROR_SOURCE_BUILDER,
            backlog_severity=severity,
            pending_submissions=engine_payload.get("pending_submissions"),
            max_pending_submissions=engine_payload.get("max_pending_submissions"),
        )

    @staticmethod
    def _alert_findings(assessment_payload: Mapping[str, Any], *, label: str) -> None:
        for finding in assessment_payload.get("findings", []):
            finding_severity = str(finding.get("severity", ""))
            if finding_severity not in {"degraded", "critical"}:
                continue
            observe_error(
                wire_names.BUILDER_HEALTH_FINDING,
                error=RuntimeError(
                    f"snapshot builder health finding: {finding.get('reason', 'unknown')}"
                ),
                component=COMPONENT,
                category="market_data",
                source=wire_names.ERROR_SOURCE_BUILDER,
                severity=finding_severity,
                reason=finding.get("reason"),
                metric=finding.get("metric"),
                observed=finding.get("observed"),
                threshold=finding.get("threshold"),
                health_label=label,
            )

    def _track_waiting_for_capacity(self, engine_payload: Mapping[str, Any]) -> None:
        if engine_payload.get("waiting_for_capacity", 0) > 0:
            self._waiting_for_capacity_samples += 1
        else:
            self._waiting_for_capacity_samples = 0

        if self._waiting_for_capacity_samples < 2:
            return
        observe_error(
            wire_names.WAITING_FOR_CAPACITY,
            error=RuntimeError("snapshot builder is waiting for worker capacity"),
            component=COMPONENT,
            category="market_data",
            source=wire_names.ERROR_SOURCE_BUILDER,
            waiting_for_capacity=engine_payload.get("waiting_for_capacity"),
            consecutive_samples=self._waiting_for_capacity_samples,
        )


__all__ = ["COMPONENT", "HealthMonitor", "derived_health", "normalize_builder_health"]
